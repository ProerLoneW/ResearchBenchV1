"""
论文中译自动执行器：直接调用用户配置的 OpenAI-Compatible 大模型，
后台线程完成「注入中文字体 → 逐文件分块翻译 → xelatex 编译 →
上传 IMA + 回写路径」全流程，期间把每一步进度写进任务状态，
前端任务窗口轮询展示。

与 translate_task.py 的分工：
  - translate_task 只做"任务机制"（建任务 / 回填 / 状态索引）；
  - 本模块是"任务机制"的第一个自动化 worker：不依赖外部 agent，
    用设置页里配置的大模型 API 自己翻。agent 手动模式仍然可用
    （task.json 照常落盘，agent 可按 SKILL.md 流程接管）。

配置来源：设置页「AI / API 配置」（SQLite ApiConfig，Key 加密存储）。
未配置时 llm_ready() 返回 False，路由层直接 400，前端按钮置灰。

安全边界：只在 data/translate/{pid}/source/ 里改文件；产物校验沿用
complete_task() 的 PDF 魔数 + ≥512KB 检查，不达标的编译结果拒绝入库。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from ..config import decrypt_secret
from ..db import SessionLocal
from . import translate_task
from .translate_task import TaskError, _update_task, fail_task, get_task

logger = logging.getLogger(__name__)

# 运行中的任务（task_id -> Thread），防止同一任务被启动两次
_RUNNING: Dict[str, threading.Thread] = {}
_RUNNING_LOCK = threading.Lock()

# 分块翻译参数：块太小请求数暴涨、太大会超 max_tokens / 上下文
CHUNK_TARGET_CHARS = 4000
CHUNK_MAX_CHARS = 7000
LLM_TIMEOUT_SECONDS = 300
LLM_MAX_TOKENS = 8000
LLM_RETRIES = 2

# 编译产物里出现这些就说明字体/宏包出问题的常见信号（仅用于日志）
XELATEX_CANDIDATES = (
    "xelatex",
    "/Library/TeX/texbin/xelatex",
    "/usr/local/texlive/bin/x86_64-linux/xelatex",
    "/usr/local/texlive/bin/aarch64-linux/xelatex",
)
BIBTEX_CANDIDATES = ("bibtex", "/Library/TeX/texbin/bibtex")

SYSTEM_PROMPT = """你是一个专业的学术论文 LaTeX 翻译引擎。请把用户给出的英文 LaTeX 片段翻译成中文。

严格规则：
1. 原样保留（一个字符都不能改）：
   - 所有 LaTeX 命令与环境名（\\section、\\cite、\\ref、\\label、\\input、
     \\usepackage、\\begin{...}/\\end{...} 的命令部分、可选参数 [] 等）；
   - 数学公式内容（$...$、\\[...\\]、equation/align 环境内部）；
   - 引用键、URL、邮箱、文件名、代码、变量名与模型名（如 DiT、CLIP、VLA、BERT）；
   - % 注释行原样保留，不翻译注释。
2. 需要翻译成中文的：
   - 正文段落；
   - \\section / \\subsection / \\subsubsection / \\paragraph 的标题文字；
   - \\caption{...} 里的自然语言；
   - \\textbf / \\emph 等包裹的自然语言。
   - 摘要（abstract 环境）正文。
   不要翻译：\\author / \\date / \\affiliation / \\thanks 里的人名、机构、邮箱
   （原样保留）；\\input / \\include 命令本身。
