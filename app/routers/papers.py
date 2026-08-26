"""论文库：CRUD、搜索、筛选、元数据自动获取、一键收录。"""
import os
import re
import sys
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..config import DATA_DIR
from ..models import Paper, Field
from ..services.metadata import fetch_metadata
from ..services.feishu import FeishuClient

ROOT_DIR = Path(__file__).resolve().parent.parent.parent  # 项目根目录
TEX_REPOS_DIR = DATA_DIR / "tex_repos"

router = APIRouter(prefix="/api/papers", tags=["papers"])


class PaperCreate(BaseModel):
    title: str
    abstract: str = ""
    field_id: Optional[int] = None
    tags: str = ""
    original_url: str = ""
    github_url: str = ""
    feishu_doc_url: str = ""
    summary: str = ""
    reading_status: str = "unread"
    arxiv_id: str = ""
    source: str = "manual"


class PaperUpdate(BaseModel):
    title: Optional[str] = None
    abstract: Optional[str] = None
    field_id: Optional[int] = None
    tags: Optional[str] = None
    original_url: Optional[str] = None
    github_url: Optional[str] = None
    feishu_doc_url: Optional[str] = None
    summary: Optional[str] = None
    tex_repo_path: Optional[str] = None
    reading_status: Optional[str] = None


class PaperOut(BaseModel):
    id: int
    title: str
    abstract: str
    field_id: Optional[int]
    field_name: Optional[str] = None
    tags: str
    original_url: str
    github_url: str
    feishu_doc_url: str
    summary: str
    tex_repo_path: str = ""
    reading_status: str
    arxiv_id: str
    source: str
    favorited_at: Optional[datetime]
    read_at: Optional[datetime]
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


def _serialize(p: Paper, field_name: Optional[str]) -> dict:
    d = PaperOut.model_validate(p).model_dump()
    d["field_name"] = field_name
    return d


