"""
arXiv 原文资产抓取：把论文的 PDF 与 LaTeX 源码搬进 IMA 知识库。

落盘结构（与 migrate_to_ima.py 保持一致）：

    ResearchBench-ima/
    └── {标签}/
        └── 0004-论文标题/
            ├── 0004-论文标题.pdf
            └── tex_source/          ← 解压后的整个源码目录树（保留相对结构）

编排入口是 ensure_paper_assets(pid)：取论文 -> 下载 -> 上传 -> 把
ima_pdf_path / ima_tex_path 回写到 metadata 里的 front-matter。
任何一步失败都收敛成可读的 reason，不向上抛裸异常。

两个已实测的外部约束：
  - arXiv 的 e-print 源码包格式不固定（tar.gz / tar.bz2 / tar / zip /
    单个 .tex 文本 / 单个 .tex.gz，甚至作者只提交了 PDF），所以必须
    **先探测真实类型再决定怎么解包**，不能看 Content-Type 就下结论。
  - IMA 上传并发超过 2 就会被限流（HTTP 403 / code 200001）。
"""
from __future__ import annotations

import bz2
import gzip
import httpx
import logging
import lzma
import shutil
import tarfile
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config
from . import ima_store
from .ima_client import FILE_TYPES, UNSUPPORTED_EXTS, IMAError

logger = logging.getLogger(__name__)

# 历史常量：早期只请求主站，国内外网经常 SSL 失败。现已由下面的
# _pdf_urls / _source_urls 按设置源做镜像回退，这两个仅作兜底参考保留。
ARXIV_PDF_URLS = (
    "https://arxiv.org/pdf/{aid}",
    "https://arxiv.org/pdf/{aid}.pdf",
)
ARXIV_EPRINT_URL = "https://arxiv.org/e-print/{aid}"

# 各镜像的内容根（与 API query base 不同）：sjtu/ustc 为国内直连，export 是 arXiv
# 官方推荐的脚本入口，限流更宽松。
ARXIV_SOURCE_ROOTS = {
    "sjtu":   "https://arxiv.sjtu.edu.cn",
    "ustc":   "https://mirrors.ustc.edu.cn/arxiv",
    "export": "https://export.arxiv.org",
    "arxiv":  "https://arxiv.org",
}

HTTP_TIMEOUT = 30        # 单次请求超时（秒）
HTTP_ATTEMPTS = 2        # 首次 + 重试 1 次
UPLOAD_WORKERS = 2       # IMA 实测 8 并发会 403，上传并发压到 2
UA = "ResearchBench/1.0 (arXiv asset fetcher; mailto:local-user)"

ROOT_FOLDER = ima_store.ROOT_FOLDER          # ResearchBench-ima
TEX_SUBFOLDER = "tex_source"
DEFAULT_TAG = "未分类"

# 解压后据此判断"这包里到底有没有 LaTeX 源码"
SOURCE_EXTS = {".tex", ".bib", ".bbl", ".sty", ".cls", ".bst", ".txt", ".md"}


