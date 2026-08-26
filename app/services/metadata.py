"""
根据论文链接自动获取标题 / 摘要等公开信息。
- arXiv 链接：调用 arXiv API 精确获取。
- 其他链接：尽力抓取页面 <title> 与 meta description。
"""
import re
import xml.etree.ElementTree as ET

import httpx

ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.I)
GITHUB_RE = re.compile(r"https?://github\.com/[^\s)\]\"'>]+", re.I)
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


async def fetch_metadata(url: str) -> dict:
    url = (url or "").strip()
    if not url:
        return {}
    m = ARXIV_ID_RE.search(url)
    if m:
        return await _from_arxiv(m.group(1))
    return await _from_html(url)


async def _from_arxiv(aid: str) -> dict:
    api = "https://export.arxiv.org/api/query"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(api, params={"id_list": aid, "max_results": 1})
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    entry = root.find("atom:entry", NS)
    if entry is None:
        return {}
    title = " ".join(entry.findtext("atom:title", "", NS).split())
    summary = " ".join(entry.findtext("atom:summary", "", NS).split())
    prim = entry.find("arxiv:primary_category", NS)
    category = prim.get("term") if prim is not None else ""
    gh = GITHUB_RE.search(summary)
    return {
        "title": title,
        "abstract": summary,
        "arxiv_id": aid,
        "category": category,
        "github": gh.group(0) if gh else "",
        "original_url": f"https://arxiv.org/abs/{aid}",
    }


async def _from_html(url: str) -> dict:
    async with httpx.AsyncClient(timeout=30,
                                 headers={"User-Agent": "Mozilla/5.0"},
                                 follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = title_m.group(1).strip() if title_m else ""
    desc_m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        html, re.I | re.S)
    if not desc_m:
        desc_m = re.search(
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
            html, re.I | re.S)
    desc = desc_m.group(1).strip() if desc_m else ""
    gh = GITHUB_RE.search(html)
    return {
        "title": title,
        "abstract": desc,
        "arxiv_id": "",
        "category": "",
        "github": gh.group(0) if gh else "",
        "original_url": url,
    }
