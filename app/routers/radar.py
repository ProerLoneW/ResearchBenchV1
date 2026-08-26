"""Research Radar：定时/手动发现最新论文与 AI 资讯。"""
import logging
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import RadarConfig, Paper
from ..config import DEFAULT_RADAR_TEMPLATES
from ..services import arxiv, news

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


@router.get("/configs", response_model=list[RadarConfigOut])
def list_configs(db: Session = Depends(get_db)):
    return db.query(RadarConfig).order_by(RadarConfig.created_at.desc()).all()


@router.post("/configs", response_model=RadarConfigOut)
def create_config(payload: RadarConfigCreate, db: Session = Depends(get_db)):
    c = RadarConfig(**payload.model_dump())
    db.add(c); db.commit(); db.refresh(c)
    return c


@router.put("/configs/{cid}", response_model=RadarConfigOut)
def update_config(cid: int, payload: RadarConfigUpdate, db: Session = Depends(get_db)):
    c = db.query(RadarConfig).filter(RadarConfig.id == cid).first()
    if not c:
        raise HTTPException(404, "配置不存在")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    db.commit(); db.refresh(c)
    return c


@router.delete("/configs/{cid}")
def delete_config(cid: int, db: Session = Depends(get_db)):
    c = db.query(RadarConfig).filter(RadarConfig.id == cid).first()
    if not c:
        raise HTTPException(404, "配置不存在")
    db.delete(c); db.commit()
    return {"ok": True}


@router.post("/run")
async def run_search(
    type: str = "paper",
    keywords: str = "",
    field: str = "",
    days: int = 2,
    max_results: int = 30,
    lang: str = "en",
    channel: str = "google",
    db: Session = Depends(get_db),
):
    """手动执行一次检索：仅检索论文或资讯其中一种。"""
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
            )
        )
    except Exception as e:
        logger.warning("radar fetch error: %r", e)
        raise HTTPException(status_code=502, detail=f"检索失败：{e!r}")

    # 去重 + 标记已在库中
    existing = set()
    for p in db.query(Paper).all():
        if p.arxiv_id:
            existing.add(p.arxiv_id)
        if p.original_url:
            existing.add(p.original_url)
    for r in results:
        key = r.get("arxiv_id") or r.get("url")
        r["in_library"] = bool(key and key in existing)
        if field and "field" not in r:
            r["field"] = field
        elif field:
            r["field"] = field
    return {
        "type": type,
        "count": len(results),
        "results": results,
        "sources": sources,
    }


@router.post("/run_template/{cid}")
async def run_template(cid: int, db: Session = Depends(get_db)):
    c = db.query(RadarConfig).filter(RadarConfig.id == cid).first()
    if not c:
        raise HTTPException(404, "配置不存在")
    kw = c.keywords
    days = c.time_range_days
    lang = getattr(c, "lang", None) or "en"
    channel = getattr(c, "channel", None) or "google"
    if c.type == "news":
        news_out = await news.fetch_news(kw, days, 30, lang=lang, channel=channel)
        results = news_out["results"]
        sources = news_out.get("sources", [])
    else:
        results = await arxiv.fetch_papers(kw, days, 30)
        sources = []
    # 复用 run_search 的去重/标记逻辑
    existing = set()
    for p in db.query(Paper).all():
        if p.arxiv_id:
            existing.add(p.arxiv_id)
        if p.original_url:
            existing.add(p.original_url)
    for r in results:
        key = r.get("arxiv_id") or r.get("url")
        r["in_library"] = bool(key and key in existing)
        r["field"] = c.field
    return {"type": c.type, "count": len(results), "results": results, "sources": sources}
