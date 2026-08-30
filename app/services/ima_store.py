"""
IMA 仓储层：把 ResearchBench 的读写建立在 IMA 知识库之上。

设计要点
--------
1. **IMA 是唯一权威源**，本模块里的 SQLite 只是一份**派生只读缓存**
   （表 ima_doc_cache / ima_sync_state）。缓存只是"上次从 IMA 拉到的快照"，
   任何写操作都先写 IMA，再回写缓存条目；绝不把缓存反向写回 IMA 之外的逻辑。

2. **IMA 很慢**：每读一条记录 = 一次列举 + 一次下载解析。95 篇论文逐条串行
   下载要两分钟以上，因此 refresh() 用线程池并发下载，并把结果落到缓存，
   后续请求在 TTL 内直接命中缓存。

3. **IMA 没有更新/删除接口**（已实测）。因此：
   - 更新 = 先把旧条目 rename 成 `_archive_{时间戳}_{原名}` 归档，
     再以**同名**上传新版本（顺序不能反，否则会撞名）；
   - 删除 = 软删除，rename 加 `【已删除】` 前缀，读取侧过滤掉。

4. **降级**：IMA 不可用时，若缓存有数据就继续读缓存并置 stale 标记，
   由路由把"数据可能过期"带给前端；不会让整个页面 500。

数据位置（与 migrate_to_ima.py 保持一致）：
    ResearchBench-ima/metadata/{id:04d}-{标题}.md      论文
    ResearchBench-ima/metadata/news/{id:04d}-{标题}.md 资讯
    ResearchBench-ima/metadata/fields.md               领域表
结构化字段用 Markdown + YAML front-matter 承载（.json 会被 IMA 拒绝）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import yaml
from sqlalchemy import (
    Column, Float, Integer, MetaData, String, Table, Text, delete, select,
    update,
)
from sqlalchemy.exc import SQLAlchemyError

from ..db import engine
from .ima_client import IMAClient, IMAError, MT_FOLDER, get_client

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
ROOT_FOLDER = "ResearchBench-ima"
METADATA = "metadata"
NEWS_SUB = "news"
ARCHIVE_PREFIX = "_archive_"

# 缓存有效期（秒）。IMA 是权威源，缓存只用来挡住高频读取。
CACHE_TTL = int(os.getenv("IMA_CACHE_TTL", "300"))
# IMA 请求失败后的重试冷却（秒），避免每次请求都去撞 IMA
FAILURE_COOLDOWN = int(os.getenv("IMA_RETRY_COOLDOWN", "30"))
# refresh 并发下载线程数。实测：8 并发会被 IMA 限流（HTTP 403 / code 200001
# "请求频率超限"）；2 并发 + 退避重试最稳，配合下面的 blob 缓存，
# 稳态刷新几乎不需要真正下载。
DOWNLOAD_WORKERS = int(os.getenv("IMA_WORKERS", "2"))
# 单条下载的限流重试次数与退避基数（秒）
DOWNLOAD_RETRIES = int(os.getenv("IMA_DOWNLOAD_RETRIES", "4"))
RETRY_BASE_SLEEP = float(os.getenv("IMA_RETRY_BASE", "2"))
# markdown 原文缓存保留天数（按 media_id 命中，IMA 改内容就会换 media_id）
BLOB_TTL_DAYS = int(os.getenv("IMA_BLOB_TTL_DAYS", "30"))

KIND_PAPER = "paper"
KIND_NEWS = "news"
KIND_FIELD = "field"
KIND_RADAR_CONFIG = "radar_config"
# 检索模板整表存一份（与 fields.md 同一套写法：front-matter + 表格）
RADAR_CONFIG_FILE = "radar_configs.md"


# ----------------------------------------------------------------------
# 名字 / 时间 / front-matter 工具
# ----------------------------------------------------------------------
def safe_name(s: str, limit: int = 60) -> str:
    """与 migrate_to_ima.safe_name 完全一致，保证读写同一套命名。"""
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]", " ", str(s or "")).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:limit].strip() or "untitled")


def _prefix(pid: int, title: str) -> str:
    return f"{int(pid):04d}-{safe_name(title)}"


def _parse_tags(raw: Any) -> List[str]:
    """兼容列表 / 逗号分隔字符串。"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [t.strip() for t in re.split(r"[,，、|]", str(raw)) if t.strip()]


def _fmt_dt(v: Any) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    return str(v)


_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _parse_dt(v: Any) -> Optional[datetime]:
    """宽松解析时间字符串；失败返回 None，绝不抛异常。"""
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    s = str(v).strip().replace("T", " ")
    s = re.sub(r"\.\d+.*$", "", s)[:19]
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _as_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


_FM_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)


def parse_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    """
    解析 Markdown 的 YAML front-matter，返回 (meta, body)。

    缺失或 YAML 解析失败时返回 ({}, 原文)，由调用方降级处理，不抛异常。
    """
    t = (text or "").lstrip("\ufeff").lstrip()
    if not t:
        return {}, ""
    m = _FM_RE.match(t)
    if not m:
        return {}, t
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        logger.warning("front-matter 解析失败，按无元数据处理: %s", exc)
        return {}, t[m.end():]
    if not isinstance(meta, dict):
        return {}, t[m.end():]
    return meta, t[m.end():]


def _sections(body: str) -> Dict[str, str]:
    """把正文按 `## ` 标题切成 {标题: 内容}。"""
    out: Dict[str, List[str]] = {}
    cur: Optional[str] = None
    for line in (body or "").splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _strip_title_heading(body: str) -> str:
    """去掉正文首行的 `# 标题`（标题已在 front-matter 里）。"""
    lines = (body or "").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].lstrip().startswith("# "):
        lines.pop(0)
    return "\n".join(lines).strip()


def _id_from_name(file_name: str) -> Optional[int]:
    m = re.match(r"^(\d{1,6})[-_ ]", (file_name or "").strip())
    return int(m.group(1)) if m else None


def _title_from_name(file_name: str) -> str:
    base = re.sub(r"\.md$", "", (file_name or "").strip(), flags=re.I)
    base = re.sub(r"^\d{1,6}[-_ ]", "", base)
    return base.strip() or "(无标题)"


# Markdown 表格单元格的转义：竖线是列分隔符，换行会截断表格行。
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


def _md_cell(v: Any) -> str:
    t = re.sub(r"\s+", " ", str(v if v is not None else "")).strip()
    return t.replace("\\", "\\\\").replace("|", "\\|")


def _md_cell_unescape(s: str) -> str:
    return s.replace("\\|", "|").replace("\\\\", "\\").strip()


# ----------------------------------------------------------------------
# 派生缓存（SQLite）
# ----------------------------------------------------------------------
_CACHE_META = MetaData()
_CACHE_TABLE = Table(
    "ima_doc_cache", _CACHE_META,
    Column("kind", String(16), primary_key=True),
    Column("doc_id", Integer, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("cached_at", Float, nullable=False),
)
_SYNC_TABLE = Table(
    "ima_sync_state", _CACHE_META,
    Column("kind", String(16), primary_key=True),
    Column("synced_at", Float, nullable=False),
    Column("error", Text),
)
# IMA 原文缓存：以 media_id 为键。
# IMA 没有原地更新接口，内容一变 media_id 就变，所以命中即代表内容未变，
# 可以直接复用，不必再花一次列举 + 一次下载。
_BLOB_TABLE = Table(
    "ima_blob_cache", _CACHE_META,
    Column("media_id", String(200), primary_key=True),
    Column("text", Text, nullable=False),
    Column("last_seen", Float, nullable=False),
)

_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def _ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        try:
            _CACHE_META.create_all(engine, checkfirst=True)
            _SCHEMA_READY = True
        except SQLAlchemyError as exc:
            logger.error("IMA 派生缓存表创建失败: %s", exc)
            raise


def _cache_load(kind: str) -> List[dict]:
    _ensure_schema()
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                select(_CACHE_TABLE.c.payload).where(_CACHE_TABLE.c.kind == kind)
            ).all()
    except SQLAlchemyError as exc:
        logger.error("读取 IMA 缓存失败: %s", exc)
        return []
    out: List[dict] = []
    for (payload,) in rows:
        try:
            out.append(json.loads(payload))
        except Exception:
            continue
    return out