@router.get("")
def list_papers(
    q: Optional[str] = None,
    field_id: Optional[int] = None,
    status: Optional[str] = None,
    tag: Optional[str] = None,
    page: int = 1,
    page_size: int = 12,
    db: Session = Depends(get_db),
):
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    query = db.query(Paper)
    if field_id is not None:
        query = query.filter(Paper.field_id == field_id)
    if status:
        query = query.filter(Paper.reading_status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Paper.title.ilike(like)) | (Paper.abstract.ilike(like))
            | (Paper.summary.ilike(like)) | (Paper.tags.ilike(like))
        )
    if tag:
        query = query.filter(Paper.tags.ilike(f"%{tag}%"))
    total = query.count()
    pages = (total + page_size - 1) // page_size if total else 0
    papers = (
        query.order_by(Paper.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    fields = {f.id: f.name for f in db.query(Field).all()}
    return {
        "items": [_serialize(p, fields.get(p.field_id)) for p in papers],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get("/fetch_metadata")
async def fetch_meta_get(url: str = ""):
    """根据链接自动获取标题/摘要等公开信息（GET，查询参数 url）。"""
    if not url.strip():
        raise HTTPException(400, "请提供论文链接")
    try:
        meta = await fetch_metadata(url)
    except Exception as e:
        raise HTTPException(502, f"获取失败：{e}")
    if not meta:
        return {"found": False}
    return {"found": True, **meta}


@router.get("/{pid}", response_model=PaperOut)
def get_paper(pid: int, db: Session = Depends(get_db)):
    p = db.query(Paper).filter(Paper.id == pid).first()
    if not p:
        raise HTTPException(404, "论文不存在")
    field = db.query(Field).filter(Field.id == p.field_id).first()
    return _serialize(p, field.name if field else None)


@router.post("", response_model=PaperOut)
async def create_paper(payload: PaperCreate, db: Session = Depends(get_db)):
    if not payload.title.strip():
        raise HTTPException(400, "标题不能为空")
    p = Paper(**payload.model_dump())
    if payload.reading_status == "read" and not p.read_at:
        p.read_at = datetime.now()
    if not p.favorited_at:
        p.favorited_at = datetime.now()
    db.add(p); db.commit(); db.refresh(p)
    field = db.query(Field).filter(Field.id == p.field_id).first()
    return _serialize(p, field.name if field else None)


@router.put("/{pid}", response_model=PaperOut)
def update_paper(pid: int, payload: PaperUpdate, db: Session = Depends(get_db)):
    p = db.query(Paper).filter(Paper.id == pid).first()
    if not p:
        raise HTTPException(404, "论文不存在")
    data = payload.model_dump(exclude_unset=True)
    if "reading_status" in data and data["reading_status"] == "read" and not p.read_at:
        p.read_at = datetime.now()
    for k, v in data.items():
        setattr(p, k, v)
    db.commit(); db.refresh(p)
    field = db.query(Field).filter(Field.id == p.field_id).first()
    return _serialize(p, field.name if field else None)


@router.delete("/{pid}")
def delete_paper(pid: int, db: Session = Depends(get_db)):
    p = db.query(Paper).filter(Paper.id == pid).first()
    if not p:
        raise HTTPException(404, "论文不存在")
    db.delete(p); db.commit()
    return {"ok": True}


@router.post("/from_radar")
async def add_from_radar(items: list[dict], db: Session = Depends(get_db)):
    """Research Radar 结果一键收录（去重）。"""
    created = 0
    for it in items:
        url = (it.get("url") or "").strip()
        aid = (it.get("arxiv_id") or "").strip()
        exists = None
        if aid:
            exists = db.query(Paper).filter(Paper.arxiv_id == aid).first()
        if not exists and url:
            exists = db.query(Paper).filter(Paper.original_url == url).first()
        if exists:
            continue
        title = it.get("title", "未命名")
        field_name = it.get("field") or ""
        field_id = None
        if field_name:
            f = db.query(Field).filter(Field.name == field_name).first()
            if not f:
                f = Field(name=field_name); db.add(f); db.commit(); db.refresh(f)
            field_id = f.id
        p = Paper(
            title=title,
            abstract=it.get("abstract", ""),
            field_id=field_id,
            original_url=url,
            github_url=it.get("github", ""),
            arxiv_id=aid,
            source="radar",
            reading_status="unread",
            favorited_at=datetime.now(),
        )
        db.add(p); created += 1
    db.commit()
    return {"created": created}


class GenerateFeishuIn(BaseModel):
    tex_repo_path: str


# ---------- TeX 仓库文件夹上传 ----------
@router.post("/{pid}/upload_tex")
async def upload_tex(pid: int, files: list[UploadFile] = File(...),
                     db: Session = Depends(get_db)):
    """
    上传 TeX 仓库文件夹（前端用 <input webkitdirectory> 多选文件）。
    保留相对目录结构保存到 data/tex_repos/{pid}/，并记录路径到论文卡片。
    """
    p = db.query(Paper).filter(Paper.id == pid).first()
    if not p:
        raise HTTPException(404, "论文不存在")
    dest = TEX_REPOS_DIR / str(pid)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    saved = 0
    for f in files:
        # 飞书/浏览器上传的相对路径在 filename 中（可能含 / 或 \\）
        rel = (f.filename or "file").replace("\\", "/")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = await f.read()
            target.write_bytes(content)
            saved += 1
        except Exception:
            continue
    if saved == 0:
        raise HTTPException(400, "未接收到任何文件")
    p.tex_repo_path = str(dest)
    db.commit()
    return {"ok": True, "path": str(dest), "files": saved}


# ---------- 笔记/心得：导入 Markdown（.md 上传 或 飞书云文档链接） ----------
class ImportMarkdownIn(BaseModel):
    markdown: Optional[str] = None     # 直接传入 md 文本（如上传 .md 解析后）
    feishu_url: Optional[str] = None   # 或传入飞书文档链接，后端拉取并转 md


@router.post("/{pid}/import_md")
async def import_markdown(pid: int, payload: ImportMarkdownIn,
                          db: Session = Depends(get_db)):
    """
    导入笔记/心得的 Markdown 来源：
      - markdown: 前端读取 .md 文件内容后直接提交；
      - feishu_url: 后端用已授权飞书账号拉取云文档并转为 Markdown。
    返回解析后的 markdown 文本，前端填入编辑器/保存。
    """
    p = db.query(Paper).filter(Paper.id == pid).first()
    if not p:
        raise HTTPException(404, "论文不存在")

    if payload.feishu_url and payload.feishu_url.strip():
        try:
            client = FeishuClient()
            md = client.fetch_document_markdown(payload.feishu_url.strip())
        except Exception as e:
            raise HTTPException(502, f"飞书文档读取失败：{e}")
        if not md.strip():
            raise HTTPException(502, "飞书文档内容为空或读取失败")
        return {"markdown": md, "source": "feishu"}
    if payload.markdown is not None:
        return {"markdown": payload.markdown, "source": "file"}
    raise HTTPException(400, "请提供 markdown 文本或飞书文档链接")


# ---------- 生成飞书文档：优先使用已上传的 TeX 仓库 ----------
@router.post("/{pid}/generate_feishu")
async def generate_feishu(pid: int, payload: Optional[GenerateFeishuIn] = None,
                          db: Session = Depends(get_db)):
    """
    调用独立 TeX→飞书 工具，把已翻译的中文 TeX 仓库写入同名飞书云文档，
    并将返回链接保存回论文卡片。
    优先使用已上传到 data/tex_repos/{id} 的文件夹；否则取请求体中的 tex_repo_path。
    """
    p = db.query(Paper).filter(Paper.id == pid).first()
    if not p:
        raise HTTPException(404, "论文不存在")
    repo = ""
    if payload and payload.tex_repo_path:
        repo = payload.tex_repo_path.strip()
    if not repo and p.tex_repo_path:
        repo = p.tex_repo_path.strip()
    # 兜底：使用已上传目录
    uploaded = TEX_REPOS_DIR / str(pid)
    if not repo and uploaded.exists():
        repo = str(uploaded)
    if not repo or not os.path.isdir(repo):
        raise HTTPException(400, "请先上传 TeX 仓库文件夹，或提供有效的 TeX 仓库路径")
    script = ROOT_DIR / "tex2feishu.py"
    if not script.exists():
        raise HTTPException(500, "未找到 tex2feishu.py")
    try:
        proc = subprocess.run(
            [sys.executable, str(script), repo, "--title", p.title or "论文"],
            capture_output=True, text=True, cwd=str(ROOT_DIR), timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "生成超时（请检查是否需要浏览器授权飞书）")
    if proc.returncode != 0:
        raise HTTPException(502, f"生成失败：{proc.stderr.strip()[-500:]}")
    # 从 stdout 解析最终 URL（脚本仅打印 URL）
    url = ""
    for line in reversed(proc.stdout.splitlines()):
        if "feishu.cn" in line or line.strip().startswith("http"):
            url = line.strip()
            break
    if not url:
        raise HTTPException(502, "未获取到飞书文档链接")
    p.feishu_doc_url = url
    db.commit()
    return {"url": url}
