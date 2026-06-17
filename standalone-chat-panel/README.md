# Ultra Studio Standalone Panel

这是当前 Ultra Studio 创作 Agent 的唯一产品入口。

## 当前入口

- 产品 UI: `standalone-chat-panel/index.html`
- 运行端口: `http://127.0.0.1:9131/`
- 前端入口: `standalone-chat-panel/src/apiPanel.ts`
- API BFF: `standalone-chat-panel/vite.config.mjs`
- 后端 API: `http://127.0.0.1:9120`
- Hermes dashboard 依赖: `http://127.0.0.1:9119`

## 不再作为产品入口的 UI

- `web/src/pages/ChatPage.tsx`
  - Hermes 自带 dashboard 的 PTY/TUI 调试入口。
  - 只作为 Hermes 原生管理面板保留，不作为 Ultra Studio 产品 UI。
- `standalone-chat-panel/src/main.ts`
  - 已删除。它是旧 WebSocket gateway 面板。
- `web/src/pages/UltraStudioChatPage.tsx`
  - 已删除。它没有被 `web/src/App.tsx` 路由引用。
- `docs/ultra-studio-zh/`
  - 文档网站，不是聊天产品 UI。

## 运行关系

浏览器只访问 `9131`。

`9131` 的 Vite BFF 负责：

- 校验面板登录 token。
- 注入 API server key。
- 注入 `tenant_id / workspace_id / project_id / user_id / roles`。
- 覆盖浏览器伪造的 `X-Hermes-*` header。
- 把请求转发到 `9120` API server。

`9119` dashboard 只保留给上传和旧 Hermes 能力依赖，不要求用户直接打开。

## 测试账号

- `alice / alice123`
- `bob / bob123`
