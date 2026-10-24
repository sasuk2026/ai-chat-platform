"""网页搜索：DuckDuckGo（ddgs 库，快且稳）为主，Bing/Google 爬虫兜底；支持网络代理。"""
import asyncio
import re
from html import unescape
from urllib.parse import unquote

import httpx

# ddgs 已在 requirements.txt 声明；优先用它（直连 DDG 接口，速度远快于爬 html 页面）
try:
    from ddgs import DDGS
    DDGS_AVAILABLE = True
except Exception:
    DDGS_AVAILABLE = False

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
    r = await client.get(url, headers=UA, timeout=10, follow_redirects=True)
    r.raise_for_status()
    return r.text


async def _ddgs_search(query: str, max_results: int = 6) -> list:
    """用 ddgs 库直连 DuckDuckGo（快且稳定），放到线程里避免阻塞事件循环。"""
    def _run():
        with DDGS() as ddgs:
            return ddgs.text(query, max_results=max_results)
    try:
        raw = await asyncio.to_thread(_run)
    except Exception:
        return []
    out = []
    for r in (raw or []):
        title = (r.get("title") or "").strip()
        url = r.get("href") or r.get("url") or ""
        snippet = r.get("body") or ""
        if title and url:
            out.append({"title": title, "url": url, "snippet": snippet})
    return out


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
    r = await client.get("https://api.duckduckgo.com/", params={"format": "json", "no_html": 1, "q": query}, timeout=10)
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


async def web_search(query: str, engine: str = "duckduckgo", proxy: str = "", max_results: int = 6, timeout: float = 8) -> list:
    """多源并发搜索，取首个非空结果；单次与整体均设硬超时，避免卡死。

    - 首选 ddgs（直连 DuckDuckGo 接口，秒回、稳定），速度远快于爬 html 页面。
    - 用户指定 bing/google 时，将其爬虫作为 ddgs 失败后的兜底（与原逻辑一致）。
    - 配置代理时（如访问 Google）走原代理模板兜底。
    """
    query = (query or "").strip()
    if not query:
        return []

    async def run(coro):
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except Exception:
            return None

    async with httpx.AsyncClient(headers=UA, follow_redirects=True, timeout=timeout) as client:
        tasks = []
        # 首选：ddgs（最快最稳）
        if DDGS_AVAILABLE:
            tasks.append(run(_ddgs_search(query, max_results)))
        # 用户指定引擎的爬虫：ddgs 失败时兜底
        if engine == "bing":
            tasks.append(run(search_bing(client, query)))
        elif engine == "google":
            tasks.append(run(search_google(client, query)))
            tasks.append(run(search_bing(client, query)))
        else:  # duckduckgo
            tasks.append(run(search_ddg_api(client, query)))
            tasks.append(run(search_ddg(client, query)))
        # 配置代理且 ddgs 不可用时，补一个代理兜底
        if proxy and not DDGS_AVAILABLE:
            for fn in (search_google, search_bing, search_ddg_api):
                tasks.append(run(_proxy_search(client, fn, query, proxy)))

        # 并发执行，谁先返回有效结果就用谁
        for fut in asyncio.as_completed(tasks):
            res = await fut
            if res:
                return res[:max_results]
    return []


async def _proxy_search(client, fn, query, proxy):
    """代理模式下经代理模板重试（保留原代理能力）。"""
    target = apply_proxy(proxy, _base_url(fn, query))
    raw = await _fetch(client, target)
    return await _parse(fn, client, query, raw)


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
