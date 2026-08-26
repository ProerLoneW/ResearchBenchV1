"""研究领域 / 分类管理。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Field

router = APIRouter(prefix="/api/fields", tags=["fields"])


class FieldCreate(BaseModel):
    name: str


class FieldOut(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


@router.get("", response_model=list[FieldOut])
def list_fields(db: Session = Depends(get_db)):
    return db.query(Field).order_by(Field.name).all()


@router.post("", response_model=FieldOut)
def create_field(payload: FieldCreate, db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "领域名称不能为空")
    if db.query(Field).filter(Field.name == name).first():
        raise HTTPException(400, "领域已存在")
    f = Field(name=name)
    db.add(f); db.commit(); db.refresh(f)
    return f


@router.delete("/{fid}")
def delete_field(fid: int, db: Session = Depends(get_db)):
    f = db.query(Field).filter(Field.id == fid).first()
    if not f:
        raise HTTPException(404, "领域不存在")
    db.delete(f); db.commit()
    return {"ok": True}
