"""
论文原文资产（PDF + LaTeX 源码）抓取。

单独放在这个 router 里，不动 app/routers/papers.py：papers.py 已经很长，
而且它混用了本地 ORM（TeX/飞书相关）与 IMA 两套数据源。

接口是 POST 且会往 IMA 知识库写文件、改写论文的 front-matter，
属于"改数据"，因此**需要管理员密码**（admin_guard 只对 GET 与白名单
里的检索类 POST 放行，本路径不在白名单，默认受保护）。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..services.arxiv_assets import ensure_paper_assets

router = APIRouter(prefix="/api/papers", tags=["assets"])


@router.post("/{pid}/fetch_assets")
def fetch_assets(
    pid: int,
    tag: Optional[str] = Query(
        None, description="归档标签（文件夹名）；不传则用论文主标签/领域"),
    with_source: bool = Query(True, description="是否一并抓取 LaTeX 源码"),
):
    """
    为某篇论文抓取 arXiv 原文 PDF 与 LaTeX 源码，上传到 IMA 知识库，
    并把 ima_pdf_path / ima_tex_path 写回论文的 front-matter。
    """
    result = ensure_paper_assets(pid, tag=tag, with_source=with_source)

    code = result.get("code")
    if code == "no_arxiv_id":
        raise HTTPException(400, result.get("reason")
                            or "该论文没有 arXiv ID，无法抓取")
    if code == "not_found":
        raise HTTPException(404, result.get("reason") or "论文不存在")
    if code != "ok":
        raise HTTPException(502, result.get("reason") or "抓取失败")

    return {
        "ok": True,
        "pdf_path": result.get("pdf_path") or "",
        "tex_path": result.get("tex_path") or "",
        "reason": result.get("reason") or "",
    }
