"""Research Radar：定时/手动发现最新论文与 AI 资讯。

数据源说明：
  - 检索模板（RadarConfig）与论文库都统一存放在 IMA 知识库，走
    `ima_store.RadarConfigRepo` / `PaperRepo`。
  - SQLite 里的 RadarConfig 表只作为**历史遗留**，首次访问时会自动
    迁移到 IMA，之后读写一律走 IMA。
  - "是否已收录"用 `PaperRepo.all_identity_keys()` 判断，它只读本地
    派生缓存，不会为一次检索去全量拉取 IMA。

进度说明：
  - /run_bg 与 /run_template_bg 是带步骤进度的后台检索：立即返回
    job_id，前端轮询 /job/{job_id} 渲染"每一步在干什么"。
  - 老的 /run 与 /run_template 保持不变（一次性同步返回），兼容旧调用。
"""
import asyncio
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import SessionLocal
from ..models import RadarConfig as LegacyRadarConfig
from ..config import DEFAULT_RADAR_TEMPLATES
from ..services import arxiv, news, radar_jobs
from ..services.ima_store import PaperRepo, RadarConfigRepo

logger = logging.getLogger("radar")

router = APIRouter(prefix="/api/radar", tags=["radar"])


class RadarConfigCreate(BaseModel):
    name: str
    type: str = "paper"           # paper / news
    field: str = ""
    keywords: str = ""
    note: str = ""
    enabled: bool = True
    time_range_days: int = 2
    lang: str = "en"              # 资讯语言：en / zh / auto
    channel: str = "google"       # 资讯渠道：google / cn / all


class RadarConfigUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    field: Optional[str] = None
    keywords: Optional[str] = None
    note: Optional[str] = None
    enabled: Optional[bool] = None
    time_range_days: Optional[int] = None
    lang: Optional[str] = None
    channel: Optional[str] = None


class RadarConfigOut(BaseModel):
    id: int
    name: str
    type: str
    field: str
    keywords: str
    note: str
    enabled: bool
    time_range_days: int
    lang: str = "en"
    channel: str = "google"

    model_config = {"from_attributes": True}


class RadarRunIn(BaseModel):
    """
    检索请求体。

    前端 app.js 是用 JSON body 调 /api/radar/run 的，而这个接口原先
    把标量声明成 query 参数，导致实际一直按默认值检索（历史 bug）。
    现在同时支持 JSON body 与 query 参数：body 优先，缺省回落到 query。
    """
    type: str = "paper"
    keywords: str = ""
    field: str = ""
    days: int = 2
    max_results: int = 30
    lang: str = "en"
    channel: str = "google"


def _repo() -> RadarConfigRepo:
    return RadarConfigRepo()


def _migrate_legacy_configs(repo: RadarConfigRepo) -> int:
    """
    一次性迁移：把 SQLite 遗留的检索模板搬到 IMA。

    仅当 IMA 中还没有任何模板时才执行，避免重复灌入。
    """
    try:
        if repo.list():
            return 0
    except Exception as e:
        logger.warning("radar: 检查 IMA 模板失败，跳过迁移: %r", e)
        return 0

    db = SessionLocal()
    try:
        rows = db.query(LegacyRadarConfig).order_by(LegacyRadarConfig.id).all()
        if not rows:
            return 0
        items = [{
            "id": r.id,
            "name": r.name,
            "type": r.type,
            "field": r.field or "",
            "keywords": r.keywords or "",
            "note": r.note or "",
            "enabled": bool(r.enabled),
            "time_range_days": r.time_range_days,
            "lang": getattr(r, "lang", None) or "en",
            "channel": getattr(r, "channel", None) or "google",
            "created_at": getattr(r, "created_at", None),
        } for r in rows]
        n = repo.seed(items)
        logger.info("radar: 已从 SQLite 迁移 %s 条检索模板到 IMA", n)
        return n
    except Exception as e:
        logger.warning("radar: 迁移遗留模板失败: %r", e)
        return 0
    finally:
        db.close()


@router.get("/configs", response_model=list[RadarConfigOut])
def list_configs():
    repo = _repo()
    _migrate_legacy_configs(repo)
    try:
        return repo.list()
    except Exception as e:
        logger.warning("radar: 读取模板失败: %r", e)
        raise HTTPException(502, f"读取检索模板失败：{e}")


@router.post("/configs", response_model=RadarConfigOut)
def create_config(payload: RadarConfigCreate):
    try:
        return _repo().create(payload.model_dump())
    except Exception as e:
        logger.warning("radar: 新建模板失败: %r", e)
        raise HTTPException(502, f"新建检索模板失败：{e}")