class AssetError(RuntimeError):
    """抓取/解包失败，消息即为可直接展示的原因。"""


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
def _get(url: str, timeout: int = HTTP_TIMEOUT) -> httpx.Response:
    last: Optional[Exception] = None
    for attempt in range(HTTP_ATTEMPTS):
        try:
            # 不显式传 proxy：httpx 显式代理在 macOS + 该本地代理下会触发
            # [SSL] record layer failure；改走默认 trust_env，让 httpx 自己从
            # HTTP_PROXY/HTTPS_PROXY 环境变量读代理即可（config.save_radar_proxy
            # 已把代理同步进这两个环境变量）。
            with httpx.Client(timeout=timeout,
                              follow_redirects=True) as client:
                resp = client.get(url, headers={"User-Agent": UA})
            if resp.status_code < 400:
                return resp
            last = RuntimeError(f"HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            last = exc
        if attempt < HTTP_ATTEMPTS - 1:
            time.sleep(1.5 * (attempt + 1))
    raise AssetError(f"下载失败 {url}（{last}）")


def _head_content_type(url: str) -> str:
    """先探一下 Content-Type，只作提示用，判定仍以文件魔数为准。"""
    try:
        with httpx.Client(timeout=15,
                          follow_redirects=True) as client:
            resp = client.head(url, headers={"User-Agent": UA})
    except httpx.HTTPError:
        return ""
    return (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()


def _normalize(arxiv_id: str) -> str:
    aid = (arxiv_id or "").strip()
    aid = aid.replace("arXiv:", "").replace("arxiv:", "").strip()
    return aid.strip("/")


def _arxiv_source_order() -> list:
    """与 radar.py 一致的回退顺序：当前设置源 → 其它国内源 → export → 主站。"""
    order = [config.ARXIV_SOURCE]
    for k in config.ARXIV_SOURCES:
        if k != config.ARXIV_SOURCE and config.ARXIV_SOURCES[k][2]:
            order.append(k)
    for k in ("export", "arxiv"):
        if k not in order:
            order.append(k)
    return order


def _pdf_urls(aid: str) -> List[str]:
    out = []
    for k in _arxiv_source_order():
        root = ARXIV_SOURCE_ROOTS.get(k)
        if not root:
            continue
        out.append(f"{root}/pdf/{aid}")
        out.append(f"{root}/pdf/{aid}.pdf")
    return out


def _source_urls(aid: str) -> List[str]:
    out = []
    for k in _arxiv_source_order():
        root = ARXIV_SOURCE_ROOTS.get(k)
        if not root:
            continue
        # /e-print 是通用入口；主站会 301/302 到 /src，httpx 会跟随重定向
        out.append(f"{root}/e-print/{aid}")
        out.append(f"{root}/src/{aid}")
    return out


def _looks_like_html(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


# ----------------------------------------------------------------------
# 类型探测：魔数优先，tar/zip 用标准库再确认一次
# ----------------------------------------------------------------------
_MAGIC: Tuple[Tuple[bytes, str], ...] = (
    (b"%PDF", "pdf"),
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bz2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"!<arch>\n", "ar"),
)


def _is_text(head: bytes) -> bool:
    return b"\x00" not in head and _decodable(head)


def _decodable(head: bytes) -> bool:
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _sniff(path: Path) -> str:
    """
    判定源码包的真实类型。

    gzip/bz2/xz 只是压缩层，里面可能是 tar 也可能是单个 .tex，
    所以命中这些魔数后先让 tarfile 用 transparent compression 再看一眼。
    """
    try:
        head = path.open("rb").read(512)
    except OSError:
        return "unknown"
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            if kind in ("gzip", "bz2", "xz", "ar") and tarfile.is_tarfile(path):
                return "tar"
            return kind
    if tarfile.is_tarfile(path):      # 未压缩的裸 tar（老提交常见）
        return "tar"
    if zipfile.is_zipfile(path):
        return "zip"
    if _is_text(head):
        return "tex"
    return "unknown"


# ----------------------------------------------------------------------
# 解包
# ----------------------------------------------------------------------
def _extract_tar(blob: Path, out: Path) -> None:
    # "r:*" = transparent compression，gz/bz2/xz 自动识别
    with tarfile.open(blob, "r:*") as tf:
        tf.extractall(out, filter="data")   # 挡住 ../ 与绝对路径


def _extract_zip(blob: Path, out: Path) -> None:
    with zipfile.ZipFile(blob) as zf:
        for info in zf.infolist():
            parts = Path(info.filename).parts
            if info.filename.startswith("/") or ".." in parts:
                logger.warning("跳过可疑的压缩包成员: %s", info.filename)
                continue
            zf.extract(info, out)


def _decompress_single(blob: Path, kind: str) -> Optional[bytes]:
    """非 tar 的单文件压缩（例如 main.tex.gz）。"""
    raw = blob.read_bytes()
    try:
        if kind == "gzip":
            return gzip.decompress(raw)
        if kind == "bz2":
            return bz2.decompress(raw)
        if kind == "xz":
            return lzma.decompress(raw)
    except (OSError, EOFError, lzma.LZMAError) as exc:
        logger.warning("解压 %s 失败: %s", blob.name, exc)
    return None


def _has_source(files: List[Path]) -> bool:
    return any(p.suffix.lower() in SOURCE_EXTS for p in files)


# ----------------------------------------------------------------------
# 下载
# ----------------------------------------------------------------------
def fetch_pdf(arxiv_id: str) -> Path:
    """
    下载 PDF 到临时目录并返回文件路径。

    按当前设置源 → 其它国内镜像 → export → 主站顺序回退；
    不管哪个源，最后都必须以 %PDF 魔数校验通过才算成功。
    """
    aid = _normalize(arxiv_id)
    if not aid:
        raise AssetError("arxiv_id 为空")
    tmp = Path(tempfile.mkdtemp(prefix="arxiv_pdf_"))
    problems: List[str] = []
    for url in _pdf_urls(aid):
        try:
            data = _get(url).content
        except AssetError as exc:
            problems.append(str(exc))
            continue
        if data.startswith(b"%PDF"):
            dest = tmp / f"{aid.replace('/', '_')}.pdf"
            dest.write_bytes(data)
            return dest
        if _looks_like_html(data):
            problems.append(f"{url} 返回的是网页而不是 PDF")
        else:
            problems.append(
                f"{url} 返回的不是 PDF（首字节={data[:8]!r}）"
            )
    shutil.rmtree(tmp, ignore_errors=True)
    raise AssetError("PDF 下载失败：" + "；".join(problems))


def _fetch_source(arxiv_id: str) -> Tuple[Optional[Path], str]:
    """按镜像回退顺序下载并解包源码，返回 (目录, 原因)。"""
    aid = _normalize(arxiv_id)
    if not aid:
        return None, "arxiv_id 为空"

    problems: List[str] = []
    for url in _source_urls(aid):
        try:
            data = _get(url).content
        except AssetError as exc:
            problems.append(str(exc))
            continue
        if not data:
            problems.append(f"{url} 返回空")
            continue
        if _looks_like_html(data):
            problems.append(f"{url} 返回的是网页而不是源码包")
            continue
        if data.startswith(b"%PDF"):
            return None, "作者只提交了 PDF，arXiv 上没有 LaTeX 源码"

        tmp = Path(tempfile.mkdtemp(prefix="arxiv_src_"))
        blob = tmp / "_download"
        blob.write_bytes(data)
        kind = _sniff(blob)
        out = tmp / "src"
        out.mkdir(exist_ok=True)

        if kind == "tar":
            _extract_tar(blob, out)
        elif kind == "zip":
            _extract_zip(blob, out)
        elif kind == "tex":
            (out / "main.tex").write_bytes(data)
        elif kind in ("gzip", "bz2", "xz"):
            inner = _decompress_single(blob, kind)
            if inner is None:
                shutil.rmtree(tmp, ignore_errors=True)
                problems.append(f"{url} {kind} 解压失败")
                continue
            if inner.startswith(b"%PDF"):
                shutil.rmtree(tmp, ignore_errors=True)
                return None, "作者只提交了 PDF，arXiv 上没有 LaTeX 源码"
            if not _is_text(inner[:512]):
                shutil.rmtree(tmp, ignore_errors=True)
                problems.append(f"{url} 解压后不是文本")
                continue
            (out / "main.tex").write_bytes(inner)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            problems.append(f"{url} 无法识别的源码包格式（首字节={data[:8]!r}）")
            continue

        files = [p for p in out.rglob("*") if p.is_file()]
        if not files:
            shutil.rmtree(tmp, ignore_errors=True)
            problems.append(f"{url} 解压后为空")
            continue
        if not _has_source(files):
            exts = sorted({(p.suffix.lower() or "(无后缀)") for p in files})
            shutil.rmtree(tmp, ignore_errors=True)
            problems.append(f"{url} 里没有 LaTeX 源文件（只有：{', '.join(exts[:8])}）")
            continue
        return out, f"从 {url} 下载，源码包为 {kind} 格式，解压出 {len(files)} 个文件"

    return None, "源码下载失败：" + "；".join(problems)


def fetch_source(arxiv_id: str) -> Optional[Path]:
    """下载并解压 LaTeX 源码；无源码（例如作者只交了 PDF）时返回 None。"""
    return _fetch_source(arxiv_id)[0]


# ----------------------------------------------------------------------
# 上传到 IMA
# ----------------------------------------------------------------------
def _ima_allowed(path: Path) -> bool:
    """
    是否上传。FILE_TYPES 是 ima_client 里的权威清单，这里只做复用，
    不另维护一份后缀表；UNSUPPORTED_EXTS 是明确会被拒的类型。
    """
    ext = path.suffix.lower()
    return ext in FILE_TYPES and ext not in UNSUPPORTED_EXTS


def _upload_tree(local_dir: Path, kb: str, base_parts: List[str],
                 stats: Dict[str, int]) -> None:
    """
    把本地目录树上传成 IMA 的 tex_source（保留相对目录结构）。

    base_parts 是 tex_source 文件夹在 IMA 里的完整路径，例如
    ["ResearchBench-ima", "VLA", "0004-标题", "tex_source"]。

    两阶段：先在单线程里把各级子目录建好（并发建同名文件夹会撞车），
    再用 2 并发上传文件——IMA 并发一高就 403。
    """
    client = ima_store._client()
    tex_fid = client.ensure_path(kb, base_parts)
    plan: List[Tuple[Path, str]] = []
    for path in sorted(p for p in local_dir.rglob("*") if p.is_file()):
        if not _ima_allowed(path):
            stats["skipped"] += 1
            continue
        rel = path.parent.relative_to(local_dir)
        parts = [p for p in rel.parts if p not in (".", "..", "")]
        target = tex_fid
        if parts:
            try:
                target = client.ensure_path(kb, base_parts + parts)
            except IMAError as exc:
                logger.warning("创建子目录失败，文件放到 tex_source 根下: %s", exc)
        plan.append((path, target))

    def work(item: Tuple[Path, str]) -> str:
        path, folder_id = item
        try:
            ima_store._thread_client(kb).upload_file(kb, path, folder_id=folder_id)
            return "uploaded"
        except Exception as exc:
            logger.warning("上传失败 %s: %s", path.name, exc)
            return "failed"

    if plan:
        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as ex:
            for verdict in ex.map(work, plan):   # 在主线程汇总，避免并发改计数
                stats[verdict] += 1


# ----------------------------------------------------------------------
# 编排
# ----------------------------------------------------------------------
_PID_LOCKS: Dict[int, threading.Lock] = {}
_PID_LOCKS_GUARD = threading.Lock()


def _pid_lock(pid: int) -> threading.Lock:
    """同一篇论文的抓取串行化，避免并发触发时重复上传。"""
    with _PID_LOCKS_GUARD:
        lock = _PID_LOCKS.get(pid)
        if lock is None:
            lock = threading.Lock()
            _PID_LOCKS[pid] = lock
        return lock


def _primary_tag(paper: Dict[str, Any]) -> str:
    """与 migrate_to_ima 一致：取主标签，没有则用领域，再没有就是"未分类"。"""
    tags = ima_store._parse_tags(paper.get("tags"))
    if tags:
        return tags[0]
    return (paper.get("field_name") or "").strip() or DEFAULT_TAG


def _writeback(pid: int, paths: Dict[str, str]) -> None:
    """
    把 ima_pdf_path / ima_tex_path 回写到论文的 front-matter。

    PaperRepo.update() 的字段白名单里没有这两个键（ima_store 不可改动），
    所以这里复用它同一套"归档旧版 + 传新版"的写入链路，只多塞两个字段，
    并借用 papers 单例的锁，保证与 PaperRepo.update() 互斥。
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
        target.update(paths)
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


def ensure_paper_assets(pid: int, tag: Optional[str] = None,
                        with_source: bool = True) -> Dict[str, Any]:
    """
    确保一篇论文的 PDF 与 LaTeX 源码已经进 IMA。

    返回 {ok, pdf_path, tex_path, reason, code}：
      code = ok            → 至少有一项资产入库
             no_arxiv_id   → 论文没有 arXiv ID（路由层转 400）
             not_found     → 论文不存在（404）
             upstream      → 下载/上传失败（502）
    reason 是可直接展示给人看的中文说明，本函数不抛裸异常。
    """
    result: Dict[str, Any] = {"ok": False, "pdf_path": "", "tex_path": "",
                              "reason": "", "code": ""}
    notes: List[str] = []
    tmp_dirs: List[Path] = []
    pdf_path = tex_path = ""

    with _pid_lock(pid):
        try:
            paper = ima_store.papers.get(pid)
        except Exception as exc:
            result.update(code="upstream",
                          reason=f"读取论文失败：{type(exc).__name__}: {exc}")
            return result
        if not paper:
            result.update(code="not_found", reason=f"论文 {pid} 不存在")
            return result

        arxiv_id = _normalize(paper.get("arxiv_id") or "")
        if not arxiv_id:
            result.update(code="no_arxiv_id",
                          reason="该论文没有 arXiv ID，无法抓取")
            return result

        # 已经在 IMA 里且未被软删除的资产直接复用，不再去 arXiv 重复下载
        existing_pdf = (paper.get("ima_pdf_path") or "").strip()
        existing_tex = (paper.get("ima_tex_path") or "").strip()
        if existing_pdf and existing_tex:
            result.update(ok=True, code="ok", pdf_path=existing_pdf,
                          tex_path=existing_tex,
                          reason="PDF 与源码已存在 IMA 中，无需重复抓取")
            return result

        base = f"{int(pid):04d}-{ima_store.safe_name(paper.get('title') or '')}"
        folder_tag = ima_store.safe_name(
            (tag or "").strip() or _primary_tag(paper), limit=40)
        paper_parts = [ROOT_FOLDER, folder_tag, base]

        try:
            client = ima_store._client()
            kb = client.knowledge_base_id
            paper_fid = client.ensure_path(kb, paper_parts)

            # ---- PDF ----
            if not existing_pdf:
                try:
                    pdf_file = fetch_pdf(arxiv_id)
                    tmp_dirs.append(pdf_file.parent)
                    client.upload_file(kb, pdf_file, folder_id=paper_fid,
                                       upload_name=f"{base}.pdf")
                    pdf_path = "/".join(paper_parts + [f"{base}.pdf"])
                except (AssetError, IMAError) as exc:
                    notes.append(f"PDF 处理失败：{exc}")
                except Exception as exc:      # 兜底，绝不让异常冒出去
                    notes.append(f"PDF 处理失败：{type(exc).__name__}: {exc}")
            else:
                pdf_path = existing_pdf

            # ---- LaTeX 源码 ----
            if with_source and not existing_tex:
                try:
                    src_dir, why = _fetch_source(arxiv_id)
                    if src_dir is None:
                        notes.append(why)
                    else:
                        # src_dir 形如 <临时目录>/src，里面还有下载到的原始压缩包，
                        # 所以整块临时目录一起登记清理
                        tmp_dirs.append(src_dir.parent)
                        stats = {"uploaded": 0, "skipped": 0, "failed": 0}
                        _upload_tree(src_dir, kb,
                                     paper_parts + [TEX_SUBFOLDER], stats)
                        if stats["uploaded"]:
                            tex_path = "/".join(paper_parts + [TEX_SUBFOLDER])
                            notes.append(
                                f"{why}；已上传 {stats['uploaded']} 个文件，"
                                f"跳过 {stats['skipped']} 个（IMA 不支持的类型）")
                            if stats["failed"]:
                                notes.append(f"{stats['failed']} 个文件上传失败")
                        else:
                            notes.append(
                                f"{why}；但没有 IMA 支持的文件可传"
                                + (f"，{stats['failed']} 个上传失败"
                                   if stats["failed"] else ""))
                except (AssetError, IMAError) as exc:
                    notes.append(f"源码处理失败：{exc}")
                except Exception as exc:
                    notes.append(f"源码处理失败：{type(exc).__name__}: {exc}")
        finally:
            for d in tmp_dirs:
                shutil.rmtree(d, ignore_errors=True)

        if pdf_path or tex_path:
            paths = {"ima_pdf_path": pdf_path or existing_pdf,
                     "ima_tex_path": tex_path or existing_tex}
            try:
                _writeback(pid, paths)
            except Exception as exc:
                notes.append(f"路径回写到 IMA 失败：{type(exc).__name__}: {exc}")

    result["ok"] = bool(pdf_path or tex_path)
    result["code"] = "ok" if result["ok"] else "upstream"
    result["pdf_path"] = pdf_path
    result["tex_path"] = tex_path
    result["reason"] = "；".join(notes)
    return result
