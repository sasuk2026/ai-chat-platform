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
    # 透传「推理/思考开关」：
    # - Agnes AI 用 chat_template_kwargs.enable_thinking
    # - DashScope/Qwen 用顶层 enable_thinking
    # 两种都发，不认识的参数服务端会忽略
    if "enable_thinking" in body:
        payload["enable_thinking"] = bool(body.get("enable_thinking"))
    if "chat_template_kwargs" in body:
        ctk = body.get("chat_template_kwargs")
        if isinstance(ctk, dict):
            payload["chat_template_kwargs"] = ctk
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


# ---------------- GitHub 仓库读写（用户自带令牌） ----------------
GH_API = "https://api.github.com"
RENDER_API = "https://api.render.com/v1"


def _gh_headers(token: str) -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "ai-chat-platform"}
    if token:
        h["Authorization"] = "Bearer " + token.strip()
    return h


def _rh_headers(key: str) -> dict:
    return {"Accept": "application/json", "User-Agent": "ai-chat-platform",
            "Authorization": "Bearer " + key.strip()}


@app.post("/api/gh/repos")
async def gh_repos(request: Request):
    """列出用户旗下的 GitHub 仓库（最近更新的在前）。"""
    body = await request.json()
    token = (body.get("token") or "").strip()
    if not token:
        return JSONResponse({"ok": False, "error": "未提供 GitHub 令牌（请在设置里填写）"})
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.get(GH_API + "/user/repos",
                                 params={"per_page": 100, "sort": "updated", "affiliation": "owner"},
                                 headers=_gh_headers(token))
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"GitHub HTTP {r.status_code}: {r.text[:200]}"})
            items = r.json() or []
            repos = [{
                "name": it.get("full_name"),
                "private": bool(it.get("private")),
                "desc": (it.get("description") or "")[:120],
                "branch": it.get("default_branch") or "main",
                "url": it.get("html_url"),
                "updated": it.get("updated_at"),
            } for it in items]
            return JSONResponse({"ok": True, "count": len(repos), "repos": repos})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/gh/create-repo")
async def gh_create_repo(request: Request):
    """创建 GitHub 仓库（自动初始化 README，默认分支 main）。"""
    body = await request.json()
    token = (body.get("token") or "").strip()
    name = (body.get("name") or "").strip()
    if not token:
        return JSONResponse({"ok": False, "error": "未提供 GitHub 令牌"})
    if not name:
        return JSONResponse({"ok": False, "error": "仓库名不能为空"})
    payload = {
        "name": name,
        "private": bool(body.get("private")),
        "auto_init": True,
        "description": (body.get("description") or "由 AI 聊天平台自动创建")[:200],
    }
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(GH_API + "/user/repos", json=payload, headers=_gh_headers(token))
            # 422 = 仓库名已被占用：自动加随机后缀重试一次（不把麻烦扔给用户/AI）
            if r.status_code == 422 and "already exists" in (r.text or "").lower():
                import secrets as _secrets
                payload["name"] = f"{name[:80]}-{_secrets.token_hex(3)}"
                payload["description"] = (body.get("description") or "由 AI 聊天平台自动创建")[:200]
                r = await client.post(GH_API + "/user/repos", json=payload, headers=_gh_headers(token))
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"GitHub HTTP {r.status_code}: {r.text[:200]}"})
            it = r.json()
            renamed = it.get("name") != name
            return JSONResponse({"ok": True, "repo": it.get("full_name"),
                                 "branch": it.get("default_branch") or "main",
                                 "url": it.get("html_url"), "private": bool(it.get("private")),
                                 "renamed": renamed,
                                 "note": ("原名已被占用，已自动改名为 " + it.get("name")) if renamed else ""})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/gh/tree")
