"""设置：自定义 API 配置（加密）、用户偏好（周目标）。"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ApiConfig, UserPrefs
from ..config import (
    encrypt_secret, decrypt_secret, RADAR_PROXY, save_radar_proxy,
    ARXIV_SOURCES, ARXIV_SOURCE, save_arxiv_source, NEWS_LANGS,
    all_cn_rss, save_custom_rss,
    RSSHUB_BASE, save_rsshub_base, load_wechat_accounts, save_wechat_accounts,
)
from ..services.ai import get_api_config

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ApiConfigIn(BaseModel):
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None   # 明文传入，后端加密存储
    model_name: Optional[str] = None
    other_params: Optional[str] = None


class ApiConfigOut(BaseModel):
    provider: str
    base_url: str
    model_name: str
    api_key_set: bool
    other_params: str


@router.get("/api", response_model=ApiConfigOut)
def read_api(db: Session = Depends(get_db)):
    cfg = get_api_config(db)
    return ApiConfigOut(
        provider=cfg.provider or "",
        base_url=cfg.base_url or "",
        model_name=cfg.model_name or "",
        api_key_set=bool(cfg.api_key),
        other_params=cfg.other_params or "{}",
    )


@router.put("/api", response_model=ApiConfigOut)
def update_api(payload: ApiConfigIn, db: Session = Depends(get_db)):
    cfg = get_api_config(db)
    if payload.provider is not None:
        cfg.provider = payload.provider
    if payload.base_url is not None:
        cfg.base_url = payload.base_url
    if payload.model_name is not None:
        cfg.model_name = payload.model_name
    if payload.other_params is not None:
        cfg.other_params = payload.other_params
    if payload.api_key is not None:
        # 仅当填写了新 key 才更新（空字符串表示不修改）
        if payload.api_key != "":
            cfg.api_key = encrypt_secret(payload.api_key)
    db.commit(); db.refresh(cfg)
    return ApiConfigOut(
        provider=cfg.provider, base_url=cfg.base_url,
        model_name=cfg.model_name, api_key_set=bool(cfg.api_key),
        other_params=cfg.other_params or "{}",
    )


class PrefsIn(BaseModel):
    weekly_goal: Optional[int] = None


class PrefsOut(BaseModel):
    weekly_goal: int


@router.get("/prefs", response_model=PrefsOut)
def read_prefs(db: Session = Depends(get_db)):
    p = db.query(UserPrefs).filter(UserPrefs.id == 1).first()
    if not p:
        p = UserPrefs(id=1); db.add(p); db.commit(); db.refresh(p)
    return PrefsOut(weekly_goal=p.weekly_goal)


@router.put("/prefs", response_model=PrefsOut)
def update_prefs(payload: PrefsIn, db: Session = Depends(get_db)):
    p = db.query(UserPrefs).filter(UserPrefs.id == 1).first()
    if not p:
        p = UserPrefs(id=1)
        db.add(p); db.commit(); db.refresh(p)
    if payload.weekly_goal is not None:
        p.weekly_goal = max(1, payload.weekly_goal)
    db.commit(); db.refresh(p)
    return PrefsOut(weekly_goal=p.weekly_goal)


class ProxyOut(BaseModel):
    proxy: str


class ProxyIn(BaseModel):
    proxy: str = ""


@router.get("/radar_proxy", response_model=ProxyOut)
def read_proxy():
    return ProxyOut(proxy=RADAR_PROXY or "")


@router.put("/radar_proxy", response_model=ProxyOut)
def update_proxy(payload: ProxyIn):
    save_radar_proxy(payload.proxy)
    return ProxyOut(proxy=payload.proxy)


class ArxivSourceOut(BaseModel):
    current: str
    sources: list  # [{key, name, cn}]


class ArxivSourceIn(BaseModel):
    source: str = "sjtu"


@router.get("/arxiv_source", response_model=ArxivSourceOut)
def read_arxiv_source():
    return ArxivSourceOut(
        current=ARXIV_SOURCE,
        sources=[
            {"key": k, "name": v[0], "cn": v[2]}
            for k, v in ARXIV_SOURCES.items()
        ],
    )


@router.put("/arxiv_source", response_model=ArxivSourceOut)
def update_arxiv_source(payload: ArxivSourceIn):
    save_arxiv_source(payload.source)
    return read_arxiv_source()


class NewsLangsOut(BaseModel):
    langs: list  # [{key, name}]


@router.get("/news_langs", response_model=NewsLangsOut)
def read_news_langs():
    return NewsLangsOut(
        langs=[{"key": k, "name": v[0]} for k, v in NEWS_LANGS.items()]
    )


class RssFeedsOut(BaseModel):
    feeds: list  # [{name, url}]
    builtin: list  # 内置源（只读）


@router.get("/news_rss", response_model=RssFeedsOut)
def read_rss_feeds():
    from ..config import CN_MEDIA_RSS
    return RssFeedsOut(
        feeds=[{"name": n, "url": u} for n, u in all_cn_rss()],
        builtin=[{"name": n, "url": u} for n, u in CN_MEDIA_RSS],
    )


class RssFeedsIn(BaseModel):
    feeds: list  # [{name, url}]（自定义部分，覆盖写入）


@router.put("/news_rss", response_model=RssFeedsOut)
def update_rss_feeds(payload: RssFeedsIn):
    save_custom_rss([(f.get("name", "自定义源"), f.get("url", "")) for f in payload.feeds if f.get("url")])
    return read_rss_feeds()


class RsshubOut(BaseModel):
    base: str


class RsshubIn(BaseModel):
    base: str = ""


@router.get("/rsshub", response_model=RsshubOut)
def read_rsshub():
    return RsshubOut(base=RSSHUB_BASE or "")


@router.put("/rsshub", response_model=RsshubOut)
def update_rsshub(payload: RsshubIn):
    save_rsshub_base(payload.base or "")
    return RsshubOut(base=RSSHUB_BASE)


class WechatOut(BaseModel):
    accounts: list  # [{name, gh}]


class WechatIn(BaseModel):
    accounts: list  # [{name, gh}]


@router.get("/wechat", response_model=WechatOut)
def read_wechat():
    return WechatOut(accounts=[{"name": n, "gh": g} for n, g in load_wechat_accounts()])


@router.put("/wechat", response_model=WechatOut)
def update_wechat(payload: WechatIn):
    save_wechat_accounts([(a.get("name", "公众号"), a.get("gh", "")) for a in payload.accounts if a.get("gh")])
    return read_wechat()