3. 输出只包含翻译后的 LaTeX 片段本身：不要解释、不要代码块围栏（```）、
   不要加"以下是翻译"之类的前后缀。保持片段原有的行结构。
4. 中文与英文/数字之间加一个空格；专业术语可在中文后用括号保留英文缩写。
5. 【最重要】只输出翻译后的 LaTeX 片段本身：
   - 禁止输出推理过程、分析、说明、注释或任何与原文无关的文字；
   - 禁止输出"以下是翻译""翻译结果如下""注意""说明"之类的前后缀；
   - 禁止用代码块围栏（```）包裹；
   - 不要复述或引用上面这些规则。"""


# ===========================================================================
# LLM 配置与调用
# ===========================================================================
def llm_ready() -> Tuple[bool, str]:
    """检查设置页是否配置了可用的大模型 API。返回 (ready, 提示)。"""
    db = SessionLocal()
    try:
        from .ai import get_api_config
        cfg = get_api_config(db)
        if not (cfg.base_url or "").strip():
            return False, "尚未配置大模型：请在「设置」页填写 AI / API 配置（Base URL）"
        if not (cfg.model_name or "").strip():
            return False, "尚未配置大模型：请在「设置」页填写模型名称（Model）"
        if not cfg.api_key:
            return False, "尚未配置大模型：请在「设置」页填写 API Key"
        return True, ""
    finally:
        db.close()


def _load_llm_config() -> dict:
    """读取一次配置（一次任务只读一次）。"""
    db = SessionLocal()
    try:
        from .ai import get_api_config
        cfg = get_api_config(db)
        base = (cfg.base_url or "").rstrip("/")
        if base.endswith("/chat/completions"):
            endpoint = base
        else:
            endpoint = base + "/chat/completions"
        extra: dict = {}
        try:
            extra = json.loads(cfg.other_params or "{}")
        except Exception:
            extra = {}
        return {
            "endpoint": endpoint,
            "model": cfg.model_name,
            "api_key": decrypt_secret(cfg.api_key) or "",
            "extra": extra if isinstance(extra, dict) else {},
        }
    finally:
        db.close()


def _chat_sync(conf: dict, messages: List[dict], max_tokens: int) -> str:
    """同步调用 OpenAI-Compatible chat completions（后台线程里用）。"""
    payload = {
        "model": conf["model"],
        "messages": messages,
        "temperature": 0.15,
        "max_tokens": max_tokens,
    }
    payload.update(conf.get("extra") or {})
    last_err: Optional[Exception] = None
    for attempt in range(LLM_RETRIES + 1):
        try:
            with httpx.Client(timeout=LLM_TIMEOUT_SECONDS) as client:
                resp = client.post(
                    conf["endpoint"],
                    headers={
                        "Authorization": f"Bearer {conf['api_key']}",
                        "Content-Type": "application/json",
                    },
                    json=payload)
                resp.raise_for_status()
                data = resp.json()
            content = (data.get("choices") or [{}])[0] \
                .get("message", {}).get("content", "")
            return _clean_model_output(content)
        except Exception as exc:
            last_err = exc
            if attempt < LLM_RETRIES:
                time.sleep(2 * (attempt + 1))     # 指数退避
    raise RuntimeError(f"大模型调用失败：{type(last_err).__name__}: {last_err}")


def _strip_fences(text: str) -> str:
    """模型偶尔会用 ```latex 围栏包裹输出，剥掉围栏。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    return t.strip()


# 推理型模型（MiniMax-M3 / DeepSeek-R1 等）会把思维链包在 <think> 里吐出来，
# 若原样写回 .tex 会直接让 xelatex 报错中断，必须剥掉。
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _clean_model_output(text: str) -> str:
    """剥掉代码块围栏与思维链，只留译文。"""
    t = _strip_fences(text)
    t = _THINK_RE.sub("", t)
    if "<think>" in t:            # 输出被截断、think 未闭合
        t = t.split("<think>")[0]
    return t.strip()


# 明显是"模型在说废话"而不是译文的特征词
_CHATTER = (
    "以下是翻译", "翻译结果如下", "以下是译文", "希望这能帮", "如果你需要",
    "作为AI", "作为人工智能", "我无法", "我不能", "请注意", "说明：",
    "规则：", "原文保留", "Let me", "I will", "Here is", "翻译说明",
)


def _validate_chunk(src: str, out: str) -> Optional[str]:
    """
    校验一块译文是否可用。返回 None 表示合格，否则返回拒绝原因。

    只做"能挡住明显事故"的粗校验：空输出、长度异常、环境数量失衡、
    以及模型把说明/推理当译文吐出来。宁可保守降级成保留原文，
    也不能让脏输出混进 .tex 导致 xelatex 中断。
    """
    if not out.strip():
        return "空输出"
    for word in _CHATTER:
        if word in out:
            return f"疑似输出了说明性文字（含“{word}”）"
    n = len(src)
    if len(out) < 0.2 * n:
        return f"译文过短（{len(out)}/{n} 字符），疑似截断"
    if len(out) > 3.0 * n:
        return f"译文过长（{len(out)}/{n} 字符）"
    sb, ob = src.count("\\begin{"), out.count("\\begin{")
    se, oe = src.count("\\end{"), out.count("\\end{")
    if ob > sb + 1 or ob < sb - 1:
        return f"环境开始标记数量不符（源 {sb} / 译文 {ob}）"
    if oe > se + 1 or oe < se - 1:
        return f"环境结束标记数量不符（源 {se} / 译文 {oe}）"
    return None


# ===========================================================================
# 分块
# ===========================================================================
def _split_chunks(text: str) -> List[str]:
    """
    按空行（段落）聚合分块，目标 ~4k 字符、硬上限 7k。
    单个超长段落再按行硬切。
    """
    blocks: List[str] = []
    for para in re.split(r"\n\s*\n", text):
        if not para.strip():
            continue
        if len(para) > CHUNK_MAX_CHARS:
            lines, buf = para.split("\n"), ""
            for ln in lines:
                # 单行本身超长（如压缩成一行的表格/长公式）：按字符硬切
                while len(ln) > CHUNK_MAX_CHARS:
                    if buf:
                        blocks.append(buf)
                        buf = ""
                    blocks.append(ln[:CHUNK_MAX_CHARS])
                    ln = ln[CHUNK_MAX_CHARS:]
                if len(buf) + len(ln) + 1 > CHUNK_MAX_CHARS:
                    if buf:
                        blocks.append(buf)
                    buf = ln
                else:
                    buf = (buf + "\n" + ln) if buf else ln
            if buf:
                blocks.append(buf)
        else:
            blocks.append(para)

    chunks: List[str] = []
    buf = ""
    for b in blocks:
        if buf and len(buf) + len(b) + 2 > CHUNK_TARGET_CHARS:
            chunks.append(buf)
            buf = b
        else:
            buf = (buf + "\n\n" + b) if buf else b
    if buf:
        chunks.append(buf)
    return chunks


# ===========================================================================
# 进度步骤
# ===========================================================================
def _steps_for(task: dict) -> List[dict]:
    """根据任务结构生成步骤列表（翻译步骤逐文件展开，与 run_task 的目标一致）。"""
    steps = [{"key": "prep", "label": "注入中文字体支持（xeCJK）",
              "status": "wait", "detail": ""}]
    appendix_set = set(task.get("appendix_files") or [])
    files = [f for f in (task.get("section_files") or [])
             if f not in appendix_set]
    if not files:
        files = [task.get("entry_tex")]
    elif task.get("entry_tex") not in files:
        files.append(task.get("entry_tex"))
    for i, f in enumerate(files, 1):
        steps.append({"key": f"file:{f}", "label": f"翻译 {f}（{i}/{len(files)}）",
                      "status": "wait", "detail": ""})
    steps.append({"key": "compile", "label": "编译中文 PDF（xelatex）",
                  "status": "wait", "detail": ""})
    steps.append({"key": "upload", "label": "上传 IMA 知识库并回写路径",
                  "status": "wait", "detail": ""})
    return steps


def _put_steps(task_id: str, steps: List[dict], progress: int) -> None:
    try:
        _update_task(task_id, steps=steps, progress=int(progress))
    except Exception as exc:
        logger.warning("更新任务进度失败: %s", exc)


def _step(steps: List[dict], key: str, status: str, detail: str = "") -> None:
    for s in steps:
        if s["key"] == key:
            s["status"] = status
            if detail:
                s["detail"] = detail
            return


def _progress_pct(steps: List[dict]) -> int:
    """按步骤完成度估算百分比（prep/文件各占大头）。"""
    total = max(1, len(steps))
    done = sum(1 for s in steps if s["status"] == "ok")
    running = sum(1 for s in steps if s["status"] == "running")
    return min(99, int((done + 0.5 * running) / total * 100))


# ===========================================================================
# 翻译执行
# ===========================================================================
_XECJK_BLOCK = """\\usepackage{xeCJK}
\\setCJKmainfont[BoldFont=Heiti SC,ItalicFont=Kaiti SC]{Songti SC}
\\setCJKsansfont{Heiti SC}
\\setCJKmonofont{Kaiti SC}
% ---- ResearchBench auto-translation ----"""


def _inject_xecjk(entry_path: Path) -> bool:
    """在 \\documentclass 之后插入 xeCJK 中文字体设置。已注入过则跳过。"""
    text = entry_path.read_text(encoding="utf-8", errors="ignore")
    if "xeCJK" in text:
        return False
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if re.search(r"\\documentclass", ln):
            lines.insert(i + 1, _XECJK_BLOCK)
            entry_path.write_text("\n".join(lines), encoding="utf-8")
            return True
    # 没有 documentclass（理论上入口探测不会选到这种文件）：追加到开头
    lines.insert(0, _XECJK_BLOCK)
    entry_path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _translate_file(path: Path, key: str, conf: dict, steps: List[dict],
                    task_id: str) -> None:
    """
    翻译单个 .tex 文件并原位写回。

    - 章节文件：全文翻译；
    - 入口文件：只翻译 \\begin{document} 之后、\\appendix（若有）之前的部分，
      preamble（宏包/标题定义）与附录原样保留。
    - 纯公式/表格为主的小块也照发给模型（prompt 已声明公式原样返回）。
    """
    _step(steps, key, "running")
    _put_steps(task_id, steps, _progress_pct(steps))

    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.split("\n")

    begin_doc = appendix = None
    for i, ln in enumerate(lines):
        if begin_doc is None and re.search(r"\\begin\s*\{document\}", ln):
            begin_doc = i
        if appendix is None and re.search(r"\\appendix\b", ln):
            appendix = i
        if begin_doc is not None and appendix is not None:
            break

    if begin_doc is None:
        head, body, tail = [], lines, []       # 没有正文环境：整体当正文
    else:
        head = lines[:begin_doc + 1]
        if appendix is not None and appendix > begin_doc:
            body = lines[begin_doc + 1:appendix]
            tail = lines[appendix:]
        else:
            body = lines[begin_doc + 1:]
            tail = []

    body_text = "\n".join(body)
    chunks = _split_chunks(body_text) if body_text.strip() else []

    out_parts: List[str] = []
    degraded: List[str] = []
    for ci, chunk in enumerate(chunks, 1):
        _step(steps, key, "running", f"第 {ci}/{len(chunks)} 块")
        _put_steps(task_id, steps, _progress_pct(steps))
        translated, why = None, ""
        for attempt in range(LLM_RETRIES + 1):
            try:
                raw = _chat_sync(
                    conf,
                    [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": chunk}],
                    max_tokens=LLM_MAX_TOKENS)
            except Exception as exc:
                why = f"调用失败：{exc}"
                continue
            bad = _validate_chunk(chunk, raw)
            if bad is None:
                translated = raw
                break
            why = bad
        if translated is None:
            # 兜底：模型这块不可信时保留英文原文，宁可漏翻也不能毁掉编译
            translated = chunk
            degraded.append(f"第 {ci} 块保留原文（{why}）")
            logger.warning("%s 第 %s 块译文校验未通过，保留原文：%s",
                           path.name, ci, why)
        elif degraded:
            _step(steps, key, "running",
                  f"第 {ci}/{len(chunks)} 块（{len(degraded)} 块降级）")
        out_parts.append(translated)

    new_text = "\n".join(head)
    if out_parts:
        new_text += "\n" + "\n\n".join(out_parts)
    if tail:
        new_text += "\n" + "\n".join(tail)
    path.write_text(new_text, encoding="utf-8")

    detail = f"{len(chunks)} 块完成"
    if degraded:
        detail += f"（{len(degraded)} 块校验未通过已保留原文：" + \
                  "；".join(degraded[:3]) + ("…" if len(degraded) > 3 else "") + "）"
    _step(steps, key, "ok", detail)
    _put_steps(task_id, steps, _progress_pct(steps))


# ===========================================================================
# 编译
# ===========================================================================
def _find_bin(candidates: Tuple[str, ...]) -> Optional[str]:
    for cand in candidates:
        p = shutil.which(cand) if "/" not in cand else (
            cand if Path(cand).exists() else None)
        if p:
            return str(p)
    return None


def _run_tex(cmd: List[str], cwd: Path, timeout: int = 600) -> None:
    env = dict(__import__("os").environ)
    texbin = str(Path(cmd[0]).parent)
    if texbin and texbin != ".":
        env["PATH"] = texbin + ":" + env.get("PATH", "")
    subprocess.run(cmd, cwd=str(cwd), timeout=timeout, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env=env)


def _compile(task: dict, steps: List[dict], task_id: str) -> Path:
    """xelatex ×1 → bibtex（有 .bib 时）→ xelatex ×2。返回编译出的 PDF。"""
    _step(steps, "compile", "running", "第 1 遍 xelatex")
    _put_steps(task_id, steps, _progress_pct(steps))

    src = Path(task["source_dir"])
    entry = Path(task["entry_tex_abs"])
    xelatex = _find_bin(XELATEX_CANDIDATES)
    if not xelatex:
        raise RuntimeError("本机未找到 xelatex（请安装 MacTeX/TeX Live），无法编译中文 PDF")
    stem = entry.stem

    _run_tex([xelatex, "-interaction=nonstopmode", entry.name], src)

    has_bib = any(src.rglob("*.bib"))
    if has_bib:
        bibtex = _find_bin(BIBTEX_CANDIDATES)
        if bibtex:
            _step(steps, "compile", "running", "bibtex 参考文献")
            _put_steps(task_id, steps, _progress_pct(steps))
            _run_tex([bibtex, stem], src)

    for i in (2, 3):
        _step(steps, "compile", "running", f"第 {i} 遍 xelatex")
        _put_steps(task_id, steps, _progress_pct(steps))
        _run_tex([xelatex, "-interaction=nonstopmode", entry.name], src)

    pdf = entry.with_suffix(".pdf")
    if not pdf.exists():
        raise RuntimeError(
            "xelatex 编译未产出 PDF：请查看源码目录里的 .log 文件定位报错")
    _step(steps, "compile", "ok", f"{pdf.stat().st_size // 1024} KB")
    _put_steps(task_id, steps, _progress_pct(steps))
    return pdf


# ===========================================================================
# 任务入口（后台线程）
# ===========================================================================
def start_task(task_id: str) -> bool:
    """启动后台线程执行自动翻译。已在跑的任务返回 False。"""
    with _RUNNING_LOCK:
        if task_id in _RUNNING and _RUNNING[task_id].is_alive():
            return False
        t = threading.Thread(target=_worker, args=(task_id,),
                             name=f"llm-translate-{task_id}", daemon=True)
        _RUNNING[task_id] = t
    t.start()
    return True


def _worker(task_id: str) -> None:
    try:
        run_task(task_id)
    except Exception as exc:
        # run_task 内部已尽量自吞异常走到 fail_task；这里兜底
        logger.exception("自动翻译线程异常: %s", task_id)
        try:
            fail_task(task_id, f"内部错误：{type(exc).__name__}: {exc}")
        except Exception:
            pass


def _full_task(task_id: str) -> Optional[dict]:
    """
    取完整任务信息：task.json（结构字段）与状态索引（进度字段）合并。

    状态索引 _tasks.json 只存轻量字段（source_dir / entry_tex_abs /
    section_files / appendix_files 都不在里面），执行翻译必须用 task.json
    里的结构信息；status / steps / progress 等运行时字段以状态索引为准。
    """
    rec = get_task(task_id)
    if not rec:
        return None
    merged = dict(rec)
    task_json = Path(rec.get("task_dir") or "") / "task.json"
    if task_json.exists():
        try:
            data = json.loads(task_json.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = dict(data)
                # 运行时状态以索引为准（索引里的 status/mode/steps/progress 更新更及时）
                for k in ("status", "mode", "steps", "progress",
                          "ima_zh_pdf_path", "task_id", "pid",
                          "updated_at", "reason"):
                    if k in rec:
                        merged[k] = rec[k]
        except (OSError, ValueError) as exc:
            logger.warning("读取 task.json 失败（退回状态索引）: %s", exc)
    return merged


def run_task(task_id: str) -> None:
    """自动翻译全流程。任何一步失败都会把任务标记为 failed + 中文原因。"""
    task = _full_task(task_id)
    if not task:
        return

    steps = _steps_for(task)
    _update_task(task_id, mode="auto", status="running",
                 steps=steps, progress=0, reason="")
    try:
        # 1. 大模型配置（任务启动时读一次）
        ready, why = llm_ready()
        if not ready:
            raise RuntimeError(why)
        conf = _load_llm_config()

        src_dir = Path(task["source_dir"])

        # 2. 注入中文字体
        _step(steps, "prep", "running")
        _put_steps(task_id, steps, _progress_pct(steps))
        _inject_xecjk(Path(task["entry_tex_abs"]))
        _step(steps, "prep", "ok")
        _put_steps(task_id, steps, _progress_pct(steps))

        # 3. 逐文件翻译：章节文件在前，入口文件最后（入口正文含标题/摘要）
        section_files = list(task.get("section_files") or [])
        appendix_set = set(task.get("appendix_files") or [])
        targets = [f for f in section_files if f not in appendix_set]
        if not targets:
            targets = [task["entry_tex"]]
        elif task["entry_tex"] not in targets:
            targets.append(task["entry_tex"])
        for rel in targets:
            fpath = src_dir / rel
            if not fpath.exists():
                continue
            _translate_file(fpath, f"file:{rel}", conf, steps, task_id)

        # 4. 编译
        pdf = _compile(task, steps, task_id)

        # 5. 复制到目标位置 → complete_task（校验 + 传 IMA + 回写）
        _step(steps, "upload", "running", "上传 IMA（较慢，请稍候）")
        _put_steps(task_id, steps, _progress_pct(steps))
        out_pdf = Path(task["output_pdf"])
        shutil.copyfile(pdf, out_pdf)
        rec = translate_task.complete_task(task_id, str(out_pdf))

        _step(steps, "upload", "ok", rec.get("ima_zh_pdf_path") or "")
        _update_task(task_id, steps=steps, progress=100,
                     ima_zh_pdf_path=rec.get("ima_zh_pdf_path") or "")
    except TaskError as exc:
        _mark_failed(task_id, steps, exc.reason)
    except Exception as exc:
        _mark_failed(task_id, steps, f"{type(exc).__name__}: {exc}")


def _mark_failed(task_id: str, steps: List[dict], reason: str) -> None:
    """把所有还在 running 的步骤标成 error，再写失败状态。"""
    for s in steps:
        if s["status"] == "running":
            s["status"] = "error"
    try:
        _update_task(task_id, steps=steps, reason=(reason or "")[:500])
        fail_task(task_id, reason)
    except Exception:
        pass


def is_running_for_pid(pid: int) -> bool:
    """该论文是否已有自动翻译任务在跑（防重复启动）。"""
    for t in list(_load_state_tasks()):
        if (t.get("pid") == int(pid) and t.get("status") == "running"
                and t.get("mode") == "auto"):
            tid = t.get("task_id")
            with _RUNNING_LOCK:
                th = _RUNNING.get(tid)
            if th and th.is_alive():
                return True
    return False


def _load_state_tasks() -> List[dict]:
    try:
        return translate_task.list_tasks()
    except Exception:
        return []
