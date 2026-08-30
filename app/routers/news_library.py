"""资讯库：沉淀来自 Research Radar 的 AI 资讯。

数据源：IMA 知识库（ResearchBench-ima/metadata/news/*.md），
读写一律走 app.services.ima_store；本地 SQLite 只是派生缓存。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.ima_client import IMAError
from ..services import ima_store

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
    def from_dict(cls, d: dict):
        """把 ima_store 的记录转成响应结构（created_at 统一成 ISO 字符串）。"""
        created = d.get("created_at")
        return cls(
            id=d.get("id"),
            title=d.get("title") or "",
            url=d.get("url") or "",
            source=d.get("source") or "",
            published=d.get("published") or "",
            summary=d.get("summary") or "",
            field=d.get("field") or "",
            note=d.get("note") or "",
            created_at=created.isoformat() if created else None,
        )


def _stale_note() -> dict:
    """IMA 不可用时带上降级提示。额外字段不影响既有分页结构。"""
    if not ima_store.news.state.stale:
        return {}
    return {
        "stale": True,
        "warning": "IMA 知识库暂时不可用，当前展示的是本地缓存快照，数据可能不是最新。",
    }


@router.get("")
def list_news(q: str = "", source: str = "", page: int = 1, page_size: int = 12):
    result = ima_store.news.list(query=q, source=source or None,
                                 page=page, page_size=page_size)
    result["items"] = [NewsOut.from_dict(d) for d in result["items"]]
    result.update(_stale_note())
    return result


@router.post("", response_model=NewsOut)
def add_news(payload: NewsCreate):
    # 按 url 去重（有 url 时）
    if payload.url:
        exist = ima_store.news.find_by_url(payload.url)
        if exist:
            return NewsOut.from_dict(exist)
    try:
        rec = ima_store.news.create(payload.model_dump())
    except IMAError as e:
        raise HTTPException(502, f"写入 IMA 失败：{e}")
    return NewsOut.from_dict(rec)


@router.post("/bulk", response_model=dict)
def add_news_bulk(payload: NewsBulk):
    try:
        added = ima_store.news.create_many(
            [it.model_dump() for it in payload.items])
    except IMAError as e:
        raise HTTPException(502, f"写入 IMA 失败：{e}")
    return {"added": added, "total": len(payload.items)}


@router.delete("/{nid}")
def delete_news(nid: int):
    # IMA 没有删除接口，只能重命名加【已删除】前缀，由读取侧过滤
    if not ima_store.news.soft_delete(nid):
        raise HTTPException(404, "资讯不存在")
    return {"ok": True}
