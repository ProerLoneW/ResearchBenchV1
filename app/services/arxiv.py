"""
arXiv 论文发现服务（无需 API Key）。
按关键词 + 时间范围检索最新论文，解析标题/摘要/时间/链接/GitHub。
"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from ..config import arxiv_api_base, ARXIV_SOURCES, ARXIV_SOURCE

GITHUB_RE = re.compile(r"https?://github\.com/[^\s)\]\"'>]+", re.I)

# fallback 顺序：当前选定源 → 其余国内直连镜像 → 主站
def _arxiv_fallback_order() -> list:
    order = [ARXIV_SOURCE]
    for k, (_, _, cn) in ARXIV_SOURCES.items():
        if k != ARXIV_SOURCE and cn:
            order.append(k)
    # 最后兜底到主站
    for k in ("arxiv", "export"):
        if k not in order:
            order.append(k)
    return order


def _build_query(keywords: str, days: int):
    kws = [k.strip() for k in keywords.split(",") if k.strip()]
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).strftime("%Y%m%d%H%M")
    end = now.strftime("%Y%m%d%H%M")
    # 关键词为空时，不做关键词约束，仅按时间范围检索（arXiv 不支持 all:* 语法，会 500）
    if kws:
        term = " OR ".join(f'all:"{k}"' for k in kws)
        kw_part = f"({term})"
        return f"{kw_part} AND submittedDate:[{start} TO {end}]"
    return f"submittedDate:[{start} TO {end}]"


async def fetch_papers(keywords: str, days: int = 2, max_results: int = 30):
    query = _build_query(keywords, days)
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    raw = ""
    last_err = None
    for key in _arxiv_fallback_order():
        base = ARXIV_SOURCES[key][1]
        # 不再显式传 proxy=RADAR_PROXY：httpx 显式代理在 macOS + 该本地代理下会触发
        # SSL record layer failure；改走默认 trust_env，让它从 HTTP_PROXY/HTTPS_PROXY
        # 环境变量读代理。config.py 在 save/load 时已同步文件代理到环境变量。
        for attempt in range(2):          # 同一源偶发握手失败，重试一次
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    resp = await client.get(base, params=params)
                    if resp.status_code == 429:
                        last_err = httpx.HTTPError("arXiv 返回 429（请求过于频繁，请稍后重试；共享代理 IP 易被限流）")
                        break
                    resp.raise_for_status()
                    raw = resp.text
                    break
            except Exception as e:
                last_err = e
                continue
        if raw:
            break
    if not raw:
        raise last_err or httpx.HTTPError("所有 arXiv 源均无法访问")

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    root = ET.fromstring(raw)
    results = []
    for entry in root.findall("atom:entry", ns):
        title = " ".join(entry.findtext("atom:title", "", ns).split())
        summary = " ".join(entry.findtext("atom:summary", "", ns).split())
        published = entry.findtext("atom:published", "", ns)
        id_url = entry.findtext("atom:id", "", ns)
        arxiv_id = id_url.rsplit("/", 1)[-1] if id_url else ""
        # 主分类
        prim = entry.find("arxiv:primary_category", ns)
        category = prim.get("term") if prim is not None else ""
        # GitHub 链接（摘要中常见）
        gh = GITHUB_RE.search(summary) or GITHUB_RE.search(title)
        github = gh.group(0) if gh else ""
        results.append({
            "title": title,
            "abstract": summary,
            "published": published,
            "url": id_url,
            "arxiv_id": arxiv_id,
            "category": category,
            "github": github,
            "authors": [a.findtext("atom:name", "", ns)
                        for a in entry.findall("atom:author", ns)],
        })
    return results