def _cache_replace(kind: str, records: List[dict]) -> None:
    _ensure_schema()
    now = time.time()
    with engine.begin() as conn:
        conn.execute(delete(_CACHE_TABLE).where(_CACHE_TABLE.c.kind == kind))
        if records:
            conn.execute(_CACHE_TABLE.insert(), [
                {"kind": kind, "doc_id": int(r.get("id") or 0),
                 "payload": json.dumps(r, ensure_ascii=False), "cached_at": now}
                for r in records
            ])


def _cache_put(kind: str, record: dict) -> None:
    _ensure_schema()
    with engine.begin() as conn:
        conn.execute(
            delete(_CACHE_TABLE).where(
                (_CACHE_TABLE.c.kind == kind)
                & (_CACHE_TABLE.c.doc_id == int(record.get("id") or 0))
            )
        )
        conn.execute(_CACHE_TABLE.insert().values(
            kind=kind, doc_id=int(record.get("id") or 0),
            payload=json.dumps(record, ensure_ascii=False), cached_at=time.time(),
        ))


def _cache_drop(kind: str, doc_id: int) -> None:
    _ensure_schema()
    with engine.begin() as conn:
        conn.execute(delete(_CACHE_TABLE).where(
            (_CACHE_TABLE.c.kind == kind) & (_CACHE_TABLE.c.doc_id == int(doc_id))
        ))


# ----------------------------------------------------------------------
# IMA 原文缓存（按 media_id）
# ----------------------------------------------------------------------
def _blob_get_many(media_ids: List[str]) -> Dict[str, str]:
    """批量取回已缓存的 markdown 原文。"""
    _ensure_schema()
    if not media_ids:
        return {}
    out: Dict[str, str] = {}
    try:
        with engine.begin() as conn:
            for chunk_start in range(0, len(media_ids), 200):
                chunk = media_ids[chunk_start:chunk_start + 200]
                rows = conn.execute(
                    select(_BLOB_TABLE.c.media_id, _BLOB_TABLE.c.text).where(
                        _BLOB_TABLE.c.media_id.in_(chunk)
                    )
                ).all()
                for mid, txt in rows:
                    out[mid] = txt
    except SQLAlchemyError as exc:
        logger.error("读取 IMA 原文缓存失败: %s", exc)
    return out


def _blob_put(media_id: str, text: str) -> None:
    if not media_id or text is None:
        return
    _ensure_schema()
    try:
        with engine.begin() as conn:
            conn.execute(delete(_BLOB_TABLE).where(
                _BLOB_TABLE.c.media_id == media_id))
            conn.execute(_BLOB_TABLE.insert().values(
                media_id=media_id, text=text, last_seen=time.time()))
    except SQLAlchemyError as exc:
        logger.warning("写入 IMA 原文缓存失败: %s", exc)


def _blob_touch(media_ids: List[str]) -> None:
    if not media_ids:
        return
    _ensure_schema()
    try:
        now = time.time()
        with engine.begin() as conn:
            for mid in media_ids:
                conn.execute(
                    _BLOB_TABLE.update()
                    .where(_BLOB_TABLE.c.media_id == mid)
                    .values(last_seen=now)
                )
    except SQLAlchemyError as exc:
        logger.warning("刷新原文缓存时间失败: %s", exc)


def _blob_prune() -> None:
    _ensure_schema()
    cutoff = time.time() - BLOB_TTL_DAYS * 86400
    try:
        with engine.begin() as conn:
            conn.execute(delete(_BLOB_TABLE).where(
                _BLOB_TABLE.c.last_seen < cutoff))
    except SQLAlchemyError as exc:
        logger.warning("清理原文缓存失败: %s", exc)


def _cache_synced_at(kind: str) -> float:
    _ensure_schema()
    try:
        with engine.begin() as conn:
            row = conn.execute(
                select(_SYNC_TABLE.c.synced_at).where(_SYNC_TABLE.c.kind == kind)
            ).first()
    except SQLAlchemyError:
        return 0.0
    return float(row[0]) if row else 0.0


def _mark_synced(kind: str, error: Optional[str] = None,
                 backoff: float = 0.0) -> None:
    _ensure_schema()
    with engine.begin() as conn:
        conn.execute(delete(_SYNC_TABLE).where(_SYNC_TABLE.c.kind == kind))
        conn.execute(_SYNC_TABLE.insert().values(
            kind=kind,
            synced_at=time.time() - backoff,
            error=(error or "")[:500] or None,
        ))


# ----------------------------------------------------------------------
# IMA 客户端
# ----------------------------------------------------------------------
_CLIENT_LOCK = threading.Lock()
_CLIENT: Optional[IMAClient] = None
_TLS = threading.local()


def _client() -> IMAClient:
    """进程内共享一个客户端（避免每次都解析知识库 ID）。"""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = get_client()
        return _CLIENT


def _thread_client(kb: str) -> IMAClient:
    """并发下载时每线程一个客户端（requests.Session 不跨线程复用）。"""
    c = getattr(_TLS, "client", None)
    if c is None:
        c = IMAClient(knowledge_base_id=kb)
        _TLS.client = c
    return c


# 文件夹 id 变动极少，进程内缓存一会儿，省掉每次 refresh 的 3 次列举
_FOLDER_TTL = float(os.getenv("IMA_FOLDER_TTL", "600"))
_FOLDER_CACHE: Dict[str, Any] = {"ts": 0.0, "meta": None, "news": None}
_FOLDER_LOCK = threading.Lock()


