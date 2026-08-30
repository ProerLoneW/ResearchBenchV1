#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 ResearchBench 现有数据原样迁移到 IMA 知识库。

目标结构（在个人知识库根目录下）：

    ResearchBench-ima/
    ├── metadata/                 网页展示数据（每篇一份 Markdown）
    │   ├── 0001-论文标题.md
    │   ├── news/0001-资讯标题.md
    │   └── fields.md
    ├── {标签}/                    按论文主标签分文件夹
    │   └── 0001-论文标题/
    │       ├── 0001-论文标题.pdf          （本地有才传）
    │       └── tex_source/                （本地有才传，解压后的源码树）
    ├── _archive/                  更新时归档旧版本（IMA 无原地更新接口）
    └── _trash/                    删除时移入（IMA 无删除接口）

说明：
  - IMA 不接受 .json，因此结构化字段用 Markdown 的 YAML front-matter 承载。
  - 一个论文可能有多个标签，这里按**主标签**（第一个）归文件夹，避免同一份
    PDF 在多个标签目录下重复；全部标签仍完整记录在 metadata 里。
  - 脚本可重复执行：已存在的 metadata 文件会跳过，不会产生重复条目。

用法：
    python migrate_to_ima.py            # 真实迁移
    python migrate_to_ima.py --dry-run  # 只预览，不上传
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db import SessionLocal                      # noqa: E402
from app.models import Field, NewsItem, Paper        # noqa: E402
from app.services.ima_client import (                # noqa: E402
    IMAClient, IMAError, MT_FOLDER, get_client, UNSUPPORTED_EXTS,
)

ROOT_FOLDER = "ResearchBench-ima"
METADATA = "metadata"
ARCHIVE = "_archive"
TRASH = "_trash"
TEX_SUBFOLDER = "tex_source"

# 上传 tex 源码时允许一并上传的扩展名（其余跳过并统计）
TEX_ALLOWED = {".tex", ".bib", ".bbl", ".sty", ".cls", ".bst", ".txt", ".md"}
IMG_ALLOWED = {".png", ".jpg", ".jpeg", ".webp"}


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def safe_name(s: str, limit: int = 60) -> str:
    """把标题转成 IMA/文件系统安全的名字。"""
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]", " ", str(s or "")).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:limit].strip() or "untitled")


def prefix(pid: int, title: str) -> str:
    return f"{pid:04d}-{safe_name(title)}"