@router.put("/configs/{cid}", response_model=RadarConfigOut)
def update_config(cid: int, payload: RadarConfigUpdate):
    data = payload.model_dump(exclude_unset=True)
    try:
        updated = _repo().update(cid, data)
    except Exception as e:
        logger.warning("radar: 更新模板失败: %r", e)
        raise HTTPException(502, f"更新检索模板失败：{e}")
    if not updated:
        raise HTTPException(404, "配置不存在")
    return updated


@router.delete("/configs/{cid}")
def delete_config(cid: int):
    try:
        ok = _repo().delete(cid)
    except Exception as e:
        logger.warning("radar: 删除模板失败: %r", e)
        raise HTTPException(502, f"删除检索模板失败：{e}")
    if not ok:
        raise HTTPException(404, "配置不存在")
    # IMA 无真删除，这里是软删除（重命名为【已删除】前缀）
    return {"ok": True, "soft_deleted": True}


def _library_keys() -> set:
    """论文库的全部身份标识（arxiv_id / original_url / url）。"""
    try:
        return PaperRepo().all_identity_keys()
    except Exception as e:
        # 拿不到就退化为"全部标为未收录"，不要让检索整体失败
        logger.warning("radar: 读取论文库标识失败，in_library 将全部为 false: %r", e)
        return set()


@router.post("/run")
async def run_search(
    payload: Optional[RadarRunIn] = None,
    type: str = "paper",
    keywords: str = "",
    field: str = "",
    days: int = 2,
    max_results: int = 30,
    lang: str = "en",
    channel: str = "google",
):
    """手动执行一次检索：仅检索论文或资讯其中一种。"""
    if payload is not None:
        type = payload.type
        keywords = payload.keywords
        field = payload.field
        days = payload.days
        max_results = payload.max_results
        lang = payload.lang
        channel = payload.channel

    try:
        if type == "news":
            news_out = await news.fetch_news(keywords, days, max_results, lang=lang, channel=channel)
            results = news_out["results"]
            sources = news_out.get("sources", [])
        else:
            results = await arxiv.fetch_papers(keywords, days, max_results)
            sources = []
    except httpx.HTTPError as e:
        logger.warning("radar fetch http error: %r", e)
        raise HTTPException(
            status_code=502,
            detail=(
                "检索源网络请求失败：无法访问 arXiv / Google News。"
                "请确认本机能联网（代理/防火墙可能拦截外网）；"
                f"错误信息：{e!r}"
            ),
        )
    except Exception as e:
        logger.warning("radar fetch error: %r", e)
        raise HTTPException(status_code=502, detail=f"检索失败：{e!r}")

    # 去重 + 标记已在库中（基于 IMA 论文库）
    existing = _library_keys()
    for r in results:
        key = r.get("arxiv_id") or r.get("url")
        r["in_library"] = bool(key and key in existing)
        if field:
            r["field"] = field
    return {
        "type": type,
        "count": len(results),
        "results": results,
        "sources": sources,
    }


@router.post("/run_template/{cid}")
async def run_template(cid: int):
    repo = _repo()
    try:
        c = repo.get(cid)
    except Exception as e:
        logger.warning("radar: 读取模板失败: %r", e)
        raise HTTPException(502, f"读取检索模板失败：{e}")
    if not c:
        raise HTTPException(404, "配置不存在")

    kw = c.get("keywords") or ""
    days = c.get("time_range_days") or 2
    lang = c.get("lang") or "en"
    channel = c.get("channel") or "google"

    try:
        if c.get("type") == "news":
            news_out = await news.fetch_news(kw, days, 30, lang=lang, channel=channel)
            results = news_out["results"]
            sources = news_out.get("sources", [])
        else:
            results = await arxiv.fetch_papers(kw, days, 30)
            sources = []
    except httpx.HTTPError as e:
        logger.warning("radar template fetch http error: %r", e)
        raise HTTPException(
            status_code=502,
            detail=(
                "检索源网络请求失败：无法访问 arXiv / Google News。"
                f"错误信息：{e!r}"
            ),
        )
    except Exception as e:
        logger.warning("radar template fetch error: %r", e)
        raise HTTPException(status_code=502, detail=f"检索失败：{e!r}")

    existing = _library_keys()
    for r in results:
        key = r.get("arxiv_id") or r.get("url")
        r["in_library"] = bool(key and key in existing)
        r["field"] = c.get("field") or ""
    return {
        "type": c.get("type"),
        "count": len(results),
        "results": results,
        "sources": sources,
    }


# ===========================================================================
# 带步骤进度的后台检索（前端轮询 /job/{job_id}）
# ===========================================================================
# 后台协程强引用，防止被 GC 中途回收
_BG_TASKS: list = []


