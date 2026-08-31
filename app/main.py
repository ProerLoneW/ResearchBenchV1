"""
AI 科研论文工作台 —— FastAPI 后端入口。
核心闭环：发现最新论文 → 一键收录 → 分类管理 → 阅读 → 笔记心得
          → TeX 中文版生成飞书阅读文档 → 阅读统计与长期复盘。
"""
import os
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .admin_guard import AdminGuardMiddleware, admin_status
from .db import init_db, SessionLocal
from .models import RadarConfig
from .config import DEFAULT_RADAR_TEMPLATES, DATA_DIR
from . import models  # noqa: F401
from .routers import (
    fields, papers, radar, stats, settings, feishu, news_library, services,
    assets, translate,
)

app = FastAPI(title="AI 科研论文工作台", version="1.0.0")

app.include_router(fields.router)
app.include_router(papers.router)
app.include_router(radar.router)
app.include_router(stats.router)
app.include_router(settings.router)
app.include_router(feishu.router)
app.include_router(news_library.router)
# 对外服务化接口：无状态、不落库，供本地 HTML 等外部客户端调用
app.include_router(services.router)
# 论文原文资产（PDF / LaTeX 源码）抓取，写入 IMA，需管理员密码
app.include_router(assets.router)
# 论文中译任务：建任务 / 查待办 / 回填中文版 PDF（写操作需管理员密码）
app.include_router(translate.router)

# ---------------------------------------------------------------------------
# 中间件。注意顺序：Starlette 中「后添加的中间件更靠外」。
# 先加管理员密码守卫、后加 CORS，才能保证 CORS 在最外层：
# 这样浏览器预检（OPTIONS）与 401 响应都会带上正确的跨域头，
# 前端才能读到 401 并弹出密码输入框。
# ---------------------------------------------------------------------------
app.add_middleware(AdminGuardMiddleware)

# ---------------------------------------------------------------------------
# CORS：默认 allow_origins=["*"]。
# 关键点：本地 HTML 双击打开时属于 file:// 协议，浏览器发出的 Origin 是 "null"。
# 按 CORS 规范，allow_origins=["*"] 时**不允许**同时 allow_credentials=True
# （浏览器会直接报错 "The value of the 'Access-Control-Allow-Origin' header
#  must not be the wildcard '*' when credentials mode is 'include'"），
# 因此这里在通配模式下强制 credentials=False。
# 若需携带 Cookie / 凭据，请把 ALLOW_ORIGINS 配成明确的域名白名单（逗号分隔），
# 此时才允许 allow_credentials=True。
# ---------------------------------------------------------------------------
_raw_origins = os.getenv("ALLOW_ORIGINS", "*").strip()
ALLOW_ORIGINS = (
    ["*"] if _raw_origins in ("", "*")
    else [o.strip() for o in _raw_origins.split(",") if o.strip()]
)
ALLOW_CREDENTIALS = "*" not in ALLOW_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],          # 需要放行 X-Admin-Password
    expose_headers=["*"],
)

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
    _settle_stale_translate_tasks()
    _warm_ima_cache()


def _settle_stale_translate_tasks():
    """
    把上次进程残留的"翻译中"任务收尾。

    自动翻译跑在后台线程里，服务一停线程就没了，任务会永远停在 running
    状态，任务窗口看起来像卡住。启动时统一标记为中断，用户可重新发起。
    """
    try:
        from .services import llm_translate
        n = llm_translate.mark_stale_running_failed()
        if n:
            print(f"[translate] 已将 {n} 个残留的翻译任务标记为中断")
    except Exception as exc:
        print(f"[translate] 收尾残留任务失败（不影响启动）: "
              f"{type(exc).__name__}: {exc}")


def _warm_ima_cache():
    """
    后台预热 IMA 派生缓存。

    IMA 读一条要一次列举 + 一次下载，首屏直接回源会很慢；这里在启动后
    开一个守护线程预拉取，失败也不影响服务（读取侧会自动降级到缓存）。
    """
    if os.getenv("IMA_WARMUP", "1").strip() in ("0", "false", "False"):
        return

    def _run():
        try:
            from .services import ima_store
            ima_store.refresh_all()
        except Exception as exc:   # 预热失败无所谓，后续请求会自己重试
            print(f"[ima] 缓存预热跳过: {type(exc).__name__}: {exc}")

    threading.Thread(target=_run, name="ima-warmup", daemon=True).start()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/admin/status")
def admin_status_api():
    """前端据此判断是否需要输入管理员密码（enabled 为 false 时完全不设防）。"""
    return admin_status()