def parse_tags(raw: Any) -> List[str]:
    """兼容 JSON 数组 / 逗号 / 顿号 / 竖线分隔的标签字段。"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return [str(t).strip() for t in v if str(t).strip()]
        except Exception:
            pass
    return [t.strip() for t in re.split(r"[,，、|]", s) if t.strip()]


def yaml_dump(data: Dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False).strip()


def fmt_dt(v: Any) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    return str(v)


def build_paper_md(p: Paper, field_name: str, paths: Dict[str, str]) -> str:
    fm = {
        "id": p.id,
        "type": "paper",
        "title": p.title or "",
        "arxiv_id": p.arxiv_id or "",
        "original_url": p.original_url or "",
        "github_url": p.github_url or "",
        "feishu_doc_url": p.feishu_doc_url or "",
        "field": field_name,
        "field_id": p.field_id,
        "tags": parse_tags(p.tags),
        "reading_status": p.reading_status or "unread",
        "source": p.source or "",
        "ima_pdf_path": paths.get("pdf", ""),
        "ima_tex_path": paths.get("tex", ""),
        "ima_zh_pdf_path": paths.get("zh_pdf", ""),
        "created_at": fmt_dt(p.created_at),
        "updated_at": fmt_dt(p.updated_at),
        "read_at": fmt_dt(p.read_at),
        "favorited_at": fmt_dt(p.favorited_at),
    }
    body = [
        f"# {p.title or '(无标题)'}",
        "",
        "## 摘要",
        (p.abstract or "（无摘要）").strip(),
        "",
        "## 笔记 / 心得",
        (p.summary or "（暂无笔记）").strip(),
        "",
    ]
    return f"---\n{yaml_dump(fm)}\n---\n\n" + "\n".join(body) + "\n"


def build_news_md(n: NewsItem) -> str:
    fm = {
        "id": n.id,
        "type": "news",
        "title": n.title or "",
        "source": getattr(n, "source", "") or "",
        "url": getattr(n, "url", "") or "",
        "published": fmt_dt(getattr(n, "published_at", None)),
        "reading_status": getattr(n, "reading_status", "") or "unread",
        "created_at": fmt_dt(getattr(n, "created_at", None)),
    }
    body = [
        f"# {n.title or '(无标题)'}",
        "",
        (getattr(n, "summary", "") or "（无摘要）").strip(),
        "",
    ]
    return f"---\n{yaml_dump(fm)}\n---\n\n" + "\n".join(body) + "\n"


def build_fields_md(fields: List[Field]) -> str:
    fm = {"type": "fields", "count": len(fields),
          "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    rows = ["| id | 名称 | 颜色 |", "| --- | --- | --- |"]
    for f in fields:
        rows.append(f"| {f.id} | {f.name} | {getattr(f, 'color', '') or ''} |")
    return (f"---\n{yaml_dump(fm)}\n---\n\n# 领域 / 标签\n\n"
            + "\n".join(rows) + "\n")


# ----------------------------------------------------------------------
# 迁移
# ----------------------------------------------------------------------
def upload_tree(client: IMAClient, kb: str, local_dir: Path,
                ima_folder_id: str, stats: Dict[str, int],
                dry: bool) -> int:
    """把本地目录树上传为 IMA 的 tex_source 文件夹（保留相对结构）。"""
    uploaded = 0
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext in UNSUPPORTED_EXTS or ext not in (TEX_ALLOWED | IMG_ALLOWED
                                                  | {".pdf"}):
            stats["skipped_ext"] += 1
            continue
        rel_parent = path.parent.relative_to(local_dir)
        target_folder = ima_folder_id
        parts = [p for p in rel_parent.parts if p not in (".", "..", "")]
        if parts:
            # 在 tex_source 下按原目录相对结构建子文件夹
            sub = _ensure_sub(client, kb, ima_folder_id, parts, dry)
            if not sub:
                if dry:
                    # 预览模式不建目录，直接用父目录 id 继续统计
                    sub = ima_folder_id
                else:
                    stats["skipped_ext"] += 1
                    continue
            target_folder = sub
        if dry:
            stats["would_upload"] += 1
            uploaded += 1
            continue
        try:
            client.upload_file(kb, path, folder_id=target_folder)
            uploaded += 1
            stats["uploaded_files"] += 1
        except IMAError as e:
            stats["errors"] += 1
            print(f"    ! 上传失败 {path.name}: {str(e)[:100]}")
    return uploaded


_SUB_CACHE: Dict[str, str] = {}


def _ensure_sub(client: IMAClient, kb: str, parent_id: str,
                parts: List[str], dry: bool) -> Optional[str]:
    # 预览模式：不建任何目录，也不发查询请求，直接复用父 id
    if dry:
        return parent_id
    cur = parent_id
    walked: List[str] = []
    for part in parts:
        walked.append(part)
        key = f"{parent_id}::{'/'.join(walked)}"
        if key in _SUB_CACHE:
            cur = _SUB_CACHE[key]
            continue
        fid = client.find_child_folder(kb, part, cur)
        if not fid and not dry:
            fid = client.create_folder(kb, part, cur)
        if not fid:
            return None
        _SUB_CACHE[key] = fid
        cur = fid
    return cur


def main() -> int:
    ap = argparse.ArgumentParser(description="迁移 ResearchBench 数据到 IMA")
    ap.add_argument("--dry-run", action="store_true", help="只预览不上传")
    ap.add_argument("--no-files", action="store_true",
                    help="只迁 metadata，不上传 pdf/tex 文件")
    args = ap.parse_args()

    dry = args.dry_run
    stats = {"papers": 0, "news": 0, "fields": 0, "uploaded_files": 0,
             "would_upload": 0, "skipped_ext": 0, "errors": 0,
             "skipped_exists": 0}

    client = get_client()
    kb = client.knowledge_base_id
    print(f"目标知识库: {kb}")
    print(f"模式: {'预览(dry-run)' if dry else '真实迁移'}\n")

    root_id = client.ensure_path(kb, [ROOT_FOLDER])
    meta_id = client.ensure_path(kb, [ROOT_FOLDER, METADATA])
    client.ensure_path(kb, [ROOT_FOLDER, ARCHIVE])
    client.ensure_path(kb, [ROOT_FOLDER, TRASH])
    print(f"目录就绪: {ROOT_FOLDER}/ (metadata/_archive/_trash)\n")

    existing_meta = set()
    if not dry:
        for it in client.list_folder(kb, meta_id):
            existing_meta.add(it.get("title") or it.get("name") or "")

    db = SessionLocal()
    try:
        fields = db.query(Field).order_by(Field.id).all()
        field_map = {f.id: f.name for f in fields}
        papers = db.query(Paper).order_by(Paper.id).all()

        # ---------- 领域 ----------
        fname = "fields.md"
        if fname in existing_meta:
            stats["skipped_exists"] += 1
            print(f"  跳过(已存在) {fname}")
        elif dry:
            print(f"  [预览] 将上传 {fname}（{len(fields)} 个领域）")
            stats["fields"] = len(fields)
        else:
            client.upload_text(kb, fname, build_fields_md(fields),
                               folder_id=meta_id)
            stats["fields"] = len(fields)
            print(f"  ✓ {fname}（{len(fields)} 个领域）")

        # ---------- 论文 ----------
        print(f"\n迁移论文（共 {len(papers)} 篇）:")
        for p in papers:
            base = prefix(p.id, p.title)
            tags = parse_tags(p.tags)
            primary = tags[0] if tags else "未分类"

            pdf_path = tex_path = ""
            if not args.no_files and p.tex_repo_path:
                local = Path(p.tex_repo_path)
                if local.is_dir():
                    # 注意：预览模式不建任何文件夹，只给一个占位 id 供统计
                    if dry:
                        paper_fid = "DRY_RUN"
                    else:
                        client.ensure_path(kb, [ROOT_FOLDER, primary])
                        paper_fid = client.ensure_path(
                            kb, [ROOT_FOLDER, primary, base])
                    for pdf in sorted(local.rglob("*.pdf")):
                        if dry:
                            stats["would_upload"] += 1
                            pdf_path = f"{ROOT_FOLDER}/{primary}/{base}/{base}.pdf"
                        else:
                            try:
                                client.upload_file(
                                    kb, pdf, folder_id=paper_fid,
                                    upload_name=f"{base}.pdf")
                                stats["uploaded_files"] += 1
                                pdf_path = (f"{ROOT_FOLDER}/{primary}/"
                                            f"{base}/{base}.pdf")
                            except IMAError as e:
                                stats["errors"] += 1
                                print(f"    ! PDF 失败: {str(e)[:80]}")
                        break
                    tex_fid = _ensure_sub(client, kb, paper_fid,
                                          [TEX_SUBFOLDER], dry)
                    # 预览模式下子文件夹不会真的创建，仍要统计将要上传的文件数
                    if tex_fid or dry:
                        upload_tree(client, kb, local, tex_fid or paper_fid,
                                    stats, dry)
                    if tex_fid or dry:
                        tex_path = (f"{ROOT_FOLDER}/{primary}/{base}/"
                                    f"{TEX_SUBFOLDER}")

            md = build_paper_md(p, field_map.get(p.field_id, ""),
                                {"pdf": pdf_path, "tex": tex_path})
            mname = f"{base}.md"
            if mname in existing_meta:
                stats["skipped_exists"] += 1
                print(f"  - 跳过(已存在) {mname}")
                continue
            if dry:
                stats["papers"] += 1
                print(f"  [预览] {mname}  主标签={primary}")
            else:
                try:
                    client.upload_text(kb, mname, md, folder_id=meta_id)
                    stats["papers"] += 1
                    print(f"  ✓ {mname}  主标签={primary}")
                except IMAError as e:
                    stats["errors"] += 1
                    print(f"  ! 失败 {mname}: {str(e)[:100]}")

        # ---------- 资讯 ----------
        news = db.query(NewsItem).order_by(NewsItem.id).all()
        if news:
            news_id = ("DRY_RUN" if dry else
                       client.ensure_path(kb, [ROOT_FOLDER, METADATA, "news"]))
            print(f"\n迁移资讯（共 {len(news)} 条）:")
            for n in news:
                nname = f"{prefix(n.id, n.title)}.md"
                if nname in existing_meta:
                    stats["skipped_exists"] += 1
                    continue
                if dry:
                    stats["news"] += 1
                    print(f"  [预览] news/{nname}")
                else:
                    try:
                        client.upload_text(kb, nname, build_news_md(n),
                                           folder_id=news_id)
                        stats["news"] += 1
                        print(f"  ✓ news/{nname}")
                    except IMAError as e:
                        stats["errors"] += 1
                        print(f"  ! 失败 {nname}: {str(e)[:100]}")
    finally:
        db.close()

    print("\n" + "=" * 52)
    print("迁移汇总")
    print("=" * 52)
    print(f"  论文 metadata : {stats['papers']}")
    print(f"  资讯 metadata : {stats['news']}")
    print(f"  领域          : {stats['fields']}")
    print(f"  文件已上传    : {stats['uploaded_files']}")
    if dry:
        print(f"  预览将上传文件: {stats['would_upload']}")
    print(f"  跳过(不支持)  : {stats['skipped_ext']}")
    print(f"  跳过(已存在)  : {stats['skipped_exists']}")
    print(f"  错误          : {stats['errors']}")
    if dry:
        print("\n这是预览模式，未上传任何内容。去掉 --dry-run 正式迁移。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
