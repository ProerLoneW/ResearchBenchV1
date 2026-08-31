"""
飞书后台设置：配置 App ID/Secret（用于 TeX→飞书 与文档写入），
并触发一次用户授权以获取 user_access_token。
"""
import json

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import DATA_DIR, FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_BASE_URL
from ..services.feishu import FeishuClient

router = APIRouter(prefix="/api/feishu", tags=["feishu"])

FC_PATH = DATA_DIR / "feishu_config.json"


def load_fc() -> dict:
    if FC_PATH.exists():
        return json.loads(FC_PATH.read_text(encoding="utf-8"))
    return {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}


def save_fc(data: dict):
    FC_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")


class FeishuConfigIn(BaseModel):
    app_id: str
    app_secret: str


@router.get("/config")
def get_config():
    fc = load_fc()
    secret = fc.get("app_secret", "")
    return {
        "app_id": fc.get("app_id", ""),
        "app_secret_set": bool(secret),
    }


@router.put("/config")
def set_config(payload: FeishuConfigIn):
    if not payload.app_id or not payload.app_secret:
        raise HTTPException(400, "app_id 与 app_secret 均必填")
    save_fc({"app_id": payload.app_id, "app_secret": payload.app_secret})
    return {"ok": True}


@router.post("/authorize")
def authorize():
    """触发飞书 OAuth 用户授权（会打开浏览器）。"""
    fc = load_fc()
    client = FeishuClient(app_id=fc.get("app_id"), app_secret=fc.get("app_secret"))
    token = client.get_user_access_token()
    masked = token[:6] + "..." + token[-4:] if token else ""
    return {"ok": True, "token_masked": masked}


@router.post("/test")
def test_feishu():
    """测试飞书应用凭证是否有效（只拿 app_access_token，不触发浏览器授权）。"""
    fc = load_fc()
    app_id = fc.get("app_id") or FEISHU_APP_ID
    app_secret = fc.get("app_secret") or FEISHU_APP_SECRET
    if not app_id:
        return {"ok": False, "detail": "未配置 App ID"}
    if not app_secret:
        return {"ok": False, "detail": "未配置 App Secret"}
    try:
        resp = requests.post(
            f"{FEISHU_BASE_URL}/auth/v3/app_access_token/internal",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=15,
        )
        data = resp.json() if resp.text else {}
    except Exception as exc:
        return {"ok": False, "detail": f"网络请求失败: {type(exc).__name__}: {exc}"}
    if not resp.ok or data.get("code", 0) != 0:
        msg = data.get("msg") or data.get("error") or f"HTTP {resp.status_code}"
        return {"ok": False, "detail": msg}
    exp = data.get("data", {}).get("expire") or data.get("expire")
    return {"ok": True, "detail": f"凭证有效（tenant_access_token 已获取{exp and f'，有效期 {exp}s' or ''}）"}
