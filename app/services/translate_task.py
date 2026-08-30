"""
论文中译任务：为 AI agent 准备翻译任务包，并在完成后回填中文版 PDF。

职责边界
--------
本模块**只做任务机制**，不调 LLM、也不跑 xelatex：

  1. create_task()   —— 从 arXiv 或 IMA 知识库取 LaTeX 源码、探测入口文件
                        与章节文件、在 data/translate/{pid}/ 下写出 task.json。
                        任务包里带一份**中文任务说明**，让 agent 知道要按
                        skills/paper-zh-translator/SKILL.md 的流程执行。
                        如果论文的 ima_tex_path 已存在，优先从 IMA 复用源码，
                        不再去 arXiv 重复下载。
  2. complete_task() —— agent 翻译 + 编译完成后回填：把中文版 PDF 传进 IMA
                        知识库，并把 ima_zh_pdf_path 写回论文的 front-matter。
  3. fail_task()     —— 标记失败并记录原因。

真正的翻译由 agent（Claude / CodeBuddy 等）执行，按钮只负责"建任务"。

落盘结构：
    data/translate/{pid}/source/           ← LaTeX 源码（arXiv 或 IMA 复用）
    data/translate/{pid}/task.json         ← 给 agent 的任务包
    data/translate/{pid}/{prefix}_zh.pdf   ← 中文版 PDF 的目标位置
    data/translate/_tasks.json             ← 任务状态索引 pending / done / failed
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import BASE_DIR, DATA_DIR
from . import arxiv_assets, ima_store
from .ima_client import MT_FOLDER, UNSUPPORTED_EXTS

logger = logging.getLogger(__name__)

ROOT_DIR = Path(BASE_DIR)
TASKS_ROOT = DATA_DIR / "translate"
STATE_FILE = TASKS_ROOT / "_tasks.json"
SKILL_REL = "skills/paper-zh-translator/SKILL.md"

# 编译产物小于这个字节数基本可以断定是 xelatex 中断（skill 里的经验值）
MIN_PDF_BYTES = 512 * 1024
PDF_MAGIC = b"%PDF"

# ----------------------------------------------------------------------
# TeX 结构探测用的正则（对应 SKILL.md 第 1 步的 grep）
# ----------------------------------------------------------------------
_STRIP_COMMENT = re.compile(r"(?<!\\)%.*$")
_DOCUMENTCLASS_RE = re.compile(r"\\documentclass")
_BEGIN_DOC_RE = re.compile(r"\\begin\s*\{document\}")
_APPENDIX_RE = re.compile(r"\\appendix\b")
_INPUT_RE = re.compile(r"\\(?:input|include)\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_SECTION_RE = re.compile(
    r"\\(section|subsection)\s*\*?\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")
# 文件名层面判定附录：X_suppl.tex / supplementary.tex / *_appendix.tex ...
_APPENDIX_NAME_RE = re.compile(
    r"(suppl|supplement|appendix|appendices)", re.IGNORECASE)
# 常见的入口文件名（按优先级）
_ENTRY_CANDIDATES = ("main.tex", "ms.tex", "paper.tex", "arxiv.tex",
                     "root.tex", "article.tex")


class TaskError(RuntimeError):
    """任务失败，reason 是可直接展示给人的中文原因。"""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


# ----------------------------------------------------------------------
# 任务状态索引（JSON，进程内加锁）
# ----------------------------------------------------------------------
_LOCK = threading.RLock()


def _ensure_root() -> None:
    TASKS_ROOT.mkdir(parents=True, exist_ok=True)


def _load_state() -> Dict[str, dict]:
    _ensure_root()
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("翻译任务状态文件损坏，按空处理: %s", exc)
        return {}
    return data.get("tasks") or {} if isinstance(data, dict) else {}


def _save_state(tasks: Dict[str, dict]) -> None:
    _ensure_root()
    STATE_FILE.write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# TeX 结构探测
# ----------------------------------------------------------------------
def scan_tex(path: Path) -> Optional[dict]:
    """
    扫一个 .tex 文件，返回结构信息（对应 SKILL.md 第 1 步的 grep）。

    只读一次文件、逐行跑正则，不把整篇读进上下文，成本极低。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    info: Dict[str, Any] = {
        "path": str(path),
        "has_documentclass": False,
        "has_begin_document": False,
        "appendix_line": None,
        "sections": [],
        "inputs": [],
    }
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _STRIP_COMMENT.sub("", raw)
        if not info["has_documentclass"] and _DOCUMENTCLASS_RE.search(line):
            info["has_documentclass"] = True
        if not info["has_begin_document"] and _BEGIN_DOC_RE.search(line):
            info["has_begin_document"] = True
        if info["appendix_line"] is None and _APPENDIX_RE.search(line):
            info["appendix_line"] = lineno
        for m in _INPUT_RE.finditer(line):
            name = m.group(1).strip()
            if name:
                info["inputs"].append(name)
        for m in _SECTION_RE.finditer(line):
            info["sections"].append(
                {"line": lineno, "level": m.group(1),
                 "title": m.group(2).strip()})
    return info