async def gh_tree(request: Request):
    """列出仓库文件树（递归，最多 300 个文件）。"""
    body = await request.json()
    token = (body.get("token") or "").strip()
    repo = (body.get("repo") or "").strip()
    branch = (body.get("branch") or "main").strip() or "main"
    if not repo or "/" not in repo:
        return JSONResponse({"ok": False, "error": "repo 需为 owner/name 格式"})
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.get(f"{GH_API}/repos/{repo}/git/trees/{branch}",
                                 params={"recursive": 1}, headers=_gh_headers(token))
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"GitHub HTTP {r.status_code}: {r.text[:200]}"})
            data = r.json() or {}
            files = [t.get("path") for t in (data.get("tree") or []) if t.get("type") == "blob"]
            dirs = [t.get("path") for t in (data.get("tree") or []) if t.get("type") == "tree" and t.get("path")]
            return JSONResponse({"ok": True, "repo": repo, "branch": branch,
                                 "truncated": bool(data.get("truncated")),
                                 "files": files[:300], "dirs": dirs[:60]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/gh/read")
async def gh_read(request: Request):
    """读取仓库中的文件内容（最多返回 8000 字符）。"""
    body = await request.json()
    token = (body.get("token") or "").strip()
    repo = (body.get("repo") or "").strip()
    path = (body.get("path") or "").strip().lstrip("/")
    branch = (body.get("branch") or "main").strip() or "main"
    if not repo or "/" not in repo or not path:
        return JSONResponse({"ok": False, "error": "需要 repo(owner/name) 和 path"})
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.get(f"{GH_API}/repos/{repo}/contents/{path}",
                                 params={"ref": branch}, headers=_gh_headers(token))
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"GitHub HTTP {r.status_code}: {r.text[:200]}"})
            data = r.json() or {}
            import base64 as _b64
            raw = (data.get("content") or "").replace("\n", "")
            try:
                text = _b64.b64decode(raw).decode("utf-8", errors="replace")
            except Exception:
                text = ""
            size = data.get("size") or len(text)
            return JSONResponse({"ok": True, "repo": repo, "path": path, "size": size,
                                 "sha": data.get("sha"),
                                 "content": text[:8000], "truncated": size > 8000})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/gh/write")
async def gh_write(request: Request):
    """写入（创建或更新）仓库文件；content 为完整文件内容（自动 base64）。

    mode=append：追加写（先读现有内容，拼接后整体提交）——用于大文件分段写入，
    避免模型单次输出被 max_tokens 截断导致文件残缺。文件不存在时等同创建。
    """
    body = await request.json()
    token = (body.get("token") or "").strip()
    repo = (body.get("repo") or "").strip()
    path = (body.get("path") or "").strip().lstrip("/")
    branch = (body.get("branch") or "main").strip() or "main"
    content = body.get("content")
    mode = (body.get("mode") or "write").strip().lower()
    if not token:
        return JSONResponse({"ok": False, "error": "未提供 GitHub 令牌"})
    if not repo or "/" not in repo or not path:
        return JSONResponse({"ok": False, "error": "需要 repo(owner/name) 和 path"})
    if content is None:
        return JSONResponse({"ok": False, "error": "content 不能为空"})
    import base64 as _b64
    cur_sha = body.get("sha")
    appended = False
    if mode == "append":
        # 追加模式：先读回现有全文，拼上本次片段后整体提交
        async with httpx.AsyncClient(timeout=25) as client:
            cur = await client.get(f"{GH_API}/repos/{repo}/contents/{path}",
                                   params={"ref": branch}, headers=_gh_headers(token))
        if cur.status_code == 200:
            data = cur.json() or {}
            raw = (data.get("content") or "").replace("\n", "")
            try:
                existing = _b64.b64decode(raw).decode("utf-8", errors="replace")
            except Exception:
                existing = ""
            content = existing + "\n" + str(content)
            cur_sha = data.get("sha")
            appended = True
        elif cur.status_code != 404:
            return JSONResponse({"ok": False, "error": f"追加写读取现有文件失败（HTTP {cur.status_code}）"})
        # 404 → 文件不存在，退化为创建
    b64 = _b64.b64encode(str(content).encode("utf-8")).decode()

    async def _put(sha=None):
        payload = {"message": body.get("message") or
                   (f"chore: 追加 {path}（AI 聊天平台）" if appended else f"chore: 更新 {path}（AI 聊天平台）"),
                   "content": b64, "branch": branch}
        if sha:
            payload["sha"] = sha
        return await client.put(f"{GH_API}/repos/{repo}/contents/{path}", json=payload,
                                headers=_gh_headers(token))

    try:
        async with httpx.AsyncClient(timeout=40) as client:
            r = await _put(cur_sha)
            # 409 = 文件已存在但没有带 sha：取当前 sha 后重试
            if r.status_code == 409 and not cur_sha:
                cur = await client.get(f"{GH_API}/repos/{repo}/contents/{path}",
                                       params={"ref": branch}, headers=_gh_headers(token))
                if cur.status_code == 200:
                    r = await _put((cur.json() or {}).get("sha"))
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"GitHub HTTP {r.status_code}: {r.text[:200]}"})
            data = r.json() or {}
            commit = (data.get("commit") or {})
            return JSONResponse({"ok": True, "repo": repo, "path": path,
                                 "bytes": len(str(content).encode("utf-8")),
                                 "appended": appended,
                                 "sha": data.get("content", {}).get("sha") if isinstance(data.get("content"), dict) else None,
                                 "commit": (commit.get("sha") or "")[:10],
                                 "commit_url": commit.get("html_url")})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/gh/delete")