def _folders(client: IMAClient, kb: str,
             create: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """返回 (metadata 文件夹 id, news 子文件夹 id)。根目录省略 folder_id。"""
    global _FOLDER_CACHE
    if create:
        meta_id = client.ensure_path(kb, [ROOT_FOLDER, METADATA])
        news_id = client.ensure_path(kb, [ROOT_FOLDER, METADATA, NEWS_SUB])
        with _FOLDER_LOCK:
            _FOLDER_CACHE = {"ts": time.time(), "meta": meta_id, "news": news_id}
        return meta_id, news_id

    now = time.time()
    with _FOLDER_LOCK:
        if _FOLDER_CACHE["meta"] and (_FOLDER_CACHE["ts"] + _FOLDER_TTL) > now:
            return _FOLDER_CACHE["meta"], _FOLDER_CACHE["news"]

    root = client.find_child_folder(kb, ROOT_FOLDER)
    meta_id = client.find_child_folder(kb, METADATA, root) if root else None
    news_id = client.find_child_folder(kb, NEWS_SUB, meta_id) if meta_id else None
    with _FOLDER_LOCK:
        _FOLDER_CACHE = {"ts": now, "meta": meta_id, "news": news_id}
    return meta_id, news_id


def _is_archived(title: Optional[str]) -> bool:
    """
    是否是被归档的旧版本。

    IMA 没有原地更新，更新=旧条目改名成 `_archive_{时间戳}_{原名}` 后上传新版本。
    归档件仍躺在同一个 metadata 目录里，若不排除，它会和现役条目同 id 撞车，
    甚至把旧内容顶掉。
    """
    return bool(title) and title.startswith(ARCHIVE_PREFIX)


def _live_md_items(items: List[dict]) -> List[dict]:
    """挑出"现役"的 markdown 条目：排除文件夹、软删除件与归档旧版本。"""
    return [
        i for i in items
        if i.get("media_type") != MT_FOLDER
        and (i.get("title") or "").lower().endswith(".md")
        and not IMAClient.is_deleted(i.get("title"))
        and not _is_archived(i.get("title"))
    ]


def _is_rate_limited(exc: Exception) -> bool:
    """IMA 限流：HTTP 403 + code 200001「请求频率超限」。"""
    msg = str(exc)
    return "200001" in msg or "频率超限" in msg or "403" in msg


def _download_one(kb: str, media_id: str) -> str:
    """下载一条文本，限流时指数退避重试。"""
    last: Optional[Exception] = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            return _thread_client(kb).download_text(media_id)
        except Exception as exc:
            last = exc
            if not _is_rate_limited(exc) or attempt == DOWNLOAD_RETRIES - 1:
                break
            time.sleep(RETRY_BASE_SLEEP * (2 ** attempt))
    raise last or IMAError(f"下载失败: {media_id}")


def _download_many(client: IMAClient, kb: str,
                   items: List[dict]) -> List[Tuple[bool, str]]:
    """
    取回一批条目的 markdown。

    先按 media_id 命中本地原文缓存（IMA 改内容必换 media_id，所以命中即可信），
    只有没命中的才真正走网络；返回 [(成功?, 文本), ...]。
    """
    if not items:
        return []
    media_ids = [i.get("media_id") for i in items]
    cached = _blob_get_many(media_ids)
    missing = [(i, it) for i, it in enumerate(items)
               if it.get("media_id") not in cached]

    out: List[Tuple[bool, str]] = [(True, cached.get(mid, ""))
                                   for mid in media_ids]
    if cached:
        logger.info("IMA 原文缓存命中 %d/%d 条，实际下载 %d 条",
                    len(cached), len(items), len(missing))

    def work(pair: Tuple[int, dict]) -> Tuple[int, bool, str]:
        i, item = pair
        try:
            return i, True, _download_one(kb, item.get("media_id"))
        except Exception as exc:
            logger.warning("IMA 下载失败 %s: %s", item.get("title"), exc)
            return i, False, ""

    if missing:
        with ThreadPoolExecutor(
            max_workers=min(DOWNLOAD_WORKERS, len(missing))
        ) as ex:
            for i, ok, txt in ex.map(work, missing):
                out[i] = (ok, txt)
                if ok:
                    _blob_put(items[i].get("media_id"), txt)

    if cached:
        _blob_touch([mid for mid in media_ids if mid in cached])
    return out


def _text_for(kb: str, item: dict) -> str:
    """单条取文本（优先原文缓存），失败抛异常。"""
    mid = item.get("media_id")
    cached = _blob_get_many([mid]).get(mid)
    if cached is not None:
        _blob_touch([mid])
        return cached
    txt = _download_one(kb, mid)
    _blob_put(mid, txt)
    return txt


def _replace_file(client: IMAClient, kb: str, folder_id: str,
                  old_item: Optional[dict], file_name: str,
                  text: str) -> dict:
    """
    IMA 没有原地更新：先把旧条目 rename 归档，再上传同名新版本。

    顺序必须是「先归档再上传」，否则 IMA 会判定重名并给新文件加时间戳后缀。
    """
    if old_item and old_item.get("media_id"):
        old_name = old_item.get("title") or file_name
        stamp = time.strftime("%Y%m%d%H%M%S")
        try:
            client.rename(kb, old_item["media_id"],
                          f"{ARCHIVE_PREFIX}{stamp}_{old_name}")
        except Exception as exc:
            # 归档失败不阻断：继续上传，只是 IMA 里会留下历史版本
            logger.warning("归档旧版本失败（继续写入新版本）: %s", exc)
    res = client.upload_text(kb, file_name, text, folder_id=folder_id)
    # 刚写入的内容直接进原文缓存，下次 refresh 不必再下载一次
    _blob_put(res.get("media_id") or "", text)
    return res


# ----------------------------------------------------------------------
# 仓储基类
# ----------------------------------------------------------------------
class PartialSync(Exception):
    """部分条目同步失败（已成功的部分仍可用）。"""

    def __init__(self, records: List[dict], failed: List[str]):
        super().__init__(f"{len(failed)} 条未同步: {', '.join(failed[:5])}")
        self.records = records
        self.failed = failed


class RepoState:
    """最近一次同步的状态，供路由判断是否要提示"数据可能过期"。"""

    def __init__(self) -> None:
        self.synced_at: float = 0.0
        self.stale: bool = False          # 当前数据是缓存快照，可能过期
        self.source: str = "cache"        # ima / cache
        self.error: str = ""              # 最近一次 IMA 错误

    def as_dict(self) -> dict:
        return {
            "stale": self.stale,
            "source": self.source,
            "error": self.error,
            "synced_at": self.synced_at,
            "synced_age": int(time.time() - self.synced_at) if self.synced_at else None,
        }


class _BaseRepo:
    kind = ""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.state = RepoState()

    # ---------------- 读 ----------------
    def _needs_refresh(self) -> bool:
        synced = _cache_synced_at(self.kind)
        if synced <= 0:
            return True
        return (time.time() - synced) > CACHE_TTL

    def _all(self, force: bool = False) -> List[dict]:
        """返回缓存中的原始记录，必要时先从 IMA 同步。"""
        with self._lock:
            if force or self._needs_refresh():
                self._refresh()
            return _cache_load(self.kind)

    def all(self) -> List[dict]:
        """全部记录（已过滤软删除），供统计类接口用。"""
        return [self._out(r) for r in self._all()]

    def refresh(self) -> List[dict]:
        """强制从 IMA 全量拉取并重建缓存。"""
        with self._lock:
            self._refresh()
            return _cache_load(self.kind)

    def _refresh(self) -> None:
        try:
            records = self._fetch_from_ima()
        except PartialSync as ps:
            # 部分条目没拉到：与旧缓存合并，宁可保留旧数据也不留空洞，
            # 并置 stale + 短冷却，下次请求会尽快重试补齐。
            msg = str(ps)[:500]
            logger.warning("IMA 同步 %s 不完整: %s", self.kind, msg)
            cached = _cache_load(self.kind)
            merged = {r.get("id"): r for r in cached}
            for r in ps.records:
                merged[r.get("id")] = r
            _cache_replace(self.kind, list(merged.values()))
            _mark_synced(self.kind, msg, backoff=max(0.0, CACHE_TTL - FAILURE_COOLDOWN))
            self.state.error = msg
            self.state.stale = True
            self.state.source = "cache" if cached else "partial"
            self.state.synced_at = time.time()
            return
        except Exception as exc:  # IMAError / 网络 / 解析异常都兜住
            msg = f"{type(exc).__name__}: {exc}"[:500]
            logger.warning("从 IMA 同步 %s 失败，降级读缓存: %s", self.kind, msg)
            cached = _cache_load(self.kind)
            self.state.error = msg
            self.state.stale = True
            self.state.source = "cache" if cached else "unavailable"
            self.state.synced_at = _cache_synced_at(self.kind)
            # 失败也推进同步时间，留出冷却期，避免每次请求都去撞 IMA
            _mark_synced(self.kind, msg, backoff=max(0.0, CACHE_TTL - FAILURE_COOLDOWN))
            if not cached:
                _cache_replace(self.kind, [])
            return

        _cache_replace(self.kind, records)
        _mark_synced(self.kind)
        self.state.stale = False
        self.state.error = ""
        self.state.source = "ima"
        self.state.synced_at = time.time()

    # ---------------- 子类实现 ----------------
    def _fetch_from_ima(self) -> List[dict]:
        raise NotImplementedError

    def _out(self, rec: dict) -> dict:
        raise NotImplementedError

    # ---------------- 分页工具 ----------------
    @staticmethod
    def _paginate(rows: List[dict], page: int, page_size: int) -> dict:
        page = max(1, int(page or 1))
        page_size = max(1, min(100, int(page_size or 12)))
        total = len(rows)
        pages = (total + page_size - 1) // page_size if total else 0
        start = (page - 1) * page_size
        return {
            "items": rows[start:start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }


# ----------------------------------------------------------------------
# 论文
# ----------------------------------------------------------------------
class PaperRepo(_BaseRepo):
    kind = KIND_PAPER

    # ---- 解析 ----
    def _parse(self, item: dict, text: str, fallback_id: int) -> Optional[dict]:
        file_name = (item.get("title") or "").strip()
        meta, body = parse_front_matter(text)
        # metadata 目录里混放着 fields.md / radar_configs.md，按类型排掉，
        # 否则它们会被当成一篇没有 id 的论文（id 落到 fallback 的负数段）。
        if meta.get("type") in ("fields", "radar_configs") or \
                file_name.lower() in ("fields.md", RADAR_CONFIG_FILE):
            return None

        pid = _as_int(meta.get("id")) or _id_from_name(file_name) or fallback_id
        degraded = not meta
        if degraded:
            title = _title_from_name(file_name)
            abstract = ""
            summary = _strip_title_heading(body)
        else:
            secs = _sections(body)
            title = str(meta.get("title") or _title_from_name(file_name))
            abstract = secs.get("摘要", "")
            summary = secs.get("笔记 / 心得", "")
            if not abstract and not summary:
                summary = _strip_title_heading(body)

        return {
            "id": pid,
            "media_id": item.get("media_id") or "",
            "file_name": file_name,
            "title": title,
            "abstract": abstract,
            "summary": summary,
            "field_id": _as_int(meta.get("field_id")),
            "field": str(meta.get("field") or ""),
            "tags": _parse_tags(meta.get("tags")),
            "reading_status": str(meta.get("reading_status") or "unread"),
            "arxiv_id": str(meta.get("arxiv_id") or ""),
            "original_url": str(meta.get("original_url") or ""),
            "github_url": str(meta.get("github_url") or ""),
            "feishu_doc_url": str(meta.get("feishu_doc_url") or ""),
            "source": str(meta.get("source") or "manual"),
            "ima_pdf_path": str(meta.get("ima_pdf_path") or ""),
            "ima_tex_path": str(meta.get("ima_tex_path") or ""),
            "ima_zh_pdf_path": str(meta.get("ima_zh_pdf_path") or ""),
            "created_at": _fmt_dt(meta.get("created_at")),
            "updated_at": _fmt_dt(meta.get("updated_at")),
            "read_at": _fmt_dt(meta.get("read_at")),
            "favorited_at": _fmt_dt(meta.get("favorited_at")),
            "degraded": degraded,
        }

    def _fetch_from_ima(self) -> List[dict]:
        client = _client()
        kb = client.knowledge_base_id
        meta_id, _ = _folders(client, kb)
        if not meta_id:
            return []
        items = _live_md_items(client.list_folder(kb, meta_id))
        items.sort(key=lambda i: i.get("title") or "")
        results = _download_many(client, kb, items)
        out: List[dict] = []
        failed: List[str] = []
        fallback = -1
        for item, (ok, text) in zip(items, results):
            if not ok:
                failed.append(item.get("title") or item.get("media_id") or "?")
                continue
            rec = self._parse(item, text, fallback)
            if rec is None:
                continue
            if rec["id"] < 0:
                fallback -= 1
            out.append(rec)
        if failed:
            raise PartialSync(out, failed)
        return out

    # ---- 输出 ----
    def _out(self, rec: dict) -> dict:
        fid = rec.get("field_id")
        field_name = rec.get("field") or ""
        if not field_name and fid is not None:
            field_name = fields.name_of(fid)
        return {
            "id": rec.get("id"),
            "title": rec.get("title") or "",
            "abstract": rec.get("abstract") or "",
            "field_id": fid,
            "field_name": field_name,
            "tags": ", ".join(_parse_tags(rec.get("tags"))),
            "original_url": rec.get("original_url") or "",
            "github_url": rec.get("github_url") or "",
            "feishu_doc_url": rec.get("feishu_doc_url") or "",
            "summary": rec.get("summary") or "",
            "tex_repo_path": "",
            "reading_status": rec.get("reading_status") or "unread",
            "arxiv_id": rec.get("arxiv_id") or "",
            "source": rec.get("source") or "manual",
            # IMA 里这份论文的资产位置。之前这三个字段只在 /translate/ima_paths
            # 里单独暴露，导致 /api/papers 与 /api/papers/{pid} 都拿不到，
            # 前端无法判断"抓过还是没抓过"，也没法正确给出重试入口。这里一并带出。
            "ima_pdf_path": rec.get("ima_pdf_path") or "",
            "ima_tex_path": rec.get("ima_tex_path") or "",
            "ima_zh_pdf_path": rec.get("ima_zh_pdf_path") or "",
            "favorited_at": _parse_dt(rec.get("favorited_at")),
            "read_at": _parse_dt(rec.get("read_at")),
            "created_at": _parse_dt(rec.get("created_at")),
            "updated_at": _parse_dt(rec.get("updated_at")),
        }

    # ---- 查询 ----
    def list(self, query: str = "", page: int = 1, page_size: int = 12,
             field_id: Optional[int] = None, status: Optional[str] = None,
             tag: Optional[str] = None) -> dict:
        kw = (query or "").strip().lower()
        tag_kw = (tag or "").strip().lower()
        rows: List[dict] = []
        for rec in self._all():
            if field_id is not None and rec.get("field_id") != field_id:
                continue
            if status and (rec.get("reading_status") or "unread") != status:
                continue
            if tag_kw and not any(
                tag_kw in t.lower() for t in _parse_tags(rec.get("tags"))
            ):
                continue
            if kw:
                hay = " ".join([
                    rec.get("title") or "", rec.get("abstract") or "",
                    rec.get("summary") or "", " ".join(_parse_tags(rec.get("tags"))),
                    rec.get("field") or "", rec.get("arxiv_id") or "",
                ]).lower()
                if kw not in hay:
                    continue
            rows.append(self._out(rec))
        rows.sort(key=lambda d: (d.get("created_at") or datetime.min, d.get("id") or 0),
                  reverse=True)
        return self._paginate(rows, page, page_size)

    def get(self, pid: int) -> Optional[dict]:
        for rec in self._all():
            if rec.get("id") == pid:
                return self._out(rec)
        return None

    def find_duplicate(self, arxiv_id: str = "", url: str = "") -> Optional[dict]:
        """按 arxiv_id / 原文链接查重（Radar 一键收录用）。"""
        for rec in self._all():
            if arxiv_id and (rec.get("arxiv_id") or "") == arxiv_id:
                return self._out(rec)
            if url and (rec.get("original_url") or "") == url:
                return self._out(rec)
        return None

    def all_identity_keys(self) -> set:
        """
        全库论文的"身份标识"集合：arxiv_id / original_url / url 三个键都收。

        Research Radar 用它判断检索结果是否已收录。**刻意只读本地派生缓存**：
        IMA 读一条 = 一次列举 + 一次下载，95 篇全量拉一遍要两分钟，
        不可能为一次检索付这个代价。缓存为空或已过期时才 refresh 一次，
        之后在 TTL 内直接命中。
        """
        rows = _cache_load(self.kind)
        if not rows or self._needs_refresh():
            self.refresh()
            rows = _cache_load(self.kind)
        keys = set()
        for rec in rows:
            for field in ("arxiv_id", "original_url", "url"):
                val = (rec.get(field) or "").strip()
                if val:
                    keys.add(val)
        return keys

    # ---- 写 ----
    def _build_md(self, rec: dict) -> str:
        fm = {
            "id": rec.get("id"),
            "type": "paper",
            "title": rec.get("title") or "",
            "arxiv_id": rec.get("arxiv_id") or "",
            "original_url": rec.get("original_url") or "",
            "github_url": rec.get("github_url") or "",
            "feishu_doc_url": rec.get("feishu_doc_url") or "",
            "field": rec.get("field") or "",
            "field_id": rec.get("field_id"),
            "tags": _parse_tags(rec.get("tags")),
            "reading_status": rec.get("reading_status") or "unread",
            "source": rec.get("source") or "manual",
            "ima_pdf_path": rec.get("ima_pdf_path") or "",
            "ima_tex_path": rec.get("ima_tex_path") or "",
            "ima_zh_pdf_path": rec.get("ima_zh_pdf_path") or "",
            "created_at": rec.get("created_at") or "",
            "updated_at": rec.get("updated_at") or "",
            "read_at": rec.get("read_at") or "",
            "favorited_at": rec.get("favorited_at") or "",
        }
        head = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False,
                              default_flow_style=False).strip()
        body = "\n".join([
            f"# {rec.get('title') or '(无标题)'}",
            "",
            "## 摘要",
            (rec.get("abstract") or "（无摘要）").strip(),
            "",
            "## 笔记 / 心得",
            (rec.get("summary") or "（暂无笔记）").strip(),
            "",
        ])
        return f"---\n{head}\n---\n\n{body}\n"

    def _next_id(self, records: List[dict]) -> int:
        ids = [r.get("id") or 0 for r in records if (r.get("id") or 0) > 0]
        return (max(ids) + 1) if ids else 1

    def create(self, data: dict) -> dict:
        with self._lock:
            records = self._all()
            now = datetime.now()
            fid = _as_int(data.get("field_id"))
            rec = {
                "id": self._next_id(records),
                "title": (data.get("title") or "").strip(),
                "abstract": data.get("abstract") or "",
                "summary": data.get("summary") or "",
                "field_id": fid,
                "field": fields.name_of(fid) if fid is not None else "",
                "tags": _parse_tags(data.get("tags")),
                "reading_status": data.get("reading_status") or "unread",
                "arxiv_id": data.get("arxiv_id") or "",
                "original_url": data.get("original_url") or "",
                "github_url": data.get("github_url") or "",
                "feishu_doc_url": data.get("feishu_doc_url") or "",
                "source": data.get("source") or "manual",
                "ima_pdf_path": "", "ima_tex_path": "", "ima_zh_pdf_path": "",
                "created_at": _fmt_dt(now),
                "updated_at": _fmt_dt(now),
                "read_at": _fmt_dt(now) if data.get("reading_status") == "read" else "",
                "favorited_at": _fmt_dt(now),
            }
            client = _client()
            kb = client.knowledge_base_id
            meta_id, _ = _folders(client, kb, create=True)
            md = self._build_md(rec)
            res = client.upload_text(
                kb, f"{_prefix(rec['id'], rec['title'])}.md",
                md, folder_id=meta_id)
            _blob_put(res.get("media_id") or "", md)
            rec["media_id"] = res.get("media_id") or ""
            rec["file_name"] = res.get("file_name") or ""
            _cache_put(self.kind, rec)
            return self._out(rec)

    def update(self, pid: int, data: dict) -> Optional[dict]:
        with self._lock:
            target = None
            for rec in self._all():
                if rec.get("id") == pid:
                    target = dict(rec)
                    break
            if target is None:
                return None

            for key in ("title", "abstract", "summary", "arxiv_id", "original_url",
                        "github_url", "feishu_doc_url", "reading_status", "source"):
                if key in data and data[key] is not None:
                    target[key] = data[key]
            if "tags" in data and data["tags"] is not None:
                target["tags"] = _parse_tags(data["tags"])
            if "field_id" in data and data["field_id"] is not None:
                fid = _as_int(data["field_id"])
                target["field_id"] = fid
                target["field"] = fields.name_of(fid) if fid is not None else ""

            now = datetime.now()
            if target.get("reading_status") == "read" and not target.get("read_at"):
                target["read_at"] = _fmt_dt(now)
            target["updated_at"] = _fmt_dt(now)

            client = _client()
            kb = client.knowledge_base_id
            meta_id, _ = _folders(client, kb, create=True)
            old_item = {"media_id": target.get("media_id"),
                        "title": target.get("file_name")} \
                if target.get("media_id") else None
            file_name = f"{_prefix(pid, target.get('title') or '')}.md"
            res = _replace_file(client, kb, meta_id, old_item, file_name,
                                self._build_md(target))
            target["media_id"] = res.get("media_id") or target.get("media_id") or ""
            target["file_name"] = res.get("file_name") or file_name
            _cache_put(self.kind, target)
            return self._out(target)

    def soft_delete(self, pid: int) -> bool:
        with self._lock:
            target = None
            for rec in self._all():
                if rec.get("id") == pid:
                    target = rec
                    break
            if target is None:
                return False
            client = _client()
            kb = client.knowledge_base_id
            try:
                if target.get("media_id"):
                    client.soft_delete(
                        kb, target["media_id"],
                        target.get("file_name") or f"{_prefix(pid, target.get('title') or '')}.md",
                    )
            except IMAError as exc:
                logger.warning("IMA 软删除失败（仍从本地视图移除）: %s", exc)
            _cache_drop(self.kind, pid)
            return True


# ----------------------------------------------------------------------
# 资讯
# ----------------------------------------------------------------------
class NewsRepo(_BaseRepo):
    kind = KIND_NEWS

    def _parse(self, item: dict, text: str, fallback_id: int) -> Optional[dict]:
        file_name = (item.get("title") or "").strip()
        meta, body = parse_front_matter(text)
        nid = _as_int(meta.get("id")) or _id_from_name(file_name) or fallback_id
        title = str(meta.get("title") or _title_from_name(file_name))
        summary = _strip_title_heading(body).strip()
        if summary == "（无摘要）":
            summary = ""
        return {
            "id": nid,
            "media_id": item.get("media_id") or "",
            "file_name": file_name,
            "title": title,
            "url": str(meta.get("url") or ""),
            "source": str(meta.get("source") or ""),
            "published": str(meta.get("published") or ""),
            "field": str(meta.get("field") or ""),
            "note": str(meta.get("note") or ""),
            "reading_status": str(meta.get("reading_status") or "unread"),
            "summary": summary,
            "created_at": _fmt_dt(meta.get("created_at")),
            "updated_at": _fmt_dt(meta.get("updated_at")),
            "degraded": not meta,
        }

    def _fetch_from_ima(self) -> List[dict]:
        client = _client()
        kb = client.knowledge_base_id
        _, news_id = _folders(client, kb)
        if not news_id:
            return []
        items = _live_md_items(client.list_folder(kb, news_id))
        items.sort(key=lambda i: i.get("title") or "")
        results = _download_many(client, kb, items)
        out: List[dict] = []
        failed: List[str] = []
        fallback = -1
        for item, (ok, text) in zip(items, results):
            if not ok:
                failed.append(item.get("title") or item.get("media_id") or "?")
                continue
            rec = self._parse(item, text, fallback)
            if rec is None:
                continue
            if rec["id"] < 0:
                fallback -= 1
            out.append(rec)
        if failed:
            raise PartialSync(out, failed)
        return out

    def _out(self, rec: dict) -> dict:
        return {
            "id": rec.get("id"),
            "title": rec.get("title") or "",
            "url": rec.get("url") or "",
            "source": rec.get("source") or "",
            "published": rec.get("published") or "",
            "summary": rec.get("summary") or "",
            "field": rec.get("field") or "",
            "note": rec.get("note") or "",
            "reading_status": rec.get("reading_status") or "unread",
            "created_at": _parse_dt(rec.get("created_at")),
        }

    def list(self, query: str = "", page: int = 1, page_size: int = 12,
             source: Optional[str] = None) -> dict:
        kw = (query or "").strip().lower()
        rows: List[dict] = []
        for rec in self._all():
            if source and (rec.get("source") or "") != source:
                continue
            if kw and kw not in " ".join([
                rec.get("title") or "", rec.get("summary") or "",
                rec.get("source") or "", rec.get("url") or "",
            ]).lower():
                continue
            rows.append(self._out(rec))
        rows.sort(key=lambda d: (d.get("created_at") or datetime.min, d.get("id") or 0),
                  reverse=True)
        return self._paginate(rows, page, page_size)

    def get(self, nid: int) -> Optional[dict]:
        for rec in self._all():
            if rec.get("id") == nid:
                return self._out(rec)
        return None

    def find_by_url(self, url: str) -> Optional[dict]:
        if not url:
            return None
        for rec in self._all():
            if (rec.get("url") or "") == url:
                return self._out(rec)
        return None

    def _build_md(self, rec: dict) -> str:
        fm = {
            "id": rec.get("id"),
            "type": "news",
            "title": rec.get("title") or "",
            "source": rec.get("source") or "",
            "url": rec.get("url") or "",
            "published": rec.get("published") or "",
            "field": rec.get("field") or "",
            "note": rec.get("note") or "",
            "reading_status": rec.get("reading_status") or "unread",
            "created_at": rec.get("created_at") or "",
            "updated_at": rec.get("updated_at") or "",
        }
        head = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False,
                              default_flow_style=False).strip()
        body = "\n".join([
            f"# {rec.get('title') or '(无标题)'}",
            "",
            (rec.get("summary") or "（无摘要）").strip(),
            "",
        ])
        return f"---\n{head}\n---\n\n{body}\n"

    def _next_id(self, records: List[dict]) -> int:
        ids = [r.get("id") or 0 for r in records if (r.get("id") or 0) > 0]
        return (max(ids) + 1) if ids else 1

    def create(self, data: dict) -> dict:
        with self._lock:
            records = self._all()
            now = datetime.now()
            rec = {
                "id": self._next_id(records),
                "title": (data.get("title") or "").strip(),
                "url": data.get("url") or "",
                "source": data.get("source") or "",
                "published": data.get("published") or "",
                "field": data.get("field") or "",
                "note": data.get("note") or "",
                "summary": data.get("summary") or "",
                "reading_status": data.get("reading_status") or "unread",
                "created_at": _fmt_dt(now),
                "updated_at": _fmt_dt(now),
            }
            client = _client()
            kb = client.knowledge_base_id
            _, news_id = _folders(client, kb, create=True)
            md = self._build_md(rec)
            res = client.upload_text(
                kb, f"{_prefix(rec['id'], rec['title'])}.md",
                md, folder_id=news_id)
            _blob_put(res.get("media_id") or "", md)
            rec["media_id"] = res.get("media_id") or ""
            rec["file_name"] = res.get("file_name") or ""
            _cache_put(self.kind, rec)
            return self._out(rec)

    def create_many(self, items: List[dict]) -> int:
        """批量新增（按 url 去重），返回新增条数。"""
        added = 0
        existing = {(r.get("url") or "") for r in self._all() if r.get("url")}
        for data in items:
            url = (data.get("url") or "").strip()
            if url and url in existing:
                continue
            self.create(data)
            if url:
                existing.add(url)
            added += 1
        return added

    def update(self, nid: int, data: dict) -> Optional[dict]:
        with self._lock:
            target = None
            for rec in self._all():
                if rec.get("id") == nid:
                    target = dict(rec)
                    break
            if target is None:
                return None
            for key in ("title", "url", "source", "published", "field", "note",
                        "summary", "reading_status"):
                if key in data and data[key] is not None:
                    target[key] = data[key]
            target["updated_at"] = _fmt_dt(datetime.now())
            client = _client()
            kb = client.knowledge_base_id
            _, news_id = _folders(client, kb, create=True)
            old_item = {"media_id": target.get("media_id"),
                        "title": target.get("file_name")} \
                if target.get("media_id") else None
            file_name = f"{_prefix(nid, target.get('title') or '')}.md"
            res = _replace_file(client, kb, news_id, old_item, file_name,
                                self._build_md(target))
            target["media_id"] = res.get("media_id") or target.get("media_id") or ""
            target["file_name"] = res.get("file_name") or file_name
            _cache_put(self.kind, target)
            return self._out(target)

    def soft_delete(self, nid: int) -> bool:
        with self._lock:
            target = None
            for rec in self._all():
                if rec.get("id") == nid:
                    target = rec
                    break
            if target is None:
                return False
            client = _client()
            kb = client.knowledge_base_id
            try:
                if target.get("media_id"):
                    client.soft_delete(
                        kb, target["media_id"],
                        target.get("file_name") or f"{_prefix(nid, target.get('title') or '')}.md",
                    )
            except IMAError as exc:
                logger.warning("IMA 软删除失败（仍从本地视图移除）: %s", exc)
            _cache_drop(self.kind, nid)
            return True


