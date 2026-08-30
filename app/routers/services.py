"""
对外服务化接口（Stateless Services）。

设计目标（沿用 example_demo 的模式）：
1. **无状态、不落库**：本模块的接口一律不读写 SQLite，不保存检索结果、不写任何文件。
   因此即使服务部署在公网上，调用方（含双击打开的本地 HTML）也只是"借用检索能力"，
   不会把个人论文库暴露给服务器。
2. **一律用 Pydantic model 接 JSON body**：原项目出现过"前端发 JSON body、
   FastAPI 却把标量声明成 query 参数"的传输层不匹配，这里统一用 model 接收，避免同类问题。
3. **检索算法不改动**：arXiv / Google News 的检索逻辑仍由 app/services/arxiv.py
   与 app/services/news.py 执行（与 app/routers/radar.py 完全一致）。

可被管理员密码放行（见 app/admin_guard.py）：这些接口是"搜索/检索"，不是"改数据"。
"""
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import arxiv, metadata, news

logger = logging.getLogger("services")

router = APIRouter(prefix="/api/services", tags=["services"])


# --------------------------------------------------------------------------
# 请求 / 响应模型（全部用 Pydantic model 接 JSON body）
# --------------------------------------------------------------------------
class RadarRunRequest(BaseModel):
    """与 app/routers/radar.py::run_search 的入参一一对应。"""

    type: str = "paper"          # paper / news
    keywords: str = ""
    field: str = ""
    days: int = 7
    max_results: int = 30
    lang: str = "en"             # 资讯语言：en / zh / auto
    channel: str = "google"      # 资讯渠道：google / cn / all


class MetadataRequest(BaseModel):
    """根据链接抓取公开元信息（arXiv 走官方 API，其余抓 <title>/meta description）。"""

    url: str = ""


class AiChatRequest(BaseModel):
    """
    调用方**自备**的模型凭据，服务端不落盘、不写日志、不保存。

    仅当环境变量 ENABLE_AI_PROXY=1 时才真正转发；否则返回 501 + 说明。
    """

    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    api_key: str = ""
    messages: list = Field(default_factory=list)
    temperature: float = 0.3
    max_tokens: int = 1500


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------
@router.get("/health")
def health():
    """健康检查（部署脚本 deploy.sh 会轮询这个接口）。"""
    return {"status": "ok", "service": "researchbench", "module": "services"}


@router.get("/capabilities")
def capabilities():
    """
    能力清单：明确列出"已服务化"与"暂未实现"，方便本地 HTML 客户端直接渲染占位说明。
    """
    return {
        "implemented": [
            {"key": "radar.run", "method": "POST", "path": "/api/services/radar/run",
             "desc": "arXiv 论文 / AI 资讯检索（无状态，不落库）"},
            {"key": "metadata", "method": "POST", "path": "/api/services/metadata",
             "desc": "按链接抓取标题/摘要/GitHub 等公开元信息"},
            {"key": "ai.chat", "method": "POST", "path": "/api/services/ai/chat",
             "desc": "调用方自备 Key 的 OpenAI 兼容对话（需服务端开启 ENABLE_AI_PROXY=1）",
             "enabled": os.getenv("ENABLE_AI_PROXY", "").strip() == "1"},
        ],
        "not_implemented": [
            {"key": "tex2feishu",
             "reason": "需要读取调用方本地的 TeX 目录，且需要飞书 OAuth 用户授权；"
                       "纯网页无法访问本地文件系统，服务端也无用户授权凭据。"},
            {"key": "local_dir_scan",
             "reason": "遍历本地文件夹只能在调用方自己的机器上做，浏览器沙箱禁止读取本地路径。"},
            {"key": "library_sync",
             "reason": "论文库保存在浏览器 localStorage，服务端有意不代存，避免个人数据外泄。"},
        ],
    }