def _job_steps(type_: str, channel: str) -> list:
    """按检索类型/渠道生成步骤列表 [(key, label), ...]。"""
    steps = [("lib", "读取论文库标识（IMA）")]
    if type_ == "news":
        if channel in ("google", "all"):
            steps.append(("google", "检索 Google News"))
        if channel in ("cn", "all"):
            steps.append(("rss", "抓取国内媒体 RSS"))
            steps.append(("wx", "抓取微信公众号（RSSHub）"))
    else:
        steps.append(("search", "检索 arXiv（多镜像自动回退）"))
    steps.append(("merge", "合并去重 · 标记已在库"))
    return steps


async def _run_job(job_id: str, *, type_: str, keywords: str, field: str,
                   days: int, max_results: int, lang: str, channel: str):
    """后台执行检索，逐步骤上报状态，结束写 result / error。"""
    try:
        radar_jobs.set_step(job_id, "lib", "running")
        existing = _library_keys()
        radar_jobs.set_step(job_id, "lib", "ok", f"论文库标识 {len(existing)} 条")

        if type_ == "news":
            radar_jobs.set_step(job_id, "merge", "running", "等待各资讯源返回")
            out = await news.fetch_news(
                keywords, days, max_results, lang=lang, channel=channel,
                progress=radar_jobs.make_progress_cb(job_id))
            results = out["results"]
            sources = out.get("sources", [])
        else:
            radar_jobs.set_step(job_id, "search", "running")
            results = await arxiv.fetch_papers(keywords, days, max_results)
            radar_jobs.set_step(job_id, "search", "ok", f"命中 {len(results)} 篇")
            sources = []

        radar_jobs.set_step(job_id, "merge", "running")
        in_lib = 0
        for r in results:
            key = r.get("arxiv_id") or r.get("url")
            r["in_library"] = bool(key and key in existing)
            if r["in_library"]:
                in_lib += 1
            if field:
                r["field"] = field
        radar_jobs.set_step(
            job_id, "merge", "ok",
            f"共 {len(results)} 条，其中已在库 {in_lib} 条")
        radar_jobs.finish_job(job_id, result={
            "type": type_,
            "count": len(results),
            "results": results,
            "sources": sources,
        })
    except httpx.HTTPError as e:
        logger.warning("radar bg job http error: %r", e)
        radar_jobs.finish_job(job_id, error=(
            "检索源网络请求失败：无法访问 arXiv / Google News。"
            "请确认本机能联网（代理/防火墙可能拦截外网）；"
            f"错误信息：{e!r}"))
    except Exception as e:
        logger.warning("radar bg job error: %r", e)
        radar_jobs.finish_job(job_id, error=f"检索失败：{e!r}")


@router.post("/run_bg")
async def run_search_bg(payload: RadarRunIn):
    """手动执行一次检索（后台 + 步骤进度）。立即返回 job_id。"""
    job = radar_jobs.create_job(
        meta={"type": payload.type, "keywords": payload.keywords},
        steps=_job_steps(payload.type, payload.channel))
    t = asyncio.create_task(_run_job(
        job["job_id"], type_=payload.type, keywords=payload.keywords,
        field=payload.field, days=payload.days,
        max_results=payload.max_results, lang=payload.lang,
        channel=payload.channel))
    _BG_TASKS.append(t)
    return {"job_id": job["job_id"], "steps": job["steps"]}


@router.post("/run_template_bg/{cid}")
async def run_template_bg(cid: int):
    """按模板执行一次检索（后台 + 步骤进度）。立即返回 job_id。"""
    repo = _repo()
    try:
        c = repo.get(cid)
    except Exception as e:
        logger.warning("radar: 读取模板失败: %r", e)
        raise HTTPException(502, f"读取检索模板失败：{e}")
    if not c:
        raise HTTPException(404, "配置不存在")

    type_ = c.get("type") or "paper"
    channel = c.get("channel") or "google"
    job = radar_jobs.create_job(
        meta={"type": type_, "keywords": c.get("keywords") or "",
              "template": c.get("name")},
        steps=_job_steps(type_, channel))
    t = asyncio.create_task(_run_job(
        job["job_id"], type_=type_, keywords=c.get("keywords") or "",
        field=c.get("field") or "", days=c.get("time_range_days") or 2,
        max_results=30, lang=c.get("lang") or "en", channel=channel))
    _BG_TASKS.append(t)
    return {"job_id": job["job_id"], "steps": job["steps"]}


@router.get("/job/{job_id}")
def get_job(job_id: str):
    """轮询检索任务：steps 每步状态；done 时带 result，failed 时带 error。"""
    job = radar_jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已过期（服务可能重启过），请重新检索")
    return job