def _find_entry(source_dir: Path) -> Optional[Path]:
    """
    定位入口 tex 文件。

    优先级：main.tex 等常见名 → 含 \\documentclass 且含 \\begin{document} 的
    （章节多、目录浅的优先）→ 任意含 \\documentclass 的 → 最大的 .tex。
    """
    tex_files = [p for p in source_dir.rglob("*.tex") if p.is_file()]
    if not tex_files:
        return None

    for name in _ENTRY_CANDIDATES:
        cand = source_dir / name
        if cand in tex_files:
            return cand

    scored: List[tuple] = []
    fallback: List[tuple] = []
    for p in tex_files:
        info = scan_tex(p)
        if info is None:
            continue
        if not info["has_documentclass"]:
            continue
        depth = len(p.relative_to(source_dir).parts)
        if info["has_begin_document"]:
            scored.append(((1, len(info["sections"]), -depth), p))
        else:
            fallback.append(((-depth, len(info["sections"])), p))
    for bucket in (scored, fallback):
        if bucket:
            bucket.sort(key=lambda t: t[0], reverse=True)
            return bucket[0][1]

    tex_files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return tex_files[0]


def _resolve_input(source_dir: Path, entry_dir: Path,
                   name: str) -> Optional[Path]:
    """把 \\input{sections/intro} 解析成真实文件路径。"""
    raw = (name or "").strip().strip('"')
    if not raw:
        return None
    stem = raw if raw.lower().endswith(".tex") else raw + ".tex"
    for base in (entry_dir, source_dir):
        cand = (base / stem)
        if cand.is_file():
            return cand
    # 目录结构对不上时按文件名兜底找一层
    for cand in source_dir.rglob(Path(stem).name):
        if cand.is_file():
            return cand
    return None


