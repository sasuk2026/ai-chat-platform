# AI Chat Platform - 托管式 AI 聊天平台

基于 Agno AgentOS + FastAPI 的托管 AI 聊天平台：任意 OpenAI 协议服务接入、
流式输出、网页搜索（DDG/Bing/Google + 代理）、多会话本地存储。

## 快速开始

### 环境要求
- Python 3.10+（或 Docker）

### 本地运行
```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
打开 http://localhost:8000 ，点左下角 ⚙️ 设置，填入你的 API 地址和 Key 即可聊天。

### 启用 Agno AgentOS（可选）
```bash
export OPENAI_API_KEY=sk-xxx        # AgentOS 内置智能体使用
export AGENT_MODEL=gpt-4o-mini      # 可选
```
重启后访问 `/agno` 使用 AgentOS REST API，`/docs` 查看全部接口。

### Docker 运行
```bash
docker build -t ai-chat-platform .
docker run -p 8000:8000 ai-chat-platform
```

## 托管部署（Railway 一键）

1. 把本目录推送到 GitHub 仓库
2. 打开 https://railway.app/new → Deploy from GitHub repo → 选择仓库
   （Railway 会读取 railway.json 用 Dockerfile 构建，自动分配域名）
3. 部署完成后 Settings → Networking → Generate Domain，得到 `https://xxx.up.railway.app`
4. 打开即用；API Key 保存在各用户自己的浏览器里，服务端不落盘

其他免费托管替代：Render（连接仓库自动部署）、Fly.io（`fly launch`）、
Vercel（仅前端静态部分）。

## 目录结构
```
app/
├── main.py           # FastAPI 入口：/api/chat(流式透传)、/api/search、AgentOS 挂载
├── search.py         # 网页搜索：DDG/Bing/Google + 代理模板
└── static/index.html # 前端聊天界面（无构建步骤）
Dockerfile            # 容器化
railway.json / Procfile / requirements.txt
```

## 技术栈
- Agno AgentOS、FastAPI、httpx、原生 JS 前端（SSE 流式渲染）
