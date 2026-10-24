"""网页搜索：DuckDuckGo（ddgs 库，快且稳）为主，Bing/Google 爬虫兜底；支持网络代理。

性能设计（关键）：
1. 结果缓存：相同 query+engine 在 TTL 内直接命中，避免重复网络往返。
2. 分级超时（staged race）：
   - 第一波只跑最快的两个源（ddgs / DDG Instant Answer），短超时（默认 2.5s）；
   - 第一波无结果才启动第二波爬虫兜底（Bing / Google HTML）；
   - 整体硬超时（默认 6s）到了立刻返回，绝不拖到请求超时。
3. 重复查询去重：并发的同一 query 共享一次真实请求（inflight 合并）。
4. 连接池复用，避免每次搜索重建 TCP/TLS。
"""
import asyncio
import base64
import re
import time
from collections import OrderedDict
from html import unescape
from urllib.parse import unquote, quote

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

# ---------------- 可调参数 ----------------
CACHE_TTL = 300          # 缓存有效期（秒）
CACHE_MAX = 256          # 缓存最多保留多少条
FAST_TIMEOUT = 2.5       # 第一波（快源）超时
TOTAL_TIMEOUT = 6.0      # 整体硬超时

# 并发友好的连接池：复用 TCP/TLS，避免每次搜索重新握手
_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10,
                       keepalive_expiry=15.0)

# ---------------- 缓存（TTL + LRU） ----------------
_cache = OrderedDict()


def _cache_key(query: str, engine: str, max_results: int) -> tuple:
    return (query.strip().lower(), engine, max_results)


def _cache_get(key):
    item = _cache.get(key)
    if not item:
        return None
    ts, val = item
    if time.monotonic() - ts > CACHE_TTL:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return val


def _cache_set(key, val):
    _cache[key] = (time.monotonic(), val)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)


# ---------------- 并发去重（同一 query 只打一次网络） ----------------
_inflight = {}


