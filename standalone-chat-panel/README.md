# Ultra Studio Standalone Panel

这是当前 Ultra Studio 创作 Agent 的唯一产品入口说明。

## 当前入口

- 产品 UI: `standalone-chat-panel/index.html`
- 运行端口: `http://127.0.0.1:9131/`
- 前端入口: `standalone-chat-panel/src/main.ts`
- 传输协议: WebSocket JSON-RPC，`/hermes/ws`
- 面板后端: Vite dev server 内置 BFF
- Hermes 后端: API Server，默认 `http://127.0.0.1:9120`
- 认证存储: SQLite，默认 `standalone-chat-panel/.ultra-studio-panel-auth.sqlite`
- 上传代理: `/hermes/upload` → BFF 转成 image data URL，随下一轮 prompt 作为多模态输入发给 API Server

当前入口只做一件事：登录后把用户输入、上传、模型切换、审批、澄清、密钥输入等事件转发给真实 Hermes API Server。前端不根据关键词直接生成图片/视频，也不调用 Atlas/FAL/其它媒体 API。

## 不再作为产品入口的 UI

- `web/src/pages/ChatPage.tsx`
  - Hermes 自带 dashboard 的 PTY/TUI 调试入口。
  - 只作为 Hermes 原生管理面板保留，不作为 Ultra Studio 产品 UI。
- `web/src/pages/UltraStudioChatPage.tsx`
  - 旧 React 实验页源码。
  - 当前没有被 `web/src/App.tsx` 路由引用。
  - 里面仍有未迁移结构：`ChatInspector`、pending prompt 操作区、plugin slots、React 组件化聊天布局。
- `docs/ultra-studio-zh/`
  - 文档网站，不是聊天产品 UI。

## 当前 WebSocket 入口能力

- `panel-auth/status`、`panel-auth/bootstrap`、`panel-auth/signup`、`panel-auth/login`、`panel-auth/me`。
- 登录后浏览器 WebSocket 连接 `/hermes/ws?token=...`，token 是 BFF 颁发的不透明 session token。
- BFF 从 SQLite 用户表解析 `tenant_id/workspace_id/project_id/user_id/roles`，再向 Hermes API Server 注入 `X-Hermes-*` principal headers。
- `session.create`、`session.list`、`session.resume`。
- `prompt.submit` 真实 agent 聊天。
- 图片附件预览、拖拽上传、`/hermes/upload`。
- 助手输出中的图片、视频 URL 预览。
- `model.options`、`config.set` 模型切换。
- `tool.start`、`tool.update`、`tool.complete` 工具卡片。
- `session.interrupt` 停止当前 turn。
- `clarify.respond`、`approval.respond`、`sudo.respond`、`secret.respond`。
- `slash.exec`、`command.dispatch`。

## 运行关系

浏览器只访问 `9131`。

`9131` 的 Vite BFF 负责：

- `/panel-auth/*`：真实登录、本地 SQLite 用户库、bootstrap 第一个账号。
- `/hermes/ws`：浏览器 WebSocket，内部翻译到 Hermes API Server `/api/sessions` 与 `/api/sessions/{id}/chat/stream`。
- `/hermes/upload`：图片上传转 data URL，作为下一轮多模态输入。
- `/panel-api/*`：带 principal headers 的 API Server 代理，供后续 UI 直接调用。

默认面板不提供本地测试账号，也不允许浏览器自带 principal header。多用户身份由 SQLite auth store 进入 BFF，再由 BFF 注入 Hermes API Server。

## 本地启动

先启动 Hermes API Server，并配置 API key，例如：

```bash
API_SERVER_ENABLED=true API_SERVER_KEY=sk-local API_SERVER_PORT=9120 hermes gateway run
```

再启动面板：

```bash
cd standalone-chat-panel
API_SERVER_KEY=sk-local npm run dev
```

第一次打开 `http://127.0.0.1:9131/` 时会出现 bootstrap 表单，用来创建第一个账号。要在本地继续创建多个测试用户，启动面板时加：

```bash
HERMES_PANEL_ALLOW_SIGNUP=1 API_SERVER_KEY=sk-local npm run dev
```

SQLite 是 P0 存储实现；后续迁移 MySQL 时应替换 `panelAuth.mjs` 的 store adapter，而不是改变浏览器协议或让浏览器传 principal。
