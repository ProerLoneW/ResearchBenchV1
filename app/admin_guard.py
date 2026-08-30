"""
管理员密码保护（数据修改保护）。

规则：
1. 环境变量 ADMIN_PASSWORD **未设置（或为空）时，中间件完全放行**，不启用任何保护。
   —— 本地开发、单机使用不受影响。
2. 设置后：POST / PUT / PATCH / DELETE 等"会改数据"的请求必须携带正确密码，
   否则返回 401；GET 一律放行（看板 / 列表 / 搜索 / 统计都正常）。
3. 白名单：健康检查与"无状态服务接口"属于检索而非改数据，即使 POST 也放行。

密码传递方式：请求头 `X-Admin-Password: <密码>`。
前端 app/static/app.js 在收到 401 时会 prompt() 让用户输入并存到 sessionStorage 后重试一次。
"""
import hmac
import logging
import os

from fastapi.responses import JSONResponse

logger = logging.getLogger("admin_guard")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
HEADER = "x-admin-password"

# 需要保护的写方法
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# ---------------------------------------------------------------------------
# 白名单：这些路径属于"检索/只读/静态资源"，即使是非 GET 也放行
# ---------------------------------------------------------------------------
PUBLIC_PREFIXES = (
    "/api/health",          # 部署脚本 / 探活
    "/api/services/",       # 无状态服务接口（radar 检索、metadata 抓取…）
    "/api/admin/status",    # 前端查询"是否启用了管理员密码"
    "/docs", "/redoc", "/openapi.json",
    "/static", "/client",
)

# 主应用内同样是"只读检索"的接口：只查库标记 in_library，不写库
PUBLIC_WRITE_PATHS = (
    "/api/radar/run",           # 手动检索
    "/api/radar/run_template",  # 按模板检索（前缀匹配）
    "/api/papers/fetch_metadata",
    "/api/radar/run_bg",        # 带步骤进度的后台检索（只是检索，不写库）
    "/api/radar/run_template_bg",  # 同上，按模板
)

UNAUTHORIZED_BODY = {
    "detail": "该操作会修改数据，需要管理员密码。请在请求头携带 X-Admin-Password，"
              "或在页面弹窗中输入管理员密码后重试。"
}


def _is_public(path: str, method: str) -> bool:
    """判断该请求是否免密。"""
    if method not in WRITE_METHODS:
        return True  # GET / HEAD / OPTIONS 一律放行
    for prefix in PUBLIC_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return True
    for p in PUBLIC_WRITE_PATHS:
        if path == p or path.startswith(p):
            return True
    return False


def _password_ok(given: str) -> bool:
    return bool(given) and hmac.compare_digest(given, ADMIN_PASSWORD)


class AdminGuardMiddleware:
    """纯 ASGI 中间件：只检查请求头，不读 body（避免大文件上传被缓冲进内存）。"""

    def __init__(self, app):
        self.app = app
        self.enabled = bool(ADMIN_PASSWORD)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        if _is_public(path, method):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        if _password_ok(headers.get(HEADER, "")):
            await self.app(scope, receive, send)
            return

        logger.warning("admin guard: 拒绝未授权的写请求 %s %s", method, path)
        resp = JSONResponse(status_code=401, content=UNAUTHORIZED_BODY)
        await resp(scope, receive, send)


def admin_status() -> dict:
    """给前端查询当前是否启用了管理员密码保护。"""
    return {"enabled": bool(ADMIN_PASSWORD)}
