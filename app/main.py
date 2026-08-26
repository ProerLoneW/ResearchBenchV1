"""
AI 科研论文工作台 —— FastAPI 后端入口。
核心闭环：发现最新论文 → 一键收录 → 分类管理 → 阅读 → 笔记心得
          → TeX 中文版生成飞书阅读文档 → 阅读统计与长期复盘。
"""
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .db import init_db, SessionLocal
from .models import RadarConfig
from .config import DEFAULT_RADAR_TEMPLATES, DATA_DIR
from . import models  # noqa: F401
from .routers import (
    fields, papers, radar, stats, settings, feishu, news_library
)

app = FastAPI(title="AI 科研论文工作台", version="1.0.0")

app.include_router(fields.router)
app.include_router(papers.router)
app.include_router(radar.router)
app.include_router(stats.router)
app.include_router(settings.router)
app.include_router(feishu.router)
app.include_router(news_library.router)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def _startup():
    init_db()
    # 首次启动：写入默认雷达检索模板
    db = SessionLocal()
    try:
        if db.query(RadarConfig).count() == 0:
            for t in DEFAULT_RADAR_TEMPLATES:
                db.add(RadarConfig(**t))
            db.commit()
    finally:
        db.close()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/health")
def health():
    return {"status": "ok"}