async def gh_delete(request: Request):
    """删除仓库中的文件。"""
    body = await request.json()
    token = (body.get("token") or "").strip()
    repo = (body.get("repo") or "").strip()
    path = (body.get("path") or "").strip().lstrip("/")
    branch = (body.get("branch") or "main").strip() or "main"
    if not token or not repo or "/" not in repo or not path:
        return JSONResponse({"ok": False, "error": "需要 token、repo(owner/name) 和 path"})
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            cur = await client.get(f"{GH_API}/repos/{repo}/contents/{path}",
                                   params={"ref": branch}, headers=_gh_headers(token))
            if cur.status_code != 200:
                return JSONResponse({"ok": False, "error": f"文件不存在或读取失败（HTTP {cur.status_code}）"})
            sha = (cur.json() or {}).get("sha")
            r = await client.request("DELETE", f"{GH_API}/repos/{repo}/contents/{path}",
                                     json={"message": f"chore: 删除 {path}（AI 聊天平台）", "sha": sha, "branch": branch},
                                     headers=_gh_headers(token))
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"GitHub HTTP {r.status_code}: {r.text[:200]}"})
            return JSONResponse({"ok": True, "repo": repo, "path": path, "deleted": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/gh/delete-repo")
async def gh_delete_repo(request: Request):
    """删除整个 GitHub 仓库（不可恢复，调用前前端会先确认仓库存在）。"""
    body = await request.json()
    token = (body.get("token") or "").strip()
    repo = (body.get("repo") or "").strip()
    if not token or not repo or "/" not in repo:
        return JSONResponse({"ok": False, "error": "需要 token 和 repo(owner/name)"})
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.delete(f"{GH_API}/repos/{repo}", headers=_gh_headers(token))
            if r.status_code in (200, 202, 204):
                return JSONResponse({"ok": True, "repo": repo, "deleted": True})
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"GitHub HTTP {r.status_code}: {r.text[:200]}"})
            return JSONResponse({"ok": True, "repo": repo, "deleted": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/gh/search")
async def gh_search(request: Request):
    """用 GitHub 令牌搜索仓库：默认搜 GitHub 全站；mine=true 只搜登录用户自己的仓库。"""
    body = await request.json()
    token = (body.get("token") or "").strip()
    q = (body.get("query") or "").strip()
    mine = bool(body.get("mine"))
    if not token:
        return JSONResponse({"ok": False, "error": "未提供 GitHub 令牌"})
    if not q:
        return JSONResponse({"ok": False, "error": "搜索关键词不能为空"})
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            if mine:
                # 搜用户自己的仓库：列出后本地按关键词过滤（更准、无 rate limit 压力）
                r = await client.get(f"{GH_API}/user/repos",
                                     params={"per_page": 100, "sort": "updated", "affiliation": "owner"},
                                     headers=_gh_headers(token))
                if r.status_code >= 300:
                    return JSONResponse({"ok": False, "error": f"GitHub HTTP {r.status_code}: {r.text[:200]}"})
                items = r.json() or []
                kw = q.lower()
                matched = [it for it in items
                           if kw in (it.get("full_name") or "").lower()
                           or kw in (it.get("description") or "").lower()]
                repos = [{
                    "name": it.get("full_name"), "private": bool(it.get("private")),
                    "desc": (it.get("description") or "")[:160],
                    "stars": it.get("stargazers_count", 0), "lang": it.get("language"),
                    "url": it.get("html_url"), "branch": it.get("default_branch") or "main",
                } for it in matched]
                return JSONResponse({"ok": True, "scope": "mine", "query": q, "count": len(repos), "repos": repos})
            else:
                # 搜 GitHub 全站公开仓库（带令牌认证，避免匿名限流）
                r = await client.get(f"{GH_API}/search/repositories",
                                     params={"q": q, "per_page": 20, "sort": "stars", "order": "desc"},
                                     headers=_gh_headers(token))
                if r.status_code >= 300:
                    return JSONResponse({"ok": False, "error": f"GitHub HTTP {r.status_code}: {r.text[:200]}"})
                data = r.json() or {}
                items = data.get("items") or []
                repos = [{
                    "name": it.get("full_name"), "private": bool(it.get("private")),
                    "desc": (it.get("description") or "")[:160],
                    "stars": it.get("stargazers_count", 0), "lang": it.get("language"),
                    "url": it.get("html_url"), "branch": it.get("default_branch") or "main",
                } for it in items]
                return JSONResponse({"ok": True, "scope": "global", "query": q,
                                     "total": data.get("total_count"), "count": len(repos), "repos": repos})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


# ---------------- Render 发布（用户自带密钥） ----------------
@app.post("/api/render/owners")
async def render_owners(request: Request):
    """列出 Render workspace（ownerId 建站必填）。"""
    body = await request.json()
    key = (body.get("key") or "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "未提供 Render API Key（请在设置里填写）"})
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(RENDER_API + "/owners", headers=_rh_headers(key))
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"Render HTTP {r.status_code}: {r.text[:200]}"})
            items = r.json() or []
            owners = [{"id": (it.get("owner") or it).get("id"),
                       "name": (it.get("owner") or it).get("name"),
                       "email": (it.get("owner") or it).get("email"),
                       "type": (it.get("owner") or it).get("type")} for it in items]
            return JSONResponse({"ok": True, "owners": owners})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/render/services")
async def render_services(request: Request):
    """列出 Render 上的服务（含线上地址）。"""
    body = await request.json()
    key = (body.get("key") or "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "未提供 Render API Key"})
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(RENDER_API + "/services", params={"limit": 50},
                                 headers=_rh_headers(key))
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"Render HTTP {r.status_code}: {r.text[:200]}"})
            items = r.json() or []
            services = []
            for it in items:
                # Render /v1/services 返回 [{service:{...}, cursor:...}]，需解包
                sv = (it.get("service") if isinstance(it, dict) else None) or it
                services.append({
                    "id": sv.get("id"),
                    "name": sv.get("name"),
                    "type": sv.get("type"),
                    "repo": sv.get("repo"),
                    "branch": sv.get("branch"),
                    "suspended": sv.get("suspended"),
                    "url": (sv.get("serviceDetails") or {}).get("url"),
                })
            return JSONResponse({"ok": True, "count": len(services), "services": services})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/render/create-service")
