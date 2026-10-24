"""网页搜索：DuckDuckGo / Bing / Google，支持网络代理模板。"""
import re
from html import unescape
from urllib.parse import unquote

import httpx

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _clean(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def apply_proxy(proxy: str, target: str) -> str:
    """proxy 模板：含 {url} 则替换为编码后的目标地址；否则前缀拼接。"""
    if not proxy:
        return target
    if "{url}" in proxy:
        return proxy.replace("{url}", unquote(unquote("{url}")) if False else target and _enc(target))
    return proxy + _enc(target)


def _enc(target: str) -> str:
    from urllib.parse import quote
    return quote(target, safe="")


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url, headers=UA, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return r.text


async def search_ddg(client, query):
    html = await _fetch(client, "https://html.duckduckgo.com/html/?q=" + _enc(query))
    results = []
    for m in re.finditer(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]*class="[^"]*result__a|$)', html, re.S):
        if len(results) >= 6:
            break
        href, title = m.group(1), _clean(m.group(2))
        if href.startswith("//"):
            href = "https:" + href
        q = re.search(r"[?&]uddg=([^&]+)", href)
        if q:
            href = unquote(q.group(1))
        sm = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>', m.group(3), re.S)
        snippet = _clean(sm.group(1)) if sm else ""
        if title:
            results.append({"title": title, "url": href, "snippet": snippet})
    if not results:
        raise ValueError("ddg empty")
    return results


async def search_ddg_api(client, query):
    r = await client.get("https://api.duckduckgo.com/", params={"format": "json", "no_html": 1, "q": query}, timeout=20)
    r.raise_for_status()
    j = r.json()
    results = []
    if j.get("AbstractText"):
        results.append({"title": j.get("Heading") or query, "url": j.get("AbstractURL", ""), "snippet": j["AbstractText"]})
    for t in (j.get("RelatedTopics") or [])[:5]:
        if t.get("Text"):
            results.append({"title": t["Text"][:60], "url": t.get("FirstURL", ""), "snippet": t["Text"]})
    if not results:
        raise ValueError("ddg api empty")
    return results


async def search_bing(client, query):
    html = await _fetch(client, "https://www.bing.com/search?q=" + _enc(query) + "&count=10")
    results = []
    for m in re.finditer(r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>(.*?)(?=<li class="b_algo"|</ol>)', html, re.S):
        if len(results) >= 6:
            break
        href, title, rest = m.group(1), _clean(m.group(2)), m.group(3)
        sm = re.search(r"<p[^>]*>(.*?)</p>", rest, re.S)
        snippet = _clean(sm.group(1)) if sm else ""
        if title:
            results.append({"title": title, "url": href, "snippet": snippet})
    if not results:
        raise ValueError("bing empty")
    return results


async def search_google(client, query):
    html = await _fetch(client, "https://www.google.com/search?q=" + _enc(query) + "&num=10&hl=zh-CN")
    results = []
    for m in re.finditer(r'<a[^>]*href="(/url\?q=)?(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.S):
        if len(results) >= 6:
            break
        href = m.group(2)
        if "google." in href:
            continue
        inner = m.group(3)
        hm = re.search(r"<h3[^>]*>(.*?)</h3>", inner, re.S)
        title = _clean(hm.group(1)) if hm else ""
        if not title:
            continue
        results.append({"title": title, "url": href, "snippet": ""})
    if not results:
        raise ValueError("google empty (需要配置代理)")
    return results


RUNNERS = {
    "duckduckgo": [search_ddg, search_ddg_api],
    "bing": [search_bing, search_ddg_api],
    "google": [search_google, search_bing, search_ddg_api],
}


async def web_search(query: str, engine: str = "duckduckgo", proxy: str = "") -> list:
    """按引擎搜索，失败自动降级；配置代理时先经代理重试。"""
    runners = RUNNERS.get(engine, RUNNERS["duckduckgo"])

    async with httpx.AsyncClient() as client:
        # 1. 带代理尝试
        if proxy:
            for fn in runners:
                try:
                    target = apply_proxy(proxy, _base_url(fn, query))
                    html_or_json = await _fetch(client, target)
                    res = await _parse(fn, client, query, html_or_json)
                    if res:
                        return res
                except Exception:
                    continue
        # 2. 直连尝试
        for fn in runners:
            try:
                res = await fn(client, query)
                if res:
                    return res
            except Exception:
                continue
    return []


def _base_url(fn, query):
    name = fn.__name__
    if name == "search_ddg":
        return "https://html.duckduckgo.com/html/?q=" + _enc(query)
    if name == "search_bing":
        return "https://www.bing.com/search?q=" + _enc(query) + "&count=10"
    if name == "search_google":
        return "https://www.google.com/search?q=" + _enc(query) + "&num=10&hl=zh-CN"
    return "https://api.duckduckgo.com/?format=json&no_html=1&q=" + _enc(query)


async def _parse(fn, client, query, raw):
    """代理模式下复用各引擎的解析逻辑（通过内存替换）。"""
    # 简化处理：直接用解析函数的原始目标重新直连解析不可行，改为在代理分支返回原始文本解析
    name = fn.__name__
    if name == "search_ddg_api":
        import json
        j = json.loads(raw)
        results = []
        if j.get("AbstractText"):
            results.append({"title": j.get("Heading") or query, "url": j.get("AbstractURL", ""), "snippet": j["AbstractText"]})
        for t in (j.get("RelatedTopics") or [])[:5]:
            if t.get("Text"):
                results.append({"title": t["Text"][:60], "url": t.get("FirstURL", ""), "snippet": t["Text"]})
        return results
    if name == "search_ddg":
        return _parse_ddg_html(raw)
    if name == "search_bing":
        return _parse_bing_html(raw)
    if name == "search_google":
        return _parse_google_html(raw)
    return []


def _parse_ddg_html(html):
    results = []
    for m in re.finditer(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]*class="[^"]*result__a|$)', html, re.S):
        if len(results) >= 6:
            break
        href, title = m.group(1), _clean(m.group(2))
        if href.startswith("//"):
            href = "https:" + href
        q = re.search(r"[?&]uddg=([^&]+)", href)
        if q:
            href = unquote(q.group(1))
        sm = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>', m.group(3), re.S)
        results.append({"title": title, "url": href, "snippet": _clean(sm.group(1)) if sm else ""})
    return [r for r in results if r["title"]]


def _parse_bing_html(html):
    results = []
    for m in re.finditer(r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>(.*?)(?=<li class="b_algo"|</ol>)', html, re.S):
        if len(results) >= 6:
            break
        sm = re.search(r"<p[^>]*>(.*?)</p>", m.group(3), re.S)
        results.append({"title": _clean(m.group(2)), "url": m.group(1), "snippet": _clean(sm.group(1)) if sm else ""})
    return [r for r in results if r["title"]]


def _parse_google_html(html):
    results = []
    for m in re.finditer(r'<a[^>]*href="(/url\?q=)?(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.S):
        if len(results) >= 6:
            break
        href = m.group(2)
        if "google." in href:
            continue
        hm = re.search(r"<h3[^>]*>(.*?)</h3>", m.group(3), re.S)
        title = _clean(hm.group(1)) if hm else ""
        if title:
            results.append({"title": title, "url": href, "snippet": ""})
    return results
