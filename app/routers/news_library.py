"""资讯库：沉淀来自 Research Radar 的 AI 资讯。只读沉淀，无修改功能。"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import NewsItem

router = APIRouter(prefix="/api/news", tags=["news_library"])


class NewsCreate(BaseModel):
    title: str
    url: str = ""
    source: str = ""
    published: str = ""
    summary: str = ""
    field: str = ""
    note: str = ""


class NewsBulk(BaseModel):
    items: list[NewsCreate]


class NewsOut(BaseModel):
    id: int
    title: str
    url: str
    source: str
    published: str
    summary: str
    field: str
    note: str
    created_at: Optional[str] = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_safe(cls, n: NewsItem):
        d = {
            "id": n.id, "title": n.title, "url": n.url, "source": n.source,
            "published": n.published, "summary": n.summary, "field": n.field,
            "note": n.note,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        return cls(**d)


@router.get("")
def list_news(q: str = "", source: str = "", page: int = 1, page_size: int = 12, db: Session = Depends(get_db)):
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    query = db.query(NewsItem)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (NewsItem.title.ilike(like)) | (NewsItem.summary.ilike(like))
        )
    if source:
        query = query.filter(NewsItem.source == source)
    total = query.count()
    pages = (total + page_size - 1) // page_size if total else 0
    items = (
        query.order_by(NewsItem.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [NewsOut.from_orm_safe(n) for n in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post("", response_model=NewsOut)
def add_news(payload: NewsCreate, db: Session = Depends(get_db)):
    # 按 url 去重（有 url 时）
    if payload.url:
        exist = db.query(NewsItem).filter(NewsItem.url == payload.url).first()
        if exist:
            return NewsOut.from_orm_safe(exist)
    n = NewsItem(**payload.model_dump())
    db.add(n); db.commit(); db.refresh(n)
    return NewsOut.from_orm_safe(n)


@router.post("/bulk", response_model=dict)
def add_news_bulk(payload: NewsBulk, db: Session = Depends(get_db)):
    added = 0
    existing_urls = {u for (u,) in db.query(NewsItem.url).all()}
    for it in payload.items:
        if it.url and it.url in existing_urls:
            continue
        n = NewsItem(**it.model_dump())
        db.add(n); added += 1
        if it.url:
            existing_urls.add(it.url)
    db.commit()
    return {"added": added, "total": len(payload.items)}


@router.delete("/{nid}")
def delete_news(nid: int, db: Session = Depends(get_db)):
    n = db.query(NewsItem).filter(NewsItem.id == nid).first()
    if not n:
        raise HTTPException(404, "资讯不存在")
    db.delete(n); db.commit()
    return {"ok": True}