async def render_create_service(request: Request):
    """把 GitHub 仓库发布为 Render 网站（静态站 / Web Service，默认免费计划）。

    type=static → 静态网站（publishPath 默认 "."）
    type=web    → Web Service（runtime=python/node/...，需 build/start 命令）
    """
    body = await request.json()
    key = (body.get("key") or "").strip()
    repo = (body.get("repo") or "").strip()
    name = (body.get("name") or "").strip()
    stype = (body.get("type") or "static").strip().lower()
    branch = (body.get("branch") or "main").strip() or "main"
    if not key:
        return JSONResponse({"ok": False, "error": "未提供 Render API Key"})
    if not repo or "/" not in repo:
        return JSONResponse({"ok": False, "error": "repo 需为 owner/name 格式"})
    if not name:
        name = repo.split("/")[-1]

    repo_url = repo if repo.startswith("http") else f"https://github.com/{repo}"
    payload = {
        "type": "static_site" if stype == "static" else "web_service",
        "name": name,
        "repo": repo_url,
        "branch": branch,
        "autoDeploy": "no",
    }
    if stype == "static":
        payload["serviceDetails"] = {
            "buildCommand": body.get("build") or "",
            "publishPath": body.get("publish") or ".",
        }
    else:
        runtime = (body.get("runtime") or "node").strip()
        payload["serviceDetails"] = {
            "runtime": runtime,
            "plan": "free",
            "envSpecificDetails": {
                "buildCommand": body.get("build") or "",
                "startCommand": body.get("start") or "",
            },
            "healthCheckPath": body.get("health") or "/",
        }
    env_vars = body.get("envVars")
    if isinstance(env_vars, list) and env_vars:
        payload["envVars"] = [{"key": str(e.get("key")), "value": str(e.get("value"))}
                              for e in env_vars if isinstance(e, dict) and e.get("key")]

    try:
        async with httpx.AsyncClient(timeout=40) as client:
            # ownerId 必填：取第一个 workspace
            ro = await client.get(RENDER_API + "/owners", headers=_rh_headers(key))
            if ro.status_code == 200:
                owners = ro.json() or []
                if owners:
                    payload["ownerId"] = (owners[0].get("owner") or owners[0]).get("id")
            if not payload.get("ownerId"):
                return JSONResponse({"ok": False, "error": "无法获取 Render workspace（ownerId），请检查 API Key"})
            r = await client.post(RENDER_API + "/services", json=payload, headers=_rh_headers(key))
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"Render HTTP {r.status_code}: {r.text[:300]}"})
            raw = r.json() or {}
            # POST /v1/services 返回 {service:{...}, deployId}，需解包
            svc = raw.get("service") if isinstance(raw, dict) else None
            if not svc:
                svc = raw
            sd = svc.get("serviceDetails") or {}
            return JSONResponse({"ok": True, "id": svc.get("id"), "name": svc.get("name"),
                                 "type": svc.get("type"), "repo": svc.get("repo"),
                                 "branch": svc.get("branch"),
                                 "deploy_id": raw.get("deployId") if isinstance(raw, dict) else None,
                                 "url": sd.get("url"),
                                 "dashboard": f"https://dashboard.render.com/web/{svc.get('id')}"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/render/deploys")
async def render_deploys(request: Request):
    """查询某服务的部署历史与状态。"""
    body = await request.json()
    key = (body.get("key") or "").strip()
    sid = (body.get("service") or "").strip()
    if not key or not sid:
        return JSONResponse({"ok": False, "error": "需要 Render Key 和 service ID"})
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{RENDER_API}/services/{sid}/deploys",
                                 params={"limit": 5}, headers=_rh_headers(key))
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"Render HTTP {r.status_code}: {r.text[:200]}"})
            items = r.json() or []
            deploys = []
            for d in items:
                dp = (d.get("deploy") if isinstance(d, dict) else None) or d
                deploys.append({"id": dp.get("id"), "status": dp.get("status"),
                                "created": dp.get("createdAt"), "finished": dp.get("finishedAt")})
            latest = deploys[0]["status"] if deploys else None
            return JSONResponse({"ok": True, "service": sid, "latest_status": latest, "deploys": deploys[:5]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/render/trigger")
async def render_trigger(request: Request):
    """手动触发一次部署（清缓存）。"""
    body = await request.json()
    key = (body.get("key") or "").strip()
    sid = (body.get("service") or "").strip()
    if not key or not sid:
        return JSONResponse({"ok": False, "error": "需要 Render Key 和 service ID"})
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{RENDER_API}/services/{sid}/deploys",
                                  json={"clearCache": "clear"}, headers=_rh_headers(key))
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"Render HTTP {r.status_code}: {r.text[:200]}"})
            d = r.json() or {}
            dp = (d.get("deploy") if isinstance(d, dict) else None) or d
            return JSONResponse({"ok": True, "deploy_id": dp.get("id"), "status": dp.get("status")})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/render/warm")
