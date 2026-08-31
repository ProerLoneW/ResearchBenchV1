"""
AI 资讯发现服务（多源、多路检索）。

检索渠道（并行拉取，去重合并）：
  1. Google News RSS     —— 国际/中文资讯，按语言切换（需代理或直连）。
  2. 国内科技媒体 RSS     —— 量子位/36氪/InfoQ 等实测可用源（国内直连）。
  3. RSSHub 微信公众号   —— 通过 gh_ 原始 ID 抓取指定公众号（需自建/可用的 RSSHub）。

每条渠道独立超时、独立容错；返回的 sources 字段报告每个渠道的成功/失败，
避免「静默失败」——用户能直接看到哪个源挂了、为什么挂。
"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from ..config import (
    NEWS_LANGS, all_cn_rss,
    RSSHUB_BASE, load_wechat_accounts,
)

GITHUB_RE = re.compile(r"https?://github\.com/[^\s)\]\"'>]+", re.I)
NEWS_URL = "https://news.google.com/rss/search"

# 资讯渠道：google=仅 Google News；cn=仅国内媒体 RSS；all=两者合并
NEWS_CHANNELS = {
    "google": "Google News",
    "cn": "国内媒体/公众号",
    "all": "Google News + 国内媒体",
}

# 浏览器 UA，国内 RSS 站对空 UA / 非浏览器 UA 会返回 403 / 反爬页
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0 Safari/537.36")}

_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def _parse_pubdate(s: str):
    """宽松解析多种 RSS 时间格式（RFC822 / 36氪 本地格式 / ISO）。"""
    if not s:
        return None
    s = s.strip()
    fmts = (
        "%a, %d %b %Y %H:%M:%S %Z",     # RFC 822
        "%a, %d %b %Y %H:%M:%S %z",     # RFC 822 + 时区偏移
        "%Y-%m-%d %H:%M:%S %z",         # 36氪：2026-08-22 19:22:19 +0800
        "%Y-%m-%dT%H:%M:%S%z",          # ISO
        "%Y-%m-%d %H:%M:%S",            # 无时区
    )
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            # 无时区的按 UTC 处理，保证与 cutoff（UTC aware）可比
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue
    # 最后尝试 fromisoformat
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _news_params(lang: str, q: str) -> dict:
    """根据语言选择 Google News 的 hl/gl/ceid 参数。"""
    spec = NEWS_LANGS.get(lang)
    if spec and spec[1]:
        params = dict(spec[1])
    else:
        params = dict(NEWS_LANGS["en"][1])
    params["q"] = q
    return params


def _looks_like_rss(raw: str) -> bool:
    """粗略判断是否真的拿到了 RSS/Atom XML，而非反爬 HTML 页面。"""
    head = raw.lstrip()[:400].lower()
    return ("<rss" in head or "<feed" in head or "<?xml" in head)


async def _fetch_google_news(keywords: str, days: int, max_results: int, lang: str = "en"):
    """从 Google News 拉取资讯（按语言 fallback）。返回 (items, error_or_None)。"""
    q = " OR ".join(k.strip() for k in keywords.split(",") if k.strip())
    if not q:
        q = "AI"
    order = [lang]
    if lang != "en":
        order.append("en")
    if "zh" not in order:
        order.append("zh")
    raw, last_err = "", None
    for lg in order:
        params = _news_params(lg, q)
        try:
            async with httpx.AsyncClient(timeout=30, headers=UA, follow_redirects=True) as client:
                resp = await client.get(NEWS_URL, params=params)
                if resp.status_code == 429:
                    last_err = "Google News 返回 429（请求过于频繁，请稍后重试）"
                    continue
                resp.raise_for_status()
                raw = resp.text
                break
        except Exception as e:
            last_err = str(e)
            continue
    if not raw:
        return [], last_err or "Google News 无返回"
    if not _looks_like_rss(raw):
        return [], "Google News 返回非 RSS 内容（可能被拦截）"
    items = _parse_rss_items(raw, days, keyword_filter=q, fallback_source="Google News", max_results=max_results)
    return items, None


def _split_keywords(keywords: str) -> list:
    """把模板关键词拆成原子词：先按逗号，再按 ' OR ' 切分，并去掉引号包裹。"""
    parts = []
    for seg in keywords.replace('"', '').split(","):
        for kw in seg.split(" OR "):
            kw = kw.strip().strip('"').strip()
            if kw:
                parts.append(kw.lower())
    # 去重保序
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p); out.append(p)
    return out


async def _fetch_cn_rss(keywords: str, days: int, max_results: int):
    """
    从国内科技媒体 RSS 拉取（国内直连）。
    返回 (items, errors:list)。RSS 无检索参数，需本地关键词过滤。
    """
    kws = _split_keywords(keywords)
    feeds = all_cn_rss()
    items, errors = [], []
    if not feeds:
        return items, ["未配置任何国内 RSS 源"]
    async with httpx.AsyncClient(timeout=25, headers=UA, follow_redirects=True) as client:
        for name, url in feeds:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                text = resp.text
                if not _looks_like_rss(text):
                    errors.append(f"{name}：返回非 RSS（可能反爬/失效）")
                    continue
                got = _parse_rss_items(text, days, keyword_filter=None,
                                       source_hint=name, max_results=0)
                if got:
                    items.extend(got)
                else:
                    errors.append(f"{name}：源可达但近期无条目")
            except Exception as e:
                errors.append(f"{name}：{e}")
                continue
    # 本地关键词过滤（公众号/RSS 无检索参数）
    if kws and items:
        filtered = []
        for it in items:
            hay = (it["title"] + " " + it.get("summary", "")).lower()
            if any(k in hay for k in kws):
                filtered.append(it)
        items = filtered
    return items, errors


async def _fetch_wechat_rsshub(keywords: str, days: int, max_results: int):
    """
    通过 RSSHub 抓取指定微信公众号（按 gh_ 原始 ID）。
    返回 (items, errors:list)。需要可用/自建的 RSSHub 实例。
    """
    accounts = load_wechat_accounts()
    items, errors = [], []
    if not accounts:
        return items, ["未配置任何公众号（在「设置」中添加 名称|gh_id）"]
    if not RSSHUB_BASE or RSSHUB_BASE.startswith("https://rsshub.app"):
        # 公共实例不稳定，先试探一次，避免每个账号都超时浪费时间
        pass
    kws = _split_keywords(keywords)
    async with httpx.AsyncClient(timeout=25, headers=UA, follow_redirects=True) as client:
        for name, gh in accounts:
            url = f"{RSSHUB_BASE}/wechat/mp/{gh}"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                text = resp.text
                if not _looks_like_rss(text):
                    errors.append(f"{name}：RSSHub 返回非 RSS（实例不可用/路由不支持）")
                    continue
                got = _parse_rss_items(text, days, keyword_filter=None,
                                       source_hint=f"微信·{name}", max_results=0)
                if got:
                    items.extend(got)
                else:
                    errors.append(f"{name}：近期无条目")
            except Exception as e:
                errors.append(f"{name}：{e}")
                continue
    if kws and items:
        filtered = []
        for it in items:
            hay = (it["title"] + " " + it.get("summary", "")).lower()
            if any(k in hay for k in kws):
                filtered.append(it)
        items = filtered
    return items, errors


def _parse_rss_items(raw: str, days: int, keyword_filter=None, source_hint=None,
                     fallback_source=None, max_results: int = 30):
    """解析 RSS/Atom XML，做时间过滤与 GitHub 链接提取。"""
    try:
        root = ET.fromstring(raw)
    except Exception:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days and days > 0 else None
    results = []
    for item in root.findall(".//item") + root.findall(".//{*}entry"):
        # Atom entry 与 RSS item 字段差异处理
        title = (item.findtext("title") or item.findtext("{*}title") or "").strip()
        link = (item.findtext("link") or "").strip()
        # Atom 的 link 常以属性形式存在
        if not link:
            le = item.find("{http://www.w3.org/2005/Atom}link")
            if le is not None:
                link = le.get("href", "")
        pub = (item.findtext("pubDate") or item.findtext("published")
               or item.findtext("updated") or item.findtext("{*}published")
               or item.findtext("{*}updated") or "").strip()
        src_el = item.find("source")
        source = (src_el.text.strip() if src_el is not None and src_el.text
                  else "") or (source_hint or fallback_source or "")
        desc = (item.findtext("description") or item.findtext("{*}summary")
                or item.findtext("{*}content") or "").strip()
        # 去除 HTML 标签，仅留纯文本摘要
        desc_text = re.sub(r"<[^>]+>", " ", desc)
        desc_text = re.sub(r"\s+", " ", desc_text).strip()
        if cutoff:
            pd = _parse_pubdate(pub)
            if pd and pd < cutoff:
                continue
        gh = GITHUB_RE.search(desc) or GITHUB_RE.search(title)
        results.append({
            "title": title,
            "url": link,
            "published": pub,
            "source": source,
            "summary": desc_text[:300],
            "github": gh.group(0) if gh else "",
        })
        if max_results and len(results) >= max_results:
            break
    return results


async def fetch_news(keywords: str, days: int = 2, max_results: int = 30,
                     lang: str = "en", channel: str = "google",
                     progress=None):
    """
    资讯检索主入口（多源并行）。
    channel: google=仅 Google News；cn=国内媒体+公众号；all=全部合并。
    返回 {results, sources}：sources 含每个渠道的成功/失败诊断。

    progress: 可选回调 (stage, status, detail)，stage ∈ google / rss / wx，
    供后台任务逐源上报步骤进度；不传则行为与原来完全一致。
    """
    def _report(stage: str, status: str, detail: str = ""):
        if progress:
            try:
                progress(stage, status, detail)
            except Exception:
                pass                                   # 进度上报失败不影响检索

    collected, sources = [], []
    seen = set()
    seen_titles = set()

    async def _drain(items, src_name):
        ok = 0
        for it in items:
            if not it.get("url"):
                continue
            if it["url"] in seen:
                continue
            # 同一 feed 偶尔重复同一篇，用标题做二次去重
            tkey = it.get("title", "").strip().lower()
            if tkey and tkey in seen_titles:
                continue
            seen.add(it["url"])
            if tkey:
                seen_titles.add(tkey)
            collected.append(it)
            ok += 1
        return ok

    if channel in ("google", "all"):
        _report("google", "running")
        items, err = await _fetch_google_news(keywords, days, max_results, lang=lang)
        ok = await _drain(items, "Google News")
        sources.append({"name": "Google News", "ok": ok,
                        "status": "ok" if not err else "error", "detail": err or f"命中 {ok} 条"})
        _report("google", "ok" if not err else "error", err or f"命中 {ok} 条")

    if channel in ("cn", "all"):
        _report("rss", "running")
        items, errors = await _fetch_cn_rss(keywords, days, max_results)
        ok = await _drain(items, "国内媒体 RSS")
        if errors:
            for e in errors:
                sources.append({"name": e.split("：")[0], "ok": 0, "status": "error", "detail": e})
        else:
            sources.append({"name": "国内媒体 RSS", "ok": ok, "status": "ok", "detail": f"命中 {ok} 条"})
        _report("rss", "ok" if not errors else "error",
                f"命中 {ok} 条" if not errors else "；".join(errors)[:200])

        # 微信公众号（RSSHub）
        _report("wx", "running")
        witems, werrors = await _fetch_wechat_rsshub(keywords, days, max_results)
        wok = await _drain(witems, "微信公众号")
        if werrors:
            for e in werrors:
                sources.append({"name": e.split("：")[0], "ok": 0, "status": "error", "detail": e})
        else:
            sources.append({"name": "微信公众号", "ok": wok, "status": "ok", "detail": f"命中 {wok} 条"})
        _report("wx", "ok" if not werrors else "error",
                f"命中 {wok} 条" if not werrors else "；".join(werrors)[:200])

    # 按时间倒序
    collected.sort(key=lambda x: _parse_pubdate(x.get("published", "")) or datetime.min, reverse=True)
    return {"results": collected[:max_results] if max_results else collected, "sources": sources}
