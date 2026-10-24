"""AI Chat Platform - Agno AgentOS 后端

- /api/chat   : OpenAI 协议流式透传（前端可动态配置 BaseURL/Key/Model）
- /api/search : 网页搜索（DDG/Bing/Google + 代理）
- /agno       : Agno AgentOS 托管的智能体 API（通过环境变量配置模型）
- /           : 静态聊天前端

托管方式见 README.md（Railway 一键部署 / 本地 uvicorn）。
"""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from .search import web_search

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="AI Chat Platform", docs_url="/docs")

def normalize_endpoint(api_base: str, action: str = "chat/completions"):
    """兼容两种填法：
    1. Base 地址： https://host/v1            -> https://host/v1/chat/completions
    2. 完整接口： https://host/v1/chat/completions?sharecode=x&ig_key=y
       （保留全部查询参数，仅拼接 action）
    """
    api_base = api_base.strip().rstrip("/")
    parts = urlsplit(api_base)
    path = parts.path or ""
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    if path.endswith("/models"):
        path = path[: -len("/models")]
    url = urlunsplit((parts.scheme, parts.netloc, path.rstrip("/") + "/" + action, parts.query, ""))
    return url


# 搜索上下文上限：太长会显著拖慢模型首字输出（TTFT），这里做硬截断
MAX_SEARCH_CONTEXT = 6000


def build_search_context(searches: list) -> str:
    """把搜索结果拼成系统提示。限制总长度，避免长摘要拖慢模型首字响应。"""
    lines, total = [], 0
    for i, s in enumerate(searches):
        snippet = (s.get("snippet") or "").strip()
        piece = f"[{i+1}] {s.get('title','')}\n{s.get('url','')}\n{snippet}"
        if total + len(piece) > MAX_SEARCH_CONTEXT:
            remain = MAX_SEARCH_CONTEXT - total
            if remain > 120:                       # 还有空间就截断最后一条
                lines.append(piece[:remain] + "…")
            break
        lines.append(piece)
        total += len(piece)
    return "以下是实时网页搜索结果，请结合它们回答并标注引用 [n]：\n" + "\n\n".join(lines)


# ---------------- OpenAI 协议流式透传 ----------------
@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    api_base = (body.get("apiBase") or "https://api.openai.com/v1").rstrip("/")
    api_key = body.get("apiKey") or ""
    model = body.get("model") or "gpt-4o-mini"
    temperature = float(body.get("temperature") or 0.7)
    messages = body.get("messages") or []
    search_enabled = bool(body.get("search"))

    # 联网搜索：取最后一条用户消息作为查询。
    # 关键优化：前端 /api/search 已经搜过一次时，会通过 searchResults 把结果传回来，
    # 这里直接复用，避免同一个 query 重复搜索两次（原先耗时直接翻倍）。
    # 用 isinstance 区分"前端没传"和"前端传了空结果"：
    # 只要前端已经搜过（哪怕是 0 条），后端就不再重复搜索。
    provided = body.get("searchResults")
    searches = provided if isinstance(provided, list) else []
    if search_enabled and messages and not isinstance(provided, list):
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        if last_user:
            searches = await web_search(
                last_user,
                engine=body.get("searchEngine") or "duckduckgo",
                proxy=body.get("proxy") or "",
            )
    if searches:
        ctx = build_search_context(searches)
        messages = [{"role": "system", "content": ctx}] + messages

    payload = {
        "model": model,
        "temperature": temperature,
        "stream": True,
        "messages": messages,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    async def gen():
        try:
            url = normalize_endpoint(api_base, "chat/completions")
            async with httpx.AsyncClient(timeout=180) as client:
                async with client.stream(
                    "POST", url,
                    json=payload, headers=headers,
                ) as resp:
                    if resp.status_code >= 300:
                        err = (await resp.aread()).decode(errors="replace")[:400]
                        yield "data: " + json.dumps({"error": f"HTTP {resp.status_code}: {err}"}) + "\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if line.strip().startswith("data:"):
                            yield line + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"error": str(e)}) + "\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------- 抓取网址内容（插件源，绕过 CORS） ----------------
@app.post("/api/fetch-url")
async def fetch_url(request: Request):
    from .search import UA
    body = await request.json()
    url = (body.get("url") or "").strip()
    proxy = body.get("proxy") or ""
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "URL 需以 http(s):// 开头"})
    if "{url}" in proxy:
        from urllib.parse import quote
        url = proxy.replace("{url}", quote(url, safe=""))
    try:
        import re as _re
        from html import unescape as _u
        async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
            r = await client.get(url, headers=UA)
            r.raise_for_status()
            text = r.text
            # 若为 HTML，抽取正文纯文本
            if "<" in text[:2000]:
                text = _re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\\1>", " ", text)
                text = _re.sub(r"(?s)<[^>]+>", "\n", text)
                text = _u(_re.sub(r"\n{3,}", "\n\n", text))
            text = text.strip()[:20000]
            return JSONResponse({"ok": True, "length": len(text), "content": text})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


# ---------------- 配置诊断：用你的 key 调 /models，返回服务商原始响应 ----------------
@app.post("/api/test-config")
async def test_config(request: Request):
    body = await request.json()
    base = (body.get("apiBase") or "").rstrip("/")
    key = body.get("apiKey") or ""
    if not base:
        return JSONResponse({"ok": False, "error": "未填 API 地址"})
    headers = {}
    if key:
        headers["Authorization"] = "Bearer " + key.strip()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(normalize_endpoint(base, "models"), headers=headers)
            text = r.text[:300]
            models = []
            try:
                ms = (r.json().get("data") or [])
                models = [m.get("id") for m in ms][:10]
            except Exception:
                pass
            return JSONResponse({"ok": r.status_code == 200, "status": r.status_code,
                                 "provider_response": text, "sample_models": models})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


# ---------------- 网页搜索 ----------------
@app.post("/api/search")
async def search(request: Request):
    body = await request.json()
    query = body.get("query") or ""
    engine = body.get("engine") or "duckduckgo"
    proxy = body.get("proxy") or ""
    results = await web_search(query, engine=engine, proxy=proxy)
    return JSONResponse({"results": results})


# ---------------- Agno AgentOS（可选，环境变量配置） ----------------
AGENTOS_AVAILABLE = False
try:
    from agno.agent import Agent
    from agno.models.openai import OpenAIChat
    from agno.os import AgentOS
    from agno.tools.duckduckgo import DuckDuckGoTools

    agno_agent = Agent(
        name="平台助手",
        model=OpenAIChat(id=os.environ.get("AGENT_MODEL", "gpt-4o-mini")),
        tools=[DuckDuckGoTools()],
        instructions=["回答使用 Markdown", "需要最新信息时使用搜索工具"],
        markdown=True,
    )
    agent_os = AgentOS(agents=[agno_agent])
    agentos_app = agent_os.get_app()
    app.mount("/agno", agentos_app, name="agno-agentos")
    AGENTOS_AVAILABLE = True
except Exception as _e:  # agno 未安装或配置不完整时，主功能不受影响
    import logging
    logging.getLogger(__name__).warning("AgentOS 未启用: %s", _e)


@app.get("/api/agentos-status")
async def agentos_status():
    return {"agentos": AGENTOS_AVAILABLE}


# ---------------- 静态前端 ----------------
# 禁用缓存：改完代码重新部署后，用户刷新即可拿到新版本（不会被旧页面/旧门禁卡住）
_NO_CACHE = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


class NoCacheStatic(StaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers.update(_NO_CACHE)
        return resp


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)


app.mount("/static", NoCacheStatic(directory=str(STATIC_DIR)), name="static")
