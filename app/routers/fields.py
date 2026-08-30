"""研究领域 / 分类管理。

数据源：IMA 知识库（ResearchBench-ima/metadata/fields.md），
读写一律走 app.services.ima_store；本地 SQLite 只是派生缓存。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.ima_client import IMAError
from ..services import ima_store

router = APIRouter(prefix="/api/fields", tags=["fields"])


class FieldCreate(BaseModel):
    name: str


class FieldOut(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


@router.get("", response_model=list[FieldOut])
def list_fields():
    return ima_store.fields.list()


@router.post("", response_model=FieldOut)
def create_field(payload: FieldCreate):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "领域名称不能为空")
    try:
        return ima_store.fields.create({"name": name})
    except IMAError as e:
        raise HTTPException(502, f"写入 IMA 失败：{e}")


@router.delete("/{fid}")
def delete_field(fid: int):
    # IMA 无删除接口：整表重写（归档旧 fields.md 后上传新版本）
    try:
        if not ima_store.fields.soft_delete(fid):
            raise HTTPException(404, "领域不存在")
    except IMAError as e:
        raise HTTPException(502, f"写入 IMA 失败：{e}")
    return {"ok": True}