async def render_warm(request: Request):
    """预热/验证网站：服务端真实请求一次 URL，返回 HTTP 状态。

    Render 免费服务 15 分钟无活动会休眠，首次访问要几十秒冷启动；
    部署 live 后主动预热一次，既验证网站真的能打开，也让用户首访不等。
    """
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        return JSONResponse({"ok": False, "error": "需要合法的网站 URL（http/https）"})
    try:
        import time as _time
        t0 = _time.monotonic()
        async with httpx.AsyncClient(timeout=50, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"})
        ms = int((_time.monotonic() - t0) * 1000)
        ct = (r.headers.get("content-type") or "")[:60]
        return JSONResponse({"ok": r.status_code < 400, "status": r.status_code,
                             "ms": ms, "content_type": ct,
                             "note": "网站可正常访问" if r.status_code < 400 else "网站返回错误状态"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


@app.post("/api/render/delete-service")
async def render_delete_service(request: Request):
    """删除 Render 服务（站点），会把线上网站一起删掉，不可恢复。"""
    body = await request.json()
    key = (body.get("key") or "").strip()
    sid = (body.get("service") or "").strip()
    if not key or not sid:
        return JSONResponse({"ok": False, "error": "需要 Render Key 和 service ID"})
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.delete(f"{RENDER_API}/services/{sid}", headers=_rh_headers(key))
            if r.status_code in (200, 202, 204):
                return JSONResponse({"ok": True, "service": sid, "deleted": True})
            if r.status_code >= 300:
                return JSONResponse({"ok": False, "error": f"Render HTTP {r.status_code}: {r.text[:200]}"})
            return JSONResponse({"ok": True, "service": sid, "deleted": True})
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
    results = await web_search(q_used, engine=engine, proxy=proxy, filter_adult=filter_adult, max_results=15)
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


# ---------------- 研究所模式 / AI harness：靶场主动探测 ----------------
def _host_in_scope(host: str, scope) -> bool:
    """判断 host 是否落在授权靶标范围内（支持父域覆盖子域）。"""
    if not host:
        return False
    host = host.lower()
    if not isinstance(scope, list):
        scope = [scope]
    from urllib.parse import urlparse
    for s in scope:
        s = (s or "").strip().lower()
        if not s:
            continue
        if s.startswith(("http://", "https://")):
            s = urlparse(s).hostname or s
        if s == host or host.endswith("." + s):
            return True
    return False


@app.post("/api/harness/scope-check")
async def harness_scope_check(request: Request):
    """校验某 URL 是否在授权靶标范围内（供前端提前拦截）。"""
    body = await request.json()
    url = (body.get("url") or "").strip()
    scope = body.get("scope") or []
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    return JSONResponse({"ok": True, "host": host, "in_scope": _host_in_scope(host, scope)})


@app.post("/api/harness/probe")
async def harness_probe(request: Request):
    """靶场主动探测：在用户声明的授权靶标范围内，向目标发构造 HTTP 请求并观察响应。

    安全闸门：target 的 host 必须落在 scope（前端带来的授权靶标列表）内，否则拒绝——
    防止 AI 越界探测未授权第三方站点（SSRF / 越权测试）。
    """
    body = await request.json()
    target = (body.get("target") or "").strip()
    method = (body.get("method") or "GET").upper()
    path = body.get("path") or "/"
    headers_in = body.get("headers") or {}
    payload = body.get("body") or ""
    scope = body.get("scope") or []
    try:
        timeout = min(float(body.get("timeout") or 8), 20)
    except Exception:
        timeout = 8
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    from urllib.parse import urlparse
    host = urlparse(target).hostname or ""
    if not host:
        return JSONResponse({"ok": False, "error": "无法解析目标主机"})
    if not _host_in_scope(host, scope):
        return JSONResponse({"ok": False,
                             "error": "目标不在授权靶标范围内，请先在「靶场空间」声明该靶标并确认授权后再探测。"})
    allowed = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
    if method not in allowed:
        return JSONResponse({"ok": False, "error": f"不支持的方法 {method}"})
    try:
        import time as _t
        h = {"User-Agent": "AI-Harness-Research/1.0"}
        if isinstance(headers_in, dict):
            for k, v in headers_in.items():
                h[str(k)] = str(v)
        body_bytes = payload.encode() if isinstance(payload, str) else b""
        has_body = method in ("POST", "PUT", "PATCH", "DELETE")
        try:
            repeat = max(1, min(int(body.get("repeat") or 1), 20))
        except Exception:
            repeat = 1
        timings = []
        last = None
        full = target.rstrip("/") + (path if path.startswith("/") else "/" + path)
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, max_redirects=3) as client:
            for _ in range(repeat):
                t0 = _t.time()
                r = await client.request(method, full, headers=h,
                                        content=body_bytes if has_body else None)
                timings.append(int((_t.time() - t0) * 1000))
            last = r
        elapsed = timings[-1]
        resp_headers = {k: v for k, v in last.headers.items()}
        preview = last.text[:20000]
        out = {
            "ok": True,
            "status": last.status_code,
            "method": method,
            "url": str(last.url),
            "headers": resp_headers,
            "body_preview": preview,
            "length": len(last.content),
            "time_ms": elapsed,
        }
        # 侧通道/计时分析：多次请求汇总最小/平均/最大耗时，便于发现时序差异
        if repeat > 1:
            out["timing"] = {"count": repeat, "min": min(timings),
                             "avg": sum(timings) // repeat, "max": max(timings)}
        return JSONResponse(out)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:400]})