# ----------------------------------------------------------------------
# 领域
# ----------------------------------------------------------------------
class FieldRepo(_BaseRepo):
    """领域表存在单个 metadata/fields.md 里（Markdown 表格）。"""

    kind = KIND_FIELD
    FILE_NAME = "fields.md"

    def __init__(self) -> None:
        super().__init__()
        self._file: Optional[dict] = None   # fields.md 的 media_id，用于更新时归档

    # ---- 解析 ----
    @staticmethod
    def _parse_rows(text: str) -> List[dict]:
        _, body = parse_front_matter(text)
        out: List[dict] = []
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 2 or not cells[0] or set(cells[0]) <= set("-: "):
                continue
            fid = _as_int(cells[0])
            if fid is None:
                continue
            out.append({"id": fid, "name": cells[1]})
        return out

    def _fetch_from_ima(self) -> List[dict]:
        client = _client()
        kb = client.knowledge_base_id
        meta_id, _ = _folders(client, kb)
        self._file = None
        if not meta_id:
            return []
        item = None
        for i in client.list_folder(kb, meta_id):
            title = i.get("title") or ""
            if i.get("media_type") != MT_FOLDER and title == self.FILE_NAME:
                item = i
                break
        if item is None:
            return []
        self._file = {"media_id": item.get("media_id"), "title": self.FILE_NAME}
        return self._parse_rows(_text_for(kb, item))

    def _out(self, rec: dict) -> dict:
        return {"id": rec.get("id"), "name": rec.get("name") or ""}

    def list(self) -> List[dict]:
        rows = [self._out(r) for r in self._all()]
        rows.sort(key=lambda d: (d.get("id") or 0))
        return rows

    def get(self, fid: int) -> Optional[dict]:
        for rec in self._all():
            if rec.get("id") == fid:
                return self._out(rec)
        return None

    def name_of(self, fid: Optional[int]) -> str:
        """只查缓存、不触发 IMA 同步（供论文输出时补全 field_name）。"""
        if fid is None:
            return ""
        try:
            for rec in _cache_load(self.kind):
                if rec.get("id") == fid:
                    return rec.get("name") or ""
        except Exception:
            pass
        return ""

    def find_by_name(self, name: str) -> Optional[dict]:
        target = (name or "").strip()
        for rec in self._all():
            if (rec.get("name") or "").strip() == target:
                return self._out(rec)
        return None

    def get_or_create(self, name: str) -> Optional[dict]:
        name = (name or "").strip()
        if not name:
            return None
        found = self.find_by_name(name)
        return found or self.create({"name": name})

    # ---- 写：整表重写（归档旧 fields.md + 上传新 fields.md）----
    def _build_md(self, rows: List[dict]) -> str:
        fm = {
            "type": "fields",
            "count": len(rows),
            "updated_at": _fmt_dt(datetime.now()),
        }
        head = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False,
                              default_flow_style=False).strip()
        table = ["| id | 名称 | 颜色 |", "| --- | --- | --- |"]
        for r in sorted(rows, key=lambda x: x.get("id") or 0):
            table.append(f"| {r.get('id')} | {r.get('name') or ''} | |")
        return f"---\n{head}\n---\n\n# 领域 / 标签\n\n" + "\n".join(table) + "\n"

    def _write(self, rows: List[dict]) -> List[dict]:
        client = _client()
        kb = client.knowledge_base_id
        meta_id, _ = _folders(client, kb, create=True)
        res = _replace_file(client, kb, meta_id, self._file, self.FILE_NAME,
                            self._build_md(rows))
        self._file = {"media_id": res.get("media_id") or "", "title": self.FILE_NAME}
        _cache_replace(self.kind, rows)
        _mark_synced(self.kind)
        self.state.stale = False
        self.state.error = ""
        self.state.source = "ima"
        self.state.synced_at = time.time()
        return rows

    def create(self, data: dict) -> dict:
        with self._lock:
            rows = self._all()
            name = (data.get("name") or "").strip()
            for r in rows:
                if (r.get("name") or "").strip() == name:
                    return self._out(r)
            ids = [r.get("id") or 0 for r in rows]
            rec = {"id": (max(ids) + 1) if ids else 1, "name": name}
            self._write(rows + [rec])
            return self._out(rec)

    def update(self, fid: int, data: dict) -> Optional[dict]:
        with self._lock:
            rows = self._all()
            for r in rows:
                if r.get("id") == fid:
                    r["name"] = (data.get("name") or r.get("name") or "").strip()
                    self._write(rows)
                    return self._out(r)
            return None

    def soft_delete(self, fid: int) -> bool:
        with self._lock:
            rows = self._all()
            left = [r for r in rows if r.get("id") != fid]
            if len(left) == len(rows):
                return False
            self._write(left)
            return True