def _clean(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def _enc(target: str) -> str:
    return quote(target, safe="")


def _decode_bing_url(href: str) -> str:
    """Bing 的结果是 bing.com/ck/a?...&u=a1<base64> 跳转链，需解码出真实地址，
    否则用户点开的是一个 Bing 中转页而不是目标网站。"""
    try:
        if "bing.com/ck/a" in href or "u=a1" in href:
            m = re.search(r"[?&]u=a1([^&]+)", href)
            if m:
                b64 = m.group(1)
                real = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", "ignore")
                if real.startswith(("http://", "https://")):
                    return real
    except Exception:
        pass
    return href


def _clean_bing_result(title: str, href: str, snippet: str) -> dict:
    """统一处理 Bing 单条结果：解码跳转链，丢弃无法还原真实地址的条目。"""
    if not title:
        return None
    real = _decode_bing_url(href)
    if real.startswith(("https://www.bing.com/ck/a", "http://www.bing.com/ck/a")):
        return None
    return {"title": title, "url": real, "snippet": snippet}


def apply_proxy(proxy: str, target: str) -> str:
    """proxy 模板：含 {url} 则替换为编码后的目标地址；否则前缀拼接。"""
    if not proxy:
        return target
    if "{url}" in proxy:
        return proxy.replace("{url}", _enc(target))
    return proxy + _enc(target)


async def _fetch(client: httpx.AsyncClient, url: str, timeout: float = 5.0) -> str:
    r = await client.get(url, headers=UA, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.text


# ---------------- ddgs（首选，最快最稳） ----------------
_ddgs_lock = asyncio.Lock()
_ddgs_obj = None


def _ddgs_new():
    return DDGS()


async def _ddgs_search(query: str, max_results: int = 6) -> list:
    """用 ddgs 库直连 DuckDuckGo；放线程里避免阻塞事件循环，并复用实例减少握手开销。"""
    global _ddgs_obj

    def _run(ddgs):
        return ddgs.text(query, max_results=max_results)

    try:
        async with _ddgs_lock:  # ddgs 非线程安全，串行访问
            if _ddgs_obj is None:
                _ddgs_obj = _ddgs_new()
            try:
                raw = await asyncio.to_thread(_run, _ddgs_obj)
            except Exception:
                _ddgs_obj = _ddgs_new()          # 实例坏了就重建再试一次
                raw = await asyncio.to_thread(_run, _ddgs_obj)
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


async def search_ddg(client, query, timeout: float = 5.0):
    html = await _fetch(client, "https://html.duckduckgo.com/html/?q=" + _enc(query), timeout)
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


async def search_ddg_api(client, query, timeout: float = 5.0):
    r = await client.get("https://api.duckduckgo.com/", params={"format": "json", "no_html": 1, "q": query}, timeout=timeout)
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


async def search_bing(client, query, timeout: float = 5.0):
    html = await _fetch(client, "https://www.bing.com/search?q=" + _enc(query) + "&count=10", timeout)
    results = []
    for m in re.finditer(r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>(.*?)(?=<li class="b_algo"|</ol>)', html, re.S):
        if len(results) >= 6:
            break
        href, title, rest = m.group(1), _clean(m.group(2)), m.group(3)
        sm = re.search(r"<p[^>]*>(.*?)</p>", rest, re.S)
        snippet = _clean(sm.group(1)) if sm else ""
        item = _clean_bing_result(title, href, snippet)
        if item:
            results.append(item)
    if not results:
        raise ValueError("bing empty")
    return results


async def search_google(client, query, timeout: float = 5.0):
    html = await _fetch(client, "https://www.google.com/search?q=" + _enc(query) + "&num=10&hl=zh-CN", timeout)
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


async def _guard(coro):
    """把任意异常/超时吞掉，返回 None，保证 race 不会被单个源拖死。"""
    try:
        return await coro
    except asyncio.CancelledError:
        return None
    except Exception:
        return None


async def _race(coros, timeout: float):
    """并发跑一组搜索源，返回第一个非空结果；超时或全失败返回 []。

    关键点：一旦拿到结果立刻取消其余任务，不再浪费时间等待。
    """
    if not coros:
        return []
    tasks = [asyncio.ensure_future(_guard(c)) for c in coros]
    deadline = time.monotonic() + timeout
    try:
        pending = set(tasks)
        while pending:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            done, pending = await asyncio.wait(pending, timeout=remain,
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                res = t.result()
                if res:
                    return res
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        # 让被取消的任务有机会清理，避免 "Task was destroyed" 警告
        await asyncio.gather(*tasks, return_exceptions=True)
    return []


def _make_proxy_client(proxy: str, timeout: float):
    """把代理栏填的内容当作真正的代理服务器（http/https/socks5）来用。

    兼容 httpx 新旧参数名（新版 proxy=，旧版 proxies=）；失败返回 None，
    调用方会自动回落到直连搜索，不会因为代理不可用就搜不出东西。
    """
    kwargs = dict(headers=UA, follow_redirects=True, timeout=timeout, limits=_LIMITS)
    for key in ("proxy", "proxies"):
        try:
            return httpx.AsyncClient(**kwargs, **{key: proxy})
        except TypeError:
            continue
        except Exception:
            return None
    return None


async def _search_once(client, query: str, engine: str, proxy: str, max_results: int) -> list:
    """执行一次真实搜索：分级超时，快源优先，慢源兜底。

    重要修正：
    1. 代理支持两种填法——「真代理服务器」（http/https/socks5）与「URL 模板」（含 {url}）。
       以前把代理一律当模板拼 URL，导致填了代理就搜不出东西（且用了一个不存在的 run()，直接崩）。
    2. 代理跑不出结果时，必须继续走直连兜底（Bing/DDG），绝不就此返回空。
    """
    started = time.monotonic()

    def _left():
        return max(0.3, TOTAL_TIMEOUT - (time.monotonic() - started))

    # 真代理服务器（非模板）→ 单独起一个走代理的 client
    pclient = None
    if proxy and "{url}" not in proxy:
        pclient = _make_proxy_client(proxy, min(TOTAL_TIMEOUT, _left()))

    try:
        # ---- 第一波：最快路径并发 ----
        wave1 = []
        if DDGS_AVAILABLE:
            wave1.append(_ddgs_search(query, max_results))
        wave1.append(search_ddg_api(client, query, timeout=min(FAST_TIMEOUT, _left())))
        if pclient is not None:
            wave1.append(search_ddg_api(pclient, query, timeout=min(FAST_TIMEOUT, _left())))
            wave1.append(search_bing(pclient, query, timeout=min(FAST_TIMEOUT, _left())))

        results = await _race(wave1, timeout=min(FAST_TIMEOUT, _left()))
        if results:
            return results

        # ---- 模板型代理兜底（只有含 {url} 才适用）----
        if proxy and "{url}" in proxy:
            results = await _race(
                [_proxy_search(client, fn, query, proxy) for fn in (search_bing, search_google)],
                timeout=min(3.0, _left()),
            )
            if results:
                return results

        # ---- 第二波：直连爬虫兜底（无论是否配代理，都要走到这一步）----
        wave2 = []
        if engine == "bing":
            wave2.append(search_bing(client, query, timeout=min(4.0, _left())))
            wave2.append(search_ddg(client, query, timeout=min(4.0, _left())))
        elif engine == "google":
            wave2.append(search_google(client, query, timeout=min(4.0, _left())))
            wave2.append(search_bing(client, query, timeout=min(4.0, _left())))
        else:                                   # duckduckgo → 必须补 Bing 兜底
            wave2.append(search_ddg(client, query, timeout=min(4.0, _left())))
            wave2.append(search_bing(client, query, timeout=min(4.0, _left())))
        if pclient is not None:
            wave2.append(search_bing(pclient, query, timeout=min(4.0, _left())))

        return await _race(wave2, timeout=_left())
    finally:
        if pclient is not None:
            try:
                await pclient.aclose()
            except Exception:
                pass


async def web_search(query: str, engine: str = "duckduckgo", proxy: str = "",
                     max_results: int = 6, timeout: float = TOTAL_TIMEOUT) -> list:
    """多源并发搜索，取首个非空结果；带缓存 + 并发去重 + 分级超时。

    - 首选 ddgs（直连 DuckDuckGo 接口，秒回、稳定）。
    - 短超时内无结果才启用 HTML 爬虫兜底（Bing / Google）。
    - 相同 query 的并发请求共享一次真实搜索，重复查询直接读缓存。
    """
    query = (query or "").strip()
    if not query:
        return []

    key = _cache_key(query, engine, max_results)
    hit = _cache_get(key)
    if hit is not None:
        return list(hit)

    # 并发去重：同一 key 只放行一次真实网络请求，其余等待同一个 Future
    loop = asyncio.get_event_loop()
    existing = _inflight.get(key)
    if existing is not None:
        try:
            res = await asyncio.wait_for(asyncio.shield(existing), timeout=timeout + 1)
            return list(res)[:max_results]
        except Exception:
            return []

    fut = loop.create_future()
    _inflight[key] = fut
    try:
        async with httpx.AsyncClient(headers=UA, follow_redirects=True,
                                     timeout=timeout, limits=_LIMITS) as client:
            results = (await _search_once(client, query, engine, proxy, max_results))[:max_results]
    except Exception:
        results = []

    _cache_set(key, results)
    if not fut.done():
        fut.set_result(results)
    _inflight.pop(key, None)
    return list(results)


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
    """代理模式下复用各引擎的解析逻辑。"""
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
        item = _clean_bing_result(_clean(m.group(2)), m.group(1), _clean(sm.group(1)) if sm else "")
        if item:
            results.append(item)
    return results


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
