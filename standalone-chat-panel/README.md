# Ultra Studio Standalone Panel

这是当前 Ultra Studio 创作 Agent 的唯一产品入口说明。

## 当前入口

- 产品 UI: `standalone-chat-panel/index.html`
- 运行端口: `http://127.0.0.1:9131/`
- 前端入口: `standalone-chat-panel/src/main.ts`
- 传输协议: WebSocket JSON-RPC，`/hermes/ws`
- 后端: Hermes dashboard/TUI gateway，`http://127.0.0.1:9119`
- 上传代理: `/hermes/upload` → Hermes dashboard `/api/chat/uploads`

当前入口只做一件事：把用户输入、上传、模型切换、审批、澄清、密钥输入等事件转发给真实 Hermes gateway。前端不根据关键词直接生成图片/视频，也不调用 Atlas/FAL/其它媒体 API。

## 不再作为产品入口，但保留为迁移参考的 UI

- `web/src/pages/ChatPage.tsx`
  - Hermes 自带 dashboard 的 PTY/TUI 调试入口。
  - 只作为 Hermes 原生管理面板保留，不作为 Ultra Studio 产品 UI。
- `standalone-chat-panel/src/apiPanel.ts`
  - 旧 API/SSE 多用户面板源码。
  - 当前不被 `standalone-chat-panel/index.html` 加载。
  - 保留为后续多用户 BFF 迁移参考，不能作为当前产品入口。
- `web/src/pages/UltraStudioChatPage.tsx`
  - 旧 React 实验页源码。
  - 当前没有被 `web/src/App.tsx` 路由引用。
  - 里面仍有未迁移结构：`ChatInspector`、pending prompt 操作区、plugin slots、React 组件化聊天布局。
- `docs/ultra-studio-zh/`
  - 文档网站，不是聊天产品 UI。

## 当前 WebSocket 入口能力

- `session.create`、`session.resume`。
- `prompt.submit` 真实 agent 聊天。
- 图片附件预览、拖拽上传、`/hermes/upload` 服务端落盘、`image.attach` / `input.detect_drop`。
- 助手输出中的图片、视频 URL 预览。
- `model.options`、`config.set` 模型切换。
- `tool.start`、`tool.update`、`tool.complete` 工具卡片。
- `session.interrupt` 停止当前 turn。
- `clarify.respond`、`approval.respond`、`sudo.respond`、`secret.respond`。
- `slash.exec`、`command.dispatch`。

## 运行关系

浏览器只访问 `9131`。

`9131` 的 Vite 代理负责：

- `/hermes/ws` 代理到 `9119 /api/ws?token=...`。
- `/hermes/upload` 代理到 `9119 /api/chat/uploads`。
- `/hermes/status` 代理到 `9119 /api/status`。

`9120` API server 和 `/panel-api` 仍可用于后续多用户 BFF 验证，但不是当前默认聊天入口。

## 旧 API 面板测试账号

- `alice / alice123`
- `bob / bob123`