# ----------------------------------------------------------------------
# Research Radar 检索模板（RadarConfig）
# ----------------------------------------------------------------------
class RadarConfigRepo(_BaseRepo):
    """
    检索模板整表存在 metadata/radar_configs.md，写法与 fields.md 一致：
    YAML front-matter 存元信息，正文一张 Markdown 表格存每一条配置。

    同样受 IMA 限制：
      - 没有更新接口 → 任何写操作都走 _replace_file()（旧版归档改名 + 上传新版）；
      - 没有删除接口 → 删除是软删除，给该行打 `【已删除】` 前缀 + deleted 标记，
        读取时过滤。被删的行仍然留在文件里，因此 _next_id() 必须把已删除行的
        id 也算进去，否则新模板会撞上旧 id。
    """

    kind = KIND_RADAR_CONFIG
    FILE_NAME = RADAR_CONFIG_FILE
    COLUMNS = (
        "id", "name", "type", "field", "keywords", "note", "enabled",
        "time_range_days", "lang", "channel", "created_at", "updated_at",
        "deleted",
    )
    TEXT_COLUMNS = ("name", "type", "field", "keywords", "note",
                    "lang", "channel")

    def __init__(self) -> None:
        super().__init__()
        self._file: Optional[dict] = None   # radar_configs.md 的 media_id，写时用于归档

    # ---- 解析 ----
    @classmethod
    def _normalize(cls, raw: dict) -> Optional[dict]:
        cid = _as_int(raw.get("id"))
        if cid is None:
            return None
        now = _fmt_dt(datetime.now())
        return {
            "id": cid,
            "name": _md_cell_unescape(str(raw.get("name") or "")),
            "type": str(raw.get("type") or "paper").strip() or "paper",
            "field": _md_cell_unescape(str(raw.get("field") or "")),
            "keywords": _md_cell_unescape(str(raw.get("keywords") or "")),
            "note": _md_cell_unescape(str(raw.get("note") or "")),
            "enabled": _as_bool(raw.get("enabled"), default=True),
            "time_range_days": _as_int(raw.get("time_range_days")) or 2,
            "lang": str(raw.get("lang") or "en").strip() or "en",
            "channel": str(raw.get("channel") or "google").strip() or "google",
            "created_at": _fmt_dt(raw.get("created_at")) or now,
            "updated_at": _fmt_dt(raw.get("updated_at")) or now,
            "deleted": "1" if _as_bool(raw.get("deleted")) else "",
        }

    @classmethod
    def _parse_rows(cls, text: str) -> List[dict]:
        """从 Markdown 表格还原配置行（已删除的行也保留，供 id 分配与清理清单）。"""
        _, body = parse_front_matter(text)
        header: List[str] = []
        rows: List[dict] = []
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [_md_cell_unescape(c)
                     for c in _CELL_SPLIT_RE.split(line.strip("|"))]
            if not header:
                header = [c.strip().lower() for c in cells]
                continue
            if not "".join(cells).strip("-: "):     # | --- | --- | 分隔行
                continue
            rows.append(dict(zip(header, cells)))

        out: List[dict] = []
        seen = set()
        for raw in rows:
            rec = cls._normalize(raw)
            if rec and rec["id"] not in seen:
                seen.add(rec["id"])
                out.append(rec)
        return out

    def _find_items(self, client: IMAClient, kb: str,
                    meta_id: str) -> List[dict]:
        """列出 metadata 下所有同名配置文件的条目（正常情况下只应有一条）。"""
        return [i for i in client.list_folder(kb, meta_id)
                if i.get("media_type") != MT_FOLDER
                and (i.get("title") or "") == self.FILE_NAME]

    def _find_item(self, client: IMAClient, kb: str,
                   meta_id: str) -> Optional[dict]:
        items = self._find_items(client, kb, meta_id)
        return items[0] if items else None

    def _dedupe(self, client: IMAClient, kb: str, meta_id: str) -> int:
        """
        清掉同名副本，只保留 self._file 指向的那一份。

        并发写入会产生副本：两个进程同时判断"文件还不存在" → 各自上传一份同名文件，
        IMA 不会拒绝同名（不像上传前会用 check_repeated_names 改名），结果 metadata
        下出现两份 radar_configs.md，而读取只取第一份，于是改的配置可能写进了另一份，
        看起来"改了没生效"。这里把多余的标记成【已删除】，读取侧自动过滤。
        """
        keep = (self._file or {}).get("media_id")
        removed = 0
        for extra in self._find_items(client, kb, meta_id):
            if keep and extra.get("media_id") == keep:
                continue
            try:
                client.soft_delete(kb, extra.get("media_id"), self.FILE_NAME)
                removed += 1
            except Exception as exc:                     # 清理失败不阻断主流程
                logger.warning("清理重复的 %s 失败: %s", self.FILE_NAME, exc)
        return removed

    def _fetch_from_ima(self) -> List[dict]:
        client = _client()
        kb = client.knowledge_base_id
        meta_id, _ = _folders(client, kb)
        self._file = None
        if not meta_id:
            return []
        item = self._find_item(client, kb, meta_id)
        if item is None:
            return []
        self._file = {"media_id": item.get("media_id"), "title": self.FILE_NAME}
        self._dedupe(client, kb, meta_id)          # 读取时顺手清掉同名副本
        return self._parse_rows(_text_for(kb, item))

    # ---- 输出 ----
    def _out(self, rec: dict) -> dict:
        return {
            "id": rec.get("id"),
            "name": rec.get("name") or "",
            "type": rec.get("type") or "paper",
            "field": rec.get("field") or "",
            "keywords": rec.get("keywords") or "",
            "note": rec.get("note") or "",
            "enabled": bool(rec.get("enabled", True)),
            "time_range_days": rec.get("time_range_days") or 2,
            "lang": rec.get("lang") or "en",
            "channel": rec.get("channel") or "google",
            "created_at": rec.get("created_at") or "",
            "updated_at": rec.get("updated_at") or "",
        }

    @staticmethod
    def _is_deleted_row(rec: dict) -> bool:
        return bool(rec.get("deleted")) or IMAClient.is_deleted(rec.get("name"))

    # ---- 查询 ----
    def list(self) -> List[dict]:
        rows = [self._out(r) for r in self._all() if not self._is_deleted_row(r)]
        rows.sort(key=lambda d: (d.get("created_at") or "", d.get("id") or 0),
                  reverse=True)
        return rows

    def list_deleted(self) -> List[dict]:
        """已软删除的行：IMA 无删除接口，需用户在客户端手动清理。"""
        return [self._out(r) for r in self._all() if self._is_deleted_row(r)]

    def get(self, cid: int) -> Optional[dict]:
        for rec in self._all():
            if rec.get("id") == cid and not self._is_deleted_row(rec):
                return self._out(rec)
        return None

    # ---- 写：整表重写（归档旧 radar_configs.md + 上传新文件）----
    def _build_md(self, rows: List[dict]) -> str:
        live = [r for r in rows if not self._is_deleted_row(r)]
        fm = {
            "type": "radar_configs",
            "count": len(live),
            "updated_at": _fmt_dt(datetime.now()),
        }
        head = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False,
                              default_flow_style=False).strip()
        table = [
            "| " + " | ".join(self.COLUMNS) + " |",
            "| " + " | ".join("---" for _ in self.COLUMNS) + " |",
        ]
        for r in sorted(rows, key=lambda x: x.get("id") or 0):
            cells = []
            for col in self.COLUMNS:
                val = r.get(col)
                if col == "enabled":
                    val = "true" if r.get("enabled", True) else "false"
                elif col == "deleted":
                    val = "1" if r.get("deleted") else ""
                cells.append(_md_cell(val))
            table.append("| " + " | ".join(cells) + " |")
        return (f"---\n{head}\n---\n\n# Research Radar 检索模板\n\n"
                + "\n".join(table) + "\n")

    def _write(self, rows: List[dict]) -> List[dict]:
        client = _client()
        kb = client.knowledge_base_id
        meta_id, _ = _folders(client, kb, create=True)
        if self._file is None:
            # 本进程还没 refresh 过（缓存命中直接走到写），先补查一次，
            # 否则会跳过归档、让 IMA 把新文件改名成 radar_configs_时间戳.md。
            item = self._find_item(client, kb, meta_id)
            if item:
                self._file = {"media_id": item.get("media_id"),
                              "title": self.FILE_NAME}
        # 写入前先清掉同名副本，避免"改了却读到另一份"的并发后遗症
        self._dedupe(client, kb, meta_id)
        res = _replace_file(client, kb, meta_id, self._file, self.FILE_NAME,
                            self._build_md(rows))
        self._file = {"media_id": res.get("media_id") or "",
                      "title": self.FILE_NAME}
        _cache_replace(self.kind, rows)
        _mark_synced(self.kind)
        self.state.stale = False
        self.state.error = ""
        self.state.source = "ima"
        self.state.synced_at = time.time()
        return rows

    def _row(self, cid: int, data: dict) -> dict:
        now = _fmt_dt(datetime.now())
        return {
            "id": cid,
            "name": (data.get("name") or "").strip(),
            "type": (data.get("type") or "paper").strip() or "paper",
            "field": (data.get("field") or "").strip(),
            "keywords": (data.get("keywords") or "").strip(),
            "note": (data.get("note") or "").strip(),
            "enabled": bool(data.get("enabled", True)),
            "time_range_days": _as_int(data.get("time_range_days")) or 2,
            "lang": (data.get("lang") or "en").strip() or "en",
            "channel": (data.get("channel") or "google").strip() or "google",
            "created_at": _fmt_dt(data.get("created_at")) or now,
            "updated_at": _fmt_dt(data.get("updated_at")) or now,
            "deleted": "",
        }

    def _next_id(self, records: List[dict]) -> int:
        """max(id)+1。已软删除的行也占位，避免新模板复用旧 id。"""
        ids = [r.get("id") or 0 for r in records if (r.get("id") or 0) > 0]
        return (max(ids) + 1) if ids else 1

    def create(self, data: dict) -> dict:
        with self._lock:
            rows = self._all()
            rec = self._row(self._next_id(rows), data)
            self._write(rows + [rec])
            return self._out(rec)

    def seed(self, items: List[dict]) -> int:
        """批量写入（迁移 / 默认模板初始化），只用一次上传。已存在则整表跳过。"""
        with self._lock:
            rows = self._all()
            if rows:
                return 0
            used: set = set()
            fresh: List[dict] = []
            for data in items:
                cid = _as_int(data.get("id"))
                if cid is None or cid in used:
                    cid = self._next_id(fresh)
                used.add(cid)
                fresh.append(self._row(cid, data))
            self._write(fresh)
            return len(fresh)

    def update(self, cid: int, data: dict) -> Optional[dict]:
        with self._lock:
            rows = self._all()
            for r in rows:
                if r.get("id") == cid and not self._is_deleted_row(r):
                    for key in self.TEXT_COLUMNS:
                        if key in data and data[key] is not None:
                            r[key] = str(data[key]).strip()
                    if "enabled" in data and data["enabled"] is not None:
                        r["enabled"] = bool(data["enabled"])
                    if data.get("time_range_days") is not None:
                        r["time_range_days"] = (_as_int(data["time_range_days"])
                                                or r.get("time_range_days") or 2)
                    r["updated_at"] = _fmt_dt(datetime.now())
                    self._write(rows)
                    return self._out(r)
            return None

    def delete(self, cid: int) -> bool:
        """软删除：IMA 无删除接口，只能打标记由读取侧过滤。"""
        with self._lock:
            rows = self._all()
            for r in rows:
                if r.get("id") == cid and not self._is_deleted_row(r):
                    stamp = time.strftime("%Y%m%d%H%M%S")
                    r["name"] = (f"{IMAClient.DELETED_PREFIX}{stamp}_"
                                 f"{r.get('name') or cid}")
                    r["deleted"] = "1"
                    r["updated_at"] = _fmt_dt(datetime.now())
                    self._write(rows)
                    return True
            return False


# ----------------------------------------------------------------------
# 模块级单例
# ----------------------------------------------------------------------
papers = PaperRepo()
news = NewsRepo()
fields = FieldRepo()
radar_configs = RadarConfigRepo()

_REPOS = (papers, news, fields, radar_configs)


def status() -> dict:
    """各仓储的同步状态，便于页面提示"数据可能过期"。"""
    return {r.kind: r.state.as_dict() for r in _REPOS}


def refresh_all() -> dict:
    """全量重建缓存（IMA 是权威源，缓存只是派生快照）。"""
    _blob_prune()
    out = {}
    for r in _REPOS:
        try:
            r.refresh()
        except Exception as exc:   # 单个仓储失败不影响其他
            logger.warning("refresh %s 失败: %s", r.kind, exc)
        out[r.kind] = r.state.as_dict()
    return out


def invalidate() -> None:
    """清空派生缓存，下次读取强制回源 IMA。"""
    for r in _REPOS:
        try:
            _cache_replace(r.kind, [])
            _mark_synced(r.kind, "", backoff=CACHE_TTL)
        except Exception as exc:
            logger.warning("清空 %s 缓存失败: %s", r.kind, exc)