def _rel(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def analyze_source(source_dir: Path) -> dict:
    """
    探测源码目录的入口与章节划分，返回 task.json 里的结构字段。
    """
    entry = _find_entry(source_dir)
    if entry is None:
        raise TaskError("no_entry", "源码包里没有找到 .tex 文件，无法定位入口")

    entry_dir = entry.parent
    info = scan_tex(entry) or {}
    section_files: List[str] = []
    appendix_files: List[str] = []
    seen = set()
    for name in info.get("inputs", []):
        target = _resolve_input(source_dir, entry_dir, name)
        if target is None or target == entry:
            continue
        rel = _rel(target, source_dir)
        if rel in seen:
            continue
        seen.add(rel)
        (appendix_files if _APPENDIX_NAME_RE.search(rel) else section_files
         ).append(rel)

    return {
        "entry_tex": _rel(entry, source_dir),
        "entry_tex_abs": str(entry),
        "entry_has_appendix_line": info.get("appendix_line"),
        "section_files": section_files,
        "appendix_files": appendix_files,
        "sections_in_entry": info.get("sections", []),
        "single_file": not section_files,
    }


# ----------------------------------------------------------------------
# 工具：命名 / 目标路径
# ----------------------------------------------------------------------
def _base_name(pid: int, title: str) -> str:
    return f"{int(pid):04d}-{ima_store.safe_name(title)}"


def _primary_tag(paper: dict) -> str:
    """与 arxiv_assets._primary_tag 一致：主标签 → 领域 → 未分类。"""
    tags = ima_store._parse_tags(paper.get("tags"))
    if tags:
        return tags[0]
    return (paper.get("field_name") or "").strip() or arxiv_assets.DEFAULT_TAG


def _target_parts(paper: dict, base: str) -> List[str]:
    """
    中文版 PDF 在 IMA 里的落点。

    原文 PDF 已经入库时直接复用它的文件夹，保证中英两版挨在一起；
    否则按 arxiv_assets 的同一套规则推导。
    """
    pdf_path = (paper.get("ima_pdf_path") or "").strip().strip("/")
    if pdf_path:
        parts = [p for p in pdf_path.split("/") if p]
        if len(parts) >= 2:
            return parts[:-1]
    return [ima_store.ROOT_FOLDER,
            ima_store.safe_name(_primary_tag(paper), limit=40), base]


def _instructions(task: dict) -> str:
    """给 agent 的中文任务说明（引用 SKILL.md，不重复发明流程）。"""
    entry = task["entry_tex"]
    entry_stem = Path(entry).stem
    src = task["source_dir"]
    return f"""你是一个论文中译 worker。请严格按 {SKILL_REL}（绝对路径：
{ROOT_DIR / SKILL_REL}）的流程，把下面这篇英文 LaTeX 论文翻译成中文并编译出 PDF。

【论文】{task['title']}（arXiv:{task['arxiv_id']}，pid={task['pid']}）

【结构】（已探测好，无需重新摸一遍）
- 源码目录：{src}
- 入口文件：{entry}
- 章节文件（相对源码目录）：{', '.join(task['section_files']) or '（单文件论文，无独立章节文件）'}
- 附录文件（**跳过不译**）：{', '.join(task['appendix_files']) or '（无独立附录文件）'}
{f"- 入口文件第 {task['entry_has_appendix_line']} 行是 \\appendix，其后内容不译" if task['entry_has_appendix_line'] else ""}

【执行步骤】
1. 注入中文化：在入口文件 \\documentclass 之后立即插入（用 Edit 工具，不要 sed/awk）：
     \\usepackage{{xeCJK}}
     \\setCJKmainfont[BoldFont=Heiti SC,ItalicFont=Kaiti SC]{{Songti SC}}
     \\setCJKsansfont{{Heiti SC}}
     \\setCJKmonofont{{Kaiti SC}}
   Linux 无 Songti SC 时换 Noto Serif CJK SC。
   axessibility 与 xelatex 冲突时用 \\IfFileExists 条件包装（见 SKILL.md 的失败处理表）。
2. 按章节文件分批翻译：保留 \\cite{{}} \\label{{}} \\ref{{}} \\input{{}} 命令、数学公式、
   变量名与模型名（DiT / CLIP 等）；翻译 \\section / \\subsection 标题、\\caption{{}}、
   正文段落；不译 \\author / \\date / \\maketitle 与首页 teaser 图注；跳过附录。
   >30KB 的文件分块读（每次 ≤300 行），不要一次性 read 进上下文。
3. 编译（**不要用 latexmk**，它在 xelatex 下跑 bibtex 有 bug）：
     cd "{src}"
     xelatex -interaction=nonstopmode {entry}
     bibtex {entry_stem}
     xelatex -interaction=nonstopmode {entry}
     xelatex -interaction=nonstopmode {entry}
4. 复制到目标路径并验证：
     cp "{Path(src) / (entry_stem + '.pdf')}" "{task['output_pdf']}"
   - 大小校验：产物 > 1MB（< 1MB 几乎肯定是 LaTeX 报错中断，查 main.log 的 `! Error`）
   - 中文校验：读 PDF 第 1-3 页，确认摘要 + 章节标题 + 一段正文已为中文
   - 保留校验：模型名、引用键 [18, 38]、URL / 邮箱保持原样
5. 回填（这一步由服务端完成上传与写库，你只管调接口）：
     curl -sS -X POST "http://127.0.0.1:$PORT/api/translate/tasks/{task['task_id']}/complete" \\
       -H "Content-Type: application/json" \\
       -H "X-Admin-Password: $ADMIN_PASSWORD" \\
       -d '{{"zh_pdf_path": "{task['output_pdf']}"}}'
   失败就调 /fail：
     curl -sS -X POST "http://127.0.0.1:$PORT/api/translate/tasks/{task['task_id']}/fail" \\
       -H "Content-Type: application/json" -H "X-Admin-Password: $ADMIN_PASSWORD" \\
       -d '{{"reason": "失败原因"}}'
   注意：这两个都是写接口，服务端开了 ADMIN_PASSWORD 时必须带 X-Admin-Password 头。

任务状态文件：{task['task_json']}（本说明的机器可读版本）。
"""


# ----------------------------------------------------------------------
# 建任务
# ----------------------------------------------------------------------
def _restore_source_from_ima(paper: dict, source_dir: Path) -> Tuple[bool, str]:
    """
    如果论文的 ima_tex_path 存在，把 IMA 里的 tex_source 目录树下载回本地。

    返回 (success, reason)。成功时 source_dir 下已有与 IMA 一致的目录结构。
    失败时返回 False 和中文原因，调用方应回退到 arXiv 下载。
    """
    tex_path = (paper.get("ima_tex_path") or "").strip().strip("/")
    if not tex_path:
        return False, "论文没有记录 ima_tex_path"

    try:
        client = ima_store._client()
        kb = client.knowledge_base_id
        folder_id = client.ensure_path(kb, tex_path.split("/"))
    except Exception as exc:
        return False, f"定位 IMA 源码目录失败：{type(exc).__name__}: {exc}"

    downloaded, skipped = 0, 0

    def _download_recursive(fid: str, local: Path) -> None:
        nonlocal downloaded, skipped
        for item in client.list_folder(kb, fid):
            name = (item.get("title") or item.get("name") or "").strip()
            if not name or client.is_deleted(name):
                continue
            mt = item.get("media_type")
            if mt == MT_FOLDER:
                child_local = local / name
                child_local.mkdir(parents=True, exist_ok=True)
                child_id = item.get("media_id") or item.get("folder_id")
                if child_id:
                    _download_recursive(child_id, child_local)
            else:
                ext = Path(name).suffix.lower()
                if ext in UNSUPPORTED_EXTS:
                    skipped += 1
                    continue
                try:
                    client.download_to(item["media_id"], local / name)
                    downloaded += 1
                except Exception as exc:
                    logger.warning("从 IMA 下载源码文件 %s 失败: %s", name, exc)
                    skipped += 1

    try:
        _download_recursive(folder_id, source_dir)
    except Exception as exc:
        return False, f"下载 IMA 源码树失败：{type(exc).__name__}: {exc}"

    if not any(source_dir.rglob("*.tex")):
        return False, f"IMA 源码目录下载完成但无 .tex 文件（下载 {downloaded} 个，跳过 {skipped} 个）"
    return True, f"从 IMA 复用源码：下载 {downloaded} 个文件，跳过 {skipped} 个"


def _source_is_corrupt(source_dir: Path) -> bool:
    """
    本地源码是否被上一轮自动翻译污染。

    推理型模型（MiniMax-M3 / DeepSeek-R1 等）偶尔会把 <think> 思维链吐进
    译文里；一旦写回 .tex，本地 source 就成了"带垃圾的版本"，后续若复用会
    继续带着垃圾跑。这里做一个廉价扫描，命中就要求重新取源。
    """
    for p in source_dir.rglob("*.tex"):
        try:
            head = p.read_text(encoding="utf-8", errors="ignore")[:400000]
        except OSError:
            continue
        if "<think>" in head:
            return True
    return False


def create_task(pid: int, force_fresh: bool = False) -> dict:
    """
    为一篇论文建中译任务。

    返回 task.json 的内容（同时落盘）。失败抛 TaskError，reason 可直接展示。

    取源优先级：
      1. 本地 source_dir 已存在 .tex 且 force_fresh=False → 复用（agent 模式）
      2. 论文 ima_tex_path 存在 → 从 IMA 下载源码（避免重复请求 arXiv）
      3. 否则 → 从 arXiv（含镜像回退）重新下载

    force_fresh=True 时跳过本地复用，但仍优先从 IMA 取，没有才回退 arXiv。
    """
    try:
        paper = ima_store.papers.get(int(pid))
    except Exception as exc:
        raise TaskError("upstream", f"读取论文失败：{type(exc).__name__}: {exc}")
    if not paper:
        raise TaskError("not_found", f"论文 {pid} 不存在")

    arxiv_id = arxiv_assets._normalize(paper.get("arxiv_id") or "")
    if not arxiv_id:
        raise TaskError("no_arxiv_id",
                        "该论文没有 arXiv ID，无法从 arXiv 取 LaTeX 源码")

    title = paper.get("title") or ""
    base = _base_name(pid, title)
    task_dir = TASKS_ROOT / str(int(pid))
    source_dir = task_dir / "source"

    with _LOCK:
        _ensure_root()
        task_dir.mkdir(parents=True, exist_ok=True)

        # 取源：本地复用 > IMA 复用 > arXiv 下载
        reused = (not force_fresh and source_dir.is_dir()
                  and any(source_dir.rglob("*.tex"))
                  and not _source_is_corrupt(source_dir))
        source_from_ima = False
        ima_reason = ""
        if not reused:
            if source_dir.exists():
                shutil.rmtree(source_dir, ignore_errors=True)
            source_dir.mkdir(parents=True, exist_ok=True)

            # 优先从 IMA 知识库复用已上传的源码（避免重复请求 arXiv）
            source_from_ima, ima_reason = _restore_source_from_ima(
                paper, source_dir)

            if not source_from_ima:
                src, why = arxiv_assets._fetch_source(arxiv_id)
                if src is None:
                    raise TaskError("no_source",
                                    (why or "arXiv 上未找到 LaTeX 源码，可能作者只提交了 PDF")
                                    + (f"；IMA 复用也未成功：{ima_reason}" if ima_reason else ""))
                try:
                    shutil.copytree(src, source_dir, dirs_exist_ok=True)
                finally:
                    # fetch_source 返回的是临时目录下的 src/，父目录即临时目录
                    shutil.rmtree(src.parent, ignore_errors=True)

        structure = analyze_source(source_dir)
        tasks = _load_state()
        task_id = _new_task_id(int(pid), tasks)
        output_pdf = task_dir / f"{base}_zh.pdf"

        task: Dict[str, Any] = {
            "task_id": task_id,
            "pid": int(pid),
            "arxiv_id": arxiv_id,
            "title": title,
            "status": "pending",
            "source_dir": str(source_dir),
            "source_reused": reused,
            "source_from_ima": source_from_ima,
            "source_origin": ("ima" if source_from_ima
                              else ("local" if reused else "arxiv")),
            "entry_tex": structure["entry_tex"],
            "entry_tex_abs": structure["entry_tex_abs"],
            "entry_has_appendix_line": structure["entry_has_appendix_line"],
            "section_files": structure["section_files"],
            "appendix_files": structure["appendix_files"],
            "sections_in_entry": structure["sections_in_entry"],
            "single_file": structure["single_file"],
            "output_pdf": str(output_pdf),
            "compiled_pdf": str(
                Path(structure["entry_tex_abs"]).with_suffix(".pdf")),
            "ima_target": {
                "folder": "/".join(_target_parts(paper, base)),
                "file_name": f"{base}_zh.pdf",
                "front_matter_key": "ima_zh_pdf_path",
            },
            "skill": SKILL_REL,
            "skill_abs": str(ROOT_DIR / SKILL_REL),
            "task_dir": str(task_dir),
            "task_json": str(task_dir / "task.json"),
            "created_at": _now(),
            "updated_at": _now(),
        }
        task["instructions"] = _instructions(task)
        (task_dir / "task.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")

        tasks[task_id] = {
            "task_id": task_id, "pid": int(pid), "arxiv_id": arxiv_id,
            "title": title, "status": "pending", "task_dir": str(task_dir),
            "entry_tex": task["entry_tex"], "output_pdf": str(output_pdf),
            "ima_zh_pdf_path": "", "reason": "",
            "created_at": task["created_at"], "updated_at": task["created_at"],
        }
        _save_state(tasks)
        return task


def _new_task_id(pid: int, tasks: Dict[str, dict]) -> str:
    stamp = time.strftime("%Y%m%d%H%M%S")
    task_id = f"zh{int(pid):04d}-{stamp}"
    n = 1
    while task_id in tasks:
        n += 1
        task_id = f"zh{int(pid):04d}-{stamp}-{n}"
    return task_id


# ----------------------------------------------------------------------
# 回填
# ----------------------------------------------------------------------
def _resolve_pdf(task: dict, zh_pdf_path: str) -> Optional[Path]:
    """接受绝对路径，也接受相对任务目录 / 源码目录的路径。"""
    raw = str(zh_pdf_path or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    cands = [p]
    if not p.is_absolute():
        cands.append(Path(task.get("task_dir") or ".") / p)
        cands.append(Path(task.get("source_dir") or ".") / p)
    for c in cands:
        if c.is_file():
            return c.resolve()
    return None


def _upload_zh_pdf(paper: dict, pdf: Path) -> str:
    """
    把中文版 PDF 传到 IMA。

    落点与原文 PDF 同一个文件夹（没有原文时按 arxiv_assets 的规则推导），
    命名 {id:04d}-{标题}_zh.pdf；返回 IMA 里的完整路径。
    """
    base = _base_name(paper.get("id") or 0, paper.get("title") or "")
    parts = _target_parts(paper, base)
    client = ima_store._client()
    kb = client.knowledge_base_id
    folder_id = client.ensure_path(kb, parts)
    name = f"{base}_zh.pdf"
    # IMA 没有原地更新接口：重传会顶成带时间戳的副本，所以先归档同名旧件
    for item in client.list_folder(kb, folder_id):
        if item.get("media_type") == ima_store.MT_FOLDER:
            continue
        if (item.get("title") or "") == name:
            try:
                client.rename(kb, item["media_id"],
                              f"{ima_store.ARCHIVE_PREFIX}"
                              f"{time.strftime('%Y%m%d%H%M%S')}_{name}")
            except Exception as exc:
                logger.warning("归档旧的中文版 PDF 失败（继续上传）: %s", exc)
            break
    client.upload_file(kb, pdf, folder_id=folder_id, upload_name=name)
    return "/".join(parts + [name])


def _writeback_zh(pid: int, zh_path: str) -> None:
    """
    把 ima_zh_pdf_path 写回论文的 front-matter。

    PaperRepo.update() 的字段白名单里没有 ima_zh_pdf_path（ima_store 不可改动），
    所以这里沿用 arxiv_assets._writeback 的同一套「归档旧版 + 传同名新版」链路，
    只多塞一个字段，并借用 papers 单例的锁，保证与 PaperRepo.update() 互斥。
    """
    repo = ima_store.papers
    with repo._lock:
        target: Optional[dict] = None
        for rec in repo._all():
            if rec.get("id") == pid:
                target = dict(rec)
                break
        if target is None:
            return
        target["ima_zh_pdf_path"] = zh_path
        target["updated_at"] = ima_store._fmt_dt(datetime.now())

        client = ima_store._client()
        kb = client.knowledge_base_id
        meta_id, _ = ima_store._folders(client, kb, create=True)
        old_item = ({"media_id": target.get("media_id"),
                     "title": target.get("file_name")}
                    if target.get("media_id") else None)
        file_name = f"{ima_store._prefix(pid, target.get('title') or '')}.md"
        res = ima_store._replace_file(client, kb, meta_id, old_item,
                                      file_name, repo._build_md(target))
        target["media_id"] = res.get("media_id") or target.get("media_id") or ""
        target["file_name"] = res.get("file_name") or file_name
        ima_store._cache_put(repo.kind, target)


def _update_task(task_id: str, **fields) -> dict:
    """更新状态索引，并把最新状态同步回 task.json（agent 端可读）。"""
    with _LOCK:
        tasks = _load_state()
        rec = tasks.get(task_id)
        if rec is None:
            raise TaskError("not_found", f"任务 {task_id} 不存在")
        rec.update(fields)
        rec["updated_at"] = _now()
        _save_state(tasks)

        task_json = Path(rec.get("task_dir") or "") / "task.json"
        if task_json.exists():
            try:
                data = json.loads(task_json.read_text(encoding="utf-8"))
                data.update(fields)
                data["updated_at"] = rec["updated_at"]
                task_json.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except (OSError, ValueError) as exc:
                logger.warning("回写 task.json 失败: %s", exc)
        return rec


def complete_task(task_id: str, zh_pdf_path: str) -> dict:
    """
    agent 翻译 + 编译完成后回填：校验产物 → 传 IMA → 写回 front-matter。
    """
    with _LOCK:
        tasks = _load_state()
        task = tasks.get(task_id)
        if task is None:
            raise TaskError("not_found", f"任务 {task_id} 不存在")

        pdf = _resolve_pdf(task, zh_pdf_path)
        if pdf is None:
            raise TaskError("no_pdf", f"找不到中文版 PDF：{zh_pdf_path}")
        if not pdf.read_bytes()[:4].startswith(PDF_MAGIC):
            raise TaskError("bad_pdf", f"{pdf} 不是合法的 PDF 文件")
        if pdf.stat().st_size < MIN_PDF_BYTES:
            raise TaskError(
                "bad_pdf",
                f"中文版 PDF 只有 {pdf.stat().st_size // 1024}KB，"
                "几乎肯定是 xelatex 编译中断（请查 main.log 里的 `! Error`）")

        paper = ima_store.papers.get(task["pid"])
        if not paper:
            raise TaskError("not_found", f"论文 {task['pid']} 不存在")
        try:
            ima_path = _upload_zh_pdf(paper, pdf)
        except Exception as exc:
            raise TaskError(
                "upload", f"中文版 PDF 上传到 IMA 失败：{type(exc).__name__}: {exc}")

        try:
            _writeback_zh(task["pid"], ima_path)
        except Exception as exc:
            raise TaskError(
                "writeback",
                f"中文版已传进 IMA（{ima_path}），但路径回写失败："
                f"{type(exc).__name__}: {exc}")

        return _update_task(task_id, status="done", ima_zh_pdf_path=ima_path,
                            reason="")


def fail_task(task_id: str, reason: str = "") -> dict:
    """标记任务失败。"""
    return _update_task(task_id, status="failed",
                        reason=(reason or "未说明原因")[:500])


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
def list_tasks(status: str = "") -> List[dict]:
    """列出任务（默认按创建时间倒序），status 可过滤 pending / done / failed。"""
    tasks = list(_load_state().values())
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    tasks.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return tasks


def get_task(task_id: str) -> Optional[dict]:
    return _load_state().get(task_id)


def ima_paths() -> Dict[int, dict]:
    """
    各论文在 IMA 里的资产路径，供前端卡片展示。

    这三个字段不在 PaperRepo._out() 的输出里（ima_store 不可改动），
    所以单独从这里取一次，前端按 pid 合并。
    """
    out: Dict[int, dict] = {}
    for rec in ima_store.papers._all():
        pid = rec.get("id")
        if pid is None:
            continue
        out[int(pid)] = {
            "ima_pdf_path": rec.get("ima_pdf_path") or "",
            "ima_tex_path": rec.get("ima_tex_path") or "",
            "ima_zh_pdf_path": rec.get("ima_zh_pdf_path") or "",
        }
    return out