@router.post("/radar/run")
async def radar_run(payload: RadarRunRequest):
    """
    检索论文 / 资讯。与 app/routers/radar.py::run_search 的唯一差别：
    不查 SQLite 的 Paper 表，因此不返回 in_library 标记 —— 该标记由调用方
    在自己的本地库里判断（参考 static_client/client.js）。
    """
    try:
        if payload.type == "news":
            news_out = await news.fetch_news(
                payload.keywords,
                payload.days,
                payload.max_results,
                lang=payload.lang,
                channel=payload.channel,
            )
            results = news_out["results"]
            sources = news_out.get("sources", [])
        else:
            results = await arxiv.fetch_papers(
                payload.keywords,
                payload.days,
                payload.max_results,
            )
            sources = []
    except httpx.HTTPError as e:
        logger.warning("services.radar http error: %r", e)
        raise HTTPException(
            status_code=502,
            detail=(
                "检索源网络请求失败：无法访问 arXiv / Google News。"
                "请确认服务器能联网（代理/防火墙可能拦截外网）；"
                f"错误信息：{e!r}"
            ),
        )
    except Exception as e:
        logger.warning("services.radar error: %r", e)
        raise HTTPException(status_code=502, detail=f"检索失败：{e!r}")

    # 与 radar.py 保持一致：field 由调用方指定时覆盖
    for r in results:
        if payload.field:
            r["field"] = payload.field

    return {
        "type": payload.type,
        "count": len(results),
        "results": results,
        "sources": sources,
    }


@router.post("/metadata")
async def fetch_meta(payload: MetadataRequest):
    """
    按链接抓取公开元信息。app/services/metadata.py 本身无状态，可直接服务化。
    arXiv 链接走 arXiv API，其他链接抓 <title> / meta description。
    """
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(400, "请提供论文链接（url）")
    try:
        meta = await metadata.fetch_metadata(url)
    except httpx.HTTPError as e:
        logger.warning("services.metadata http error: %r", e)
        raise HTTPException(502, f"抓取失败：无法访问该链接或服务器无外网；错误信息：{e!r}")
    except Exception as e:
        logger.warning("services.metadata error: %r", e)
        raise HTTPException(502, f"抓取失败：{e!r}")
    if not meta:
        return {"found": False}
    return {"found": True, **meta}


@router.post("/ai/chat")
async def ai_chat(payload: AiChatRequest):
    """
    AI 对话（可选能力）。

    安全取舍：服务端**不接受也不保存**任何长期凭据。调用方必须自备 base_url / model /
    api_key，请求结束后即丢弃，不写盘、不进日志。
    出于"开放代理"风险考量，默认关闭，需服务端显式设置 ENABLE_AI_PROXY=1 才启用。
    若你希望完全不外传，建议在使用者自己的环境里配置（见 README「部署到服务器」一节）。
    """
    if os.getenv("ENABLE_AI_PROXY", "").strip() != "1":
        raise HTTPException(
            status_code=501,
            detail=(
                "服务端未启用 AI 代理（默认关闭）。"
                "原因：AI 能力需要调用方的模型 Key，托管服务器代持存在泄露与滥用风险。"
                "请在自己的环境配置 AI（主应用「设置-API」页），"
                "或由服务端管理员设置环境变量 ENABLE_AI_PROXY=1 后"
                "由调用方在请求中自备 Key（服务端不落盘、不记录）。"
            ),
        )
    if not payload.api_key or not payload.model:
        raise HTTPException(400, "缺少 model 或 api_key（本接口不保存任何凭据）")

    base = (payload.base_url or "https://api.openai.com/v1").rstrip("/")
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    body = {
        "model": payload.model,
        "messages": payload.messages,
        "temperature": payload.temperature,
        "max_tokens": payload.max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {payload.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        return {"content": data["choices"][0]["message"]["content"]}
    except httpx.HTTPError as e:
        raise HTTPException(502, f"模型服务请求失败：{e!r}")
    except Exception as e:
        raise HTTPException(502, f"模型服务调用失败：{e!r}")
