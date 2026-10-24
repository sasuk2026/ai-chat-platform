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

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from .search import web_search

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="AI Chat Platform", docs_url="/docs")

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

    # 联网搜索：取最后一条用户消息作为查询
    searches = []
    if search_enabled and messages:
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        if last_user:
            searches = await web_search(
                last_user,
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
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    async def gen():
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                async with client.stream(
                    "POST", api_base + "/chat/completions",
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
            r = await client.get(base + "/models", headers=headers)
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
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