# ---------------- 红队工具种子插件（研究所模式一键加载） ----------------
@app.get("/api/plugins/seed")
async def plugins_seed():
    """返回内置的红队工具参考插件（markdown 文件），供前端一键载入 harness。"""
    import os as _os
    base = _os.path.join(_os.path.dirname(__file__), "static", "plugins", "redteam")
    items = []
    try:
        for fn in sorted(_os.listdir(base)):
            if fn.endswith(".md"):
                with open(_os.path.join(base, fn), "r", encoding="utf-8") as f:
                    items.append({"name": fn[:-3], "content": f.read()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200], "items": []})
    return JSONResponse({"ok": True, "items": items})


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
# 推理模式开关（Agno 官方现版本用法：挂 reasoning_model 即开启独立推理阶段；
# 旧版 reasoning=True 已废弃。env: AGENT_REASONING=1 + AGENT_REASONING_MODEL=gpt-5.2）
AGENT_REASONING = os.environ.get("AGENT_REASONING", "").strip().lower() in ("1", "true", "yes", "on")
AGENT_REASONING_MODEL = (os.environ.get("AGENT_REASONING_MODEL") or "").strip()
AGENT_REASONING_EFFORT = (os.environ.get("AGENT_REASONING_EFFORT") or "medium").strip()

