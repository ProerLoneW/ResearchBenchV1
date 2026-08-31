"""设置：自定义 API 配置（加密）、用户偏好（周目标）、后端连通性测试。"""
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import httpx
import requests
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
    load_ima_creds, save_ima_creds, resolve_ima_creds,
    HELP_DOC_URL, arxiv_api_base,
)
from ..services import ima_store
from ..services.ai import get_api_config
from ..services.ima_client import IMAClient, IMAError

router = APIRouter(prefix="/api/settings", tags=["settings"])


class HelpDocOut(BaseModel):
    url: str


@router.get("/help_doc_url", response_model=HelpDocOut)
def read_help_doc_url():
    """返回设置页「?」图标点击后跳转的使用说明书地址（来自环境变量）。"""
    return HelpDocOut(url=HELP_DOC_URL or "")


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


# ----------------------------------------------------------------------
# 连通性测试
# ----------------------------------------------------------------------
class TestOut(BaseModel):
    ok: bool
    detail: str


@router.post("/api/test", response_model=TestOut)
def test_api_config(db: Session = Depends(get_db)):
    """测试自定义 LLM API 是否可达（发一个 1 token 的非流式请求）。"""
    cfg = get_api_config(db)
    base = (cfg.base_url or "").strip()
    if not base:
        return TestOut(ok=False, detail="未配置 Base URL")
    key = decrypt_secret(cfg.api_key or "")
    if not key:
        return TestOut(ok=False, detail="未配置 API Key")
    model = (cfg.model_name or "gpt-4o-mini").strip() or "gpt-4o-mini"
    url = base.rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "stream": False,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        return TestOut(ok=False, detail=f"网络请求失败: {exc}")
    if not resp.ok:
        return TestOut(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
    return TestOut(ok=True, detail=f"API 可达（model={model}）")


@router.post("/ima/test", response_model=TestOut)
def test_ima():
    """测试 IMA OpenAPI 凭证是否可用。"""
    try:
        client = IMAClient(timeout=15)
        kbs = client.get_addable_knowledge_bases(limit=5)
        if not kbs:
            return TestOut(ok=True, detail="凭证有效，但未找到可写知识库")
        names = [kb.get("kb_name") or kb.get("name") or "未命名" for kb in kbs[:3]]
        return TestOut(ok=True, detail=f"凭证有效，可写知识库: {', '.join(names)}")
    except IMAError as exc:
        return TestOut(ok=False, detail=str(exc)[:200])
    except Exception as exc:
        return TestOut(ok=False, detail=f"测试异常: {type(exc).__name__}: {exc}")


class ArxivTestIn(BaseModel):
    source: Optional[str] = None  # 指定要测试的源 key；为空则测试当前已保存源


class ArxivSourceStatus(BaseModel):
    key: str
    name: str
    ok: bool
    detail: str


class ArxivTestOut(BaseModel):
    ok: bool                 # 被测源是否可达
    detail: str              # 被测源的结果说明
    tested: str              # 实际测试的源 key
    sources: list            # 全部源的可达性扫描 [{key, name, ok, detail}]


def _probe_one_source(key: str, base: str) -> ArxivSourceStatus:
    """探测单个 arXiv 源是否可达（与 Radar 一致：走默认 trust_env 读代理）。"""
    url = base + "?search_query=all:AI&start=0&max_results=1"
    try:
        resp = httpx.get(url, timeout=12, follow_redirects=True)
    except Exception as exc:
        return ArxivSourceStatus(
            key=key, name=ARXIV_SOURCES[key][0], ok=False,
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
        )
    if resp.is_success:
        return ArxivSourceStatus(
            key=key, name=ARXIV_SOURCES[key][0], ok=True, detail="可达"
        )
    return ArxivSourceStatus(
        key=key, name=ARXIV_SOURCES[key][0], ok=False, detail=f"HTTP {resp.status_code}"
    )


@router.post("/arxiv_source/test", response_model=ArxivTestOut)
def test_arxiv_source(payload: ArxivTestIn = ArxivTestIn()):
    """测试 arXiv 检索源。

    - 优先测试请求体里指定的 source（即设置页下拉框当前选中的项）；
    - 同时扫描全部源，返回每个源的可达性，便于用户直接看到哪个能用
      （这也解释了为什么 Radar 能检索到——它会自动回退到可达的源）。
    """
    key = payload.source if payload.source in ARXIV_SOURCES else ARXIV_SOURCE
    base = ARXIV_SOURCES[key][1]
    # 并发探测全部源，避免逐个超时拖慢
    results = {}
    with ThreadPoolExecutor(max_workers=len(ARXIV_SOURCES)) as ex:
        futs = {k: ex.submit(_probe_one_source, k, v[1]) for k, v in ARXIV_SOURCES.items()}
        for k, f in futs.items():
            results[k] = f.result()
    scan = [results[k].model_dump() for k in ARXIV_SOURCES]
    tested = results[key]
    note = ""
    # 若被测源不可达但存在可达源，给出提示，与 Radar 的回退行为对齐
    reachable = [s["name"] for s in scan if s["ok"]]
    if not tested.ok and reachable:
        note = f"（该源不可达，但以下源可用：{', '.join(reachable)}；Radar 会自动回退到可用源）"
    return ArxivTestOut(
        ok=tested.ok,
        detail=(f"{tested.name} 可达" if tested.ok else f"{tested.name} 不可达：{tested.detail}{note}"),
        tested=key,
        sources=scan,
    )


@router.post("/radar_proxy/test", response_model=TestOut)
def test_radar_proxy():
    """测试当前代理配置是否能正常访问 arXiv。"""
    if RADAR_PROXY:
        via = f"（经代理 {RADAR_PROXY}）"
    else:
        via = "（当前为直连，使用环境 HTTPS_PROXY/HTTP_PROXY）"
    try:
        resp = httpx.get(
            "https://export.arxiv.org/api/query?search_query=all:AI&start=0&max_results=1",
            timeout=15, follow_redirects=True,
        )
    except Exception as exc:
        return TestOut(ok=False, detail=f"无法访问 arXiv{via}: {type(exc).__name__}: {exc}")
    if not resp.is_success:
        return TestOut(ok=False, detail=f"HTTP {resp.status_code}{via}")
    return TestOut(ok=True, detail=f"可访问 arXiv{via}")


@router.post("/rsshub/test", response_model=TestOut)
def test_rsshub():
    """测试配置的 RSSHub 地址是否可达。"""
    base = (RSSHUB_BASE or "").strip()
    if not base:
        return TestOut(ok=False, detail="未配置 RSSHub 地址")
    try:
        resp = httpx.get(base.rstrip("/") + "/health", timeout=10, follow_redirects=True)
    except Exception as exc:
        return TestOut(ok=False, detail=f"请求失败: {type(exc).__name__}: {exc}")
    if resp.is_success:
        return TestOut(ok=True, detail=f"RSSHub 在线（HTTP {resp.status_code}）")
    # 服务在线但 /health 不存在时给更准确的提示
    if resp.status_code == 404:
        return TestOut(ok=False, detail=f"RSSHub 返回 404（/health 路径不存在，服务可能在线）")
    return TestOut(ok=False, detail=f"HTTP {resp.status_code}")


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


# ----------------------------------------------------------------------
# IMA 知识库凭证（存储后端）
# 服务器部署没有 .env 时，可在「设置」页填写并持久化到 data/ima_creds.json。
# ----------------------------------------------------------------------
class ImaOut(BaseModel):
    client_id_set: bool
    api_key_set: bool
    kb_id_set: bool
    # 每个参数的真实生效来源：env（环境变量）/ settings（设置页文件）/ none（未配置）
    client_id_src: str = "none"
    api_key_src: str = "none"
    kb_id_src: str = "none"
    # 命中来源时的环境变量名（仅变量名，不含值）
    client_id_env: str = ""
    api_key_env: str = ""
    kb_id_env: str = ""


class ImaIn(BaseModel):
    client_id: str = ""
    api_key: str = ""      # 留空表示不修改
    kb_id: str = ""        # 留空表示不修改


@router.get("/ima", response_model=ImaOut)
def read_ima():
    """返回每个参数是否已配置及真实生效来源（环境变量/设置页文件），不回传明文。"""
    r = resolve_ima_creds()
    return ImaOut(
        client_id_set=bool(r.get("client_id_set")),
        api_key_set=bool(r.get("api_key_set")),
        kb_id_set=bool(r.get("kb_id_set")),
        client_id_src=r.get("client_id_src", "none"),
        api_key_src=r.get("api_key_src", "none"),
        kb_id_src=r.get("kb_id_src", "none"),
        client_id_env=r.get("client_id_env", ""),
        api_key_env=r.get("api_key_env", ""),
        kb_id_env=r.get("kb_id_env", ""),
    )


@router.put("/ima", response_model=ImaOut)
def update_ima(payload: ImaIn):
    """保存 IMA 凭证并重置缓存的客户端，使下次请求即用新凭证。"""
    save_ima_creds(payload.client_id, payload.api_key, payload.kb_id)
    ima_store.reset_client()
    return read_ima()
