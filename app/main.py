"""AI Chat Platform - Agno AgentOS 后端

- /api/chat   : OpenAI 协议流式透传（前端可动态配置 BaseURL/Key/Model）
- /api/search : 网页搜索（DDG/Bing/Google + 代理）
- /agno       : Agno AgentOS 托管的智能体 API（通过环境变量配置模型）
- /           : 静态聊天前端

托管方式见 README.md（Railway 一键部署 / 本地 uvicorn）。
"""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from .search import web_search, diagnose, clean_query

STATIC_DIR = Path(__file__).parent / "static"


def _msg_text(content) -> str:
    """从用户消息里抽取纯文本：兼容纯字符串与多模态数组（图文混合）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""

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
    for suffix in ("/chat/completions", "/images/generations", "/models"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    url = urlunsplit((parts.scheme, parts.netloc, path.rstrip("/") + "/" + action, parts.query, ""))
    return url


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
    tools = body.get("tools")
    tool_choice = body.get("tool_choice")

    # 联网搜索：取最后一条用户消息作为查询（兼容图文混合消息）
    searches = []
    if search_enabled and messages:
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        last_user_text = _msg_text(last_user)
        if last_user_text:
            searches = await web_search(
                last_user_text,
                engine=body.get("searchEngine") or "duckduckgo",
                proxy=body.get("proxy") or "",
            )
            if searches:
                ctx = "以下是实时网页搜索结果，请结合它们回答并标注引用 [n]：\n" + "\n\n".join(
                    f"[{i+1}] {s['title']}\n{s['url']}\n{s['snippet']}" for i, s in enumerate(searches)
                )
                messages = [{"role": "system", "content": ctx}] + messages

    payload = {
        "model": model,
        "temperature": temperature,
        "stream": True,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
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
    q_used = clean_query(query) or query
    filter_adult = bool(body.get("filter_adult", True))
    results = await web_search(q_used, engine=engine, proxy=proxy, filter_adult=filter_adult)
    return JSONResponse({"results": results, "query_used": q_used, "filter_adult": filter_adult})


@app.post("/api/search-debug")
async def search_debug(request: Request):
    """搜索体检：逐个测试各搜索源，返回线上真实可用情况。"""
    body = await request.json()
    q = (body.get("query") or "test").strip()
    px = body.get("proxy") or ""
    try:
        res = await diagnose(q, px)
        return JSONResponse({"ok": True, "result": res})
    except Exception as e:
        return JSONResponse({"ok": False, "error": repr(e)[:300]})


# ---------------- AI 生图（OpenAI images 协议） ----------------
@app.post("/api/generate-image")
async def generate_image(request: Request):
    """调用生图模型（OpenAI /images/generations 协议）生成图片。

    前端配置的 apiBase/apiKey 直接复用；model 用设置里的「生图模型」（如 dall-e-3 / gpt-image-1）。
    返回的图片统一转成 base64 dataURL，保证消息持久保存、不依赖外部链接。
    """
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    api_base = (body.get("apiBase") or "https://api.openai.com/v1").rstrip("/")
    api_key = body.get("apiKey") or ""
    model = body.get("model") or "dall-e-3"
    size = body.get("size") or "1024x1024"
    try:
        n = max(1, min(2, int(body.get("n") or 1)))
    except Exception:
        n = 1
    if not prompt:
        return JSONResponse({"ok": False, "error": "提示词为空"})
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    payload = {"model": model, "prompt": prompt[:4000], "n": n, "size": size}
    try:
        import base64 as _b64
        async with httpx.AsyncClient(timeout=150, follow_redirects=True) as client:
            r = await client.post(normalize_endpoint(api_base, "images/generations"),
                                  json=payload, headers=headers)
            if r.status_code >= 300:
                return JSONResponse({"ok": False,
                                     "error": f"HTTP {r.status_code}: {r.text[:300]}"})
            data = r.json() or {}
            items = data.get("data") or []
            images = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                if it.get("b64_json"):
                    images.append("data:image/png;base64," + it["b64_json"])
                elif it.get("url"):
                    # URL 形式：服务端下载后转 base64，前端消息里持久保存
                    try:
                        ir = await client.get(it["url"])
                        if ir.status_code == 200 and ir.content:
                            ct = (ir.headers.get("content-type") or "image/png").split(";")[0].strip()
                            if not ct.startswith("image/"):
                                ct = "image/png"
                            images.append(f"data:{ct};base64," + _b64.b64encode(ir.content).decode())
                        else:
                            images.append(it["url"])
                    except Exception:
                        images.append(it["url"])
            if not images:
                return JSONResponse({"ok": False, "error": "服务端未返回图片（data 为空）"})
            revised = ""
            if isinstance(items[0], dict):
                revised = items[0].get("revised_prompt") or ""
            return JSONResponse({"ok": True, "images": images, "revised_prompt": revised,
                                 "count": len(images)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


# ---------------- 插件发现：在 GitHub 搜索相关仓库 ----------------
@app.post("/api/discover-plugins")
async def discover_plugins(request: Request):
    """按查询词在 GitHub 搜索相关仓库（插件 / 工具 / bot），返回候选清单。"""
    body = await request.json()
    query = (body.get("query") or "").strip()
    limit = int(body.get("limit") or 8)
    token = (body.get("token") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not query:
        return JSONResponse({"ok": False, "error": "查询词为空"})
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ai-chat-platform"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc", "per_page": limit},
                headers=headers,
            )
            r.raise_for_status()
            items = (r.json() or {}).get("items", [])
            results = [{
                "name": it.get("name"),
                "full_name": it.get("full_name"),
                "description": it.get("description") or "",
                "html_url": it.get("html_url"),
                "stars": it.get("stargazers_count", 0),
                "language": it.get("language") or "",
                "topics": it.get("topics") or [],
            } for it in items]
            return JSONResponse({"ok": True, "count": len(results), "results": results})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


# ---------------- 插件发现：获取仓库 README 作为插件正文 ----------------
@app.post("/api/plugin-readme")
async def plugin_readme(request: Request):
    """获取某 GitHub 仓库的 README，作为插件正文（个性化设定）返回。"""
    body = await request.json()
    full_name = (body.get("full_name") or "").strip()
    token = (body.get("token") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not full_name or "/" not in full_name:
        return JSONResponse({"ok": False, "error": "需提供 owner/repo"})
    headers = {"Accept": "application/vnd.github.raw+json", "User-Agent": "ai-chat-platform"}
    if token:
        headers["Authorization"] = "Bearer " + token
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        # 1) 优先 GitHub API（带 token 时基本不会触发限流）
        try:
            r = await client.get(f"https://api.github.com/repos/{full_name}/readme", headers=headers)
            if r.status_code == 200 and r.text.strip():
                text = r.text.strip()[:8000]
                return JSONResponse({"ok": True, "content": text, "length": len(text)})
        except Exception:
            pass
        # 2) 兜底：直连 raw.githubusercontent.com（匿名也比 API 宽松，绕开 403 限流）
        for br in ("main", "master"):
            try:
                rr = await client.get(f"https://raw.githubusercontent.com/{full_name}/{br}/README.md")
                if rr.status_code == 200 and rr.text.strip():
                    text = rr.text.strip()[:8000]
                    return JSONResponse({"ok": True, "content": text, "length": len(text)})
            except Exception:
                continue
    return JSONResponse({"ok": False, "error": "无法获取该仓库 README（GitHub 限流，可在「设置」填写 GitHub Token 后重试）"})


# ---------------- 插件安装：深入抓取仓库内容（不止 README） ----------------
@app.post("/api/agent-browse")
async def agent_browse(request: Request):
    """深入某 GitHub 仓库，抓取适合作为插件正文的内容（README + 其他说明/提示词文档）。"""
    body = await request.json()
    full_name = (body.get("full_name") or "").strip()
    token = (body.get("token") or os.environ.get("GITHUB_TOKEN") or "").strip()
    max_len = int(body.get("max_len") or 6000)
    if not full_name or "/" not in full_name:
        return JSONResponse({"ok": False, "error": "需提供 owner/repo"})

    # 候选文档（按重要性排序）
    doc_candidates = [
        "README.md", "README_zh.md", "README_zh-CN.md", "USAGE.md", "GUIDE.md",
        "PROMPT.md", "PROMPTS.md", "SYSTEM.md", "AGENTS.md", "CONTRIBUTING.md",
        "docs/README.md", "docs/guide.md",
    ]

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        doc_paths = list(doc_candidates)
        # 带 token 时先用 contents API 列出根目录，挑出真正的 .md（更精准）
        if token:
            try:
                rr = await client.get(
                    f"https://api.github.com/repos/{full_name}/contents/",
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "ai-chat-platform", "Authorization": "Bearer " + token},
                )
                if rr.status_code == 200:
                    items = rr.json() or []
                    mds = [it["path"] for it in items if isinstance(it, dict) and str(it.get("name", "")).lower().endswith(".md")]
                    prioritized = [p for p in doc_candidates if p in mds]
                    others = [m for m in mds if m not in doc_candidates][:4]
                    doc_paths = (prioritized + others)[:6]
            except Exception:
                pass

        async def grab(path):
            # raw 匿名也比 API 宽松，直接抓，避开 403 限流
            for br in ("main", "master"):
                try:
                    g = await client.get(f"https://raw.githubusercontent.com/{full_name}/{br}/{path}")
                    if g.status_code == 200 and g.text.strip():
                        return (path, g.text.strip())
                except Exception:
                    continue
            return None

        results = await asyncio.gather(*[grab(p) for p in doc_paths])
        results = [r for r in results if r]

    if not results:
        return JSONResponse({"ok": False, "error": "无法获取该仓库内容（GitHub 限流，可在「设置」填写 GitHub Token 后重试）"})

    merged, total = [], 0
    for path, text in results:
        chunk = f"# 来自 {path}\n\n{text}\n\n"
        if total + len(chunk) > max_len:
            chunk = chunk[: max(0, max_len - total)]
        merged.append(chunk)
        total += len(chunk)
        if total >= max_len:
            break
    content = "".join(merged).strip()
    return JSONResponse({"ok": True, "content": content, "length": len(content), "files": [p for p, _ in results]})


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
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