AGENTOS_AVAILABLE = False
try:
    from agno.agent import Agent
    from agno.models.openai import OpenAIChat
    from agno.os import AgentOS
    from agno.tools.duckduckgo import DuckDuckGoTools

    agent_kwargs = {}
    reasoning_model = None
    if AGENT_REASONING and AGENT_REASONING_MODEL:
        try:
            from agno.models.openai import OpenAIResponses
            reasoning_model = OpenAIResponses(
                id=AGENT_REASONING_MODEL,
                reasoning_effort=AGENT_REASONING_EFFORT,
                reasoning_summary="auto",
            )
            agent_kwargs["reasoning_model"] = reasoning_model
        except Exception as _re:
            import logging
            logging.getLogger(__name__).warning("推理模型配置失败: %s", _re)

    agno_agent = Agent(
        name="平台助手",
        model=OpenAIChat(id=os.environ.get("AGENT_MODEL", "gpt-4o-mini")),
        tools=[DuckDuckGoTools()],
        instructions=["回答使用 Markdown", "需要最新信息时使用搜索工具"],
        markdown=True,
        **agent_kwargs,
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
    return {
        "agentos": AGENTOS_AVAILABLE,
        "reasoning": bool(AGENT_REASONING and AGENT_REASONING_MODEL),
        "reasoning_model": AGENT_REASONING_MODEL or None,
        "reasoning_effort": AGENT_REASONING_EFFORT if (AGENT_REASONING and AGENT_REASONING_MODEL) else None,
    }


# ---------------- 静态前端 ----------------
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
