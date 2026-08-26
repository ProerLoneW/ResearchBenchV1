"""
AI 大脑服务：通过用户配置的 OpenAI-Compatible API 提供
自然语言交互 / 总结 / 论文库问答。未配置时优雅降级。
（WorkBuddy 作为宿主 AI，本服务用于工作台自身的 AI 能力，
 使系统不依赖单一平台即可运行。）
"""
import json

import httpx

from ..config import decrypt_secret
from ..models import ApiConfig


def get_api_config(db):
    cfg = db.query(ApiConfig).filter(ApiConfig.id == 1).first()
    if not cfg:
        cfg = ApiConfig(id=1)
        db.add(cfg); db.commit(); db.refresh(cfg)
    return cfg


def _endpoint(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        base = "https://api.openai.com/v1"
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


async def chat(messages, db, temperature=0.3, max_tokens=1500):
    cfg = get_api_config(db)
    if not cfg.base_url or not cfg.model_name:
        raise RuntimeError("尚未配置 API（请在「设置」页填写 Base URL / Model / Key）。")
    api_key = decrypt_secret(cfg.api_key)
    if not api_key:
        raise RuntimeError("API Key 未配置。")

    payload = {
        "model": cfg.model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    extra = {}
    try:
        extra = json.loads(cfg.other_params or "{}")
    except Exception:
        extra = {}
    payload.update(extra)

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            _endpoint(cfg.base_url),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]


async def summarize(text: str, db, max_tokens=800):
    messages = [
        {"role": "system", "content": "你是一名严谨的科研助理，请用中文提炼要点，分条列出，控制在 200 字内。"},
        {"role": "user", "content": f"请总结以下内容：\n\n{text[:6000]}"},
    ]
    return await chat(messages, db, max_tokens=max_tokens)


async def ask_library(question: str, papers_context: str, db, max_tokens=1200):
    messages = [
        {"role": "system", "content": "你是用户的私人科研助理，基于用户论文库内容回答科研问题，引用论文标题，不编造。"},
        {"role": "user", "content": f"用户论文库摘要：\n{papers_context[:6000]}\n\n问题：{question}"},
    ]
    return await chat(messages, db, max_tokens=max_tokens)
