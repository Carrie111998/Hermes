# Ultra Studio Standalone Panel

这是当前 Ultra Studio 创作 Agent 的唯一产品入口说明。

## 当前入口

- 产品 UI: `standalone-chat-panel/index.html`
- 运行端口: `http://127.0.0.1:9131/`
- 前端入口: `standalone-chat-panel/src/apiPanel.ts`
- API BFF: `standalone-chat-panel/vite.config.mjs`
- 后端 API: `http://127.0.0.1:9120`
- Hermes dashboard 依赖: `http://127.0.0.1:9119`

## 不再作为产品入口，但保留为迁移参考的 UI

- `web/src/pages/ChatPage.tsx`
  - Hermes 自带 dashboard 的 PTY/TUI 调试入口。
  - 只作为 Hermes 原生管理面板保留，不作为 Ultra Studio 产品 UI。
- `standalone-chat-panel/src/main.ts`
  - 旧 WebSocket gateway 面板源码。
  - 当前不被 `standalone-chat-panel/index.html` 加载。
  - 作为迁移参考保留；当前产品入口已经迁走工具进度、server-side upload、动态模型、服务端 stop、approval 响应。
- `web/src/pages/UltraStudioChatPage.tsx`
  - 旧 React 实验页源码。
  - 当前没有被 `web/src/App.tsx` 路由引用。
  - 里面仍有未迁移结构：`ChatInspector`、pending prompt 操作区、plugin slots、React 组件化聊天布局。
- `docs/ultra-studio-zh/`
  - 文档网站，不是聊天产品 UI。

## 已迁移到当前产品入口的能力

- 登录 token。
- 多用户 session 列表、创建、打开。
- `/panel-api` BFF 注入服务端身份 header。
- `/api/sessions/:id/chat/stream` SSE 聊天。
- 图片附件预览、拖拽上传、`/hermes/upload` 服务端落盘、路径注入 prompt。
- 助手输出中的图片、视频 URL 预览。
- `/v1/models` 动态模型列表。
- `tool.started`、`tool.progress`、`tool.completed`、`tool.failed` 工具卡片。
- `/api/sessions/:id/chat/stop` 服务端停止当前 stream。
- `approval.request` pending panel 与 `/api/sessions/:id/chat/approval` 响应。
- `clarify.request` / `sudo.request` pending panel 与 `/api/sessions/:id/chat/prompt` 响应。

## 尚未迁移的旧面板能力

- 旧 gateway WebSocket 事件流和 JSON-RPC method 调用。
- `secret.request` 的多用户安全捕获回调；当前 UI 能显示但不会提交密钥。
- 旧 gateway 的 `input.detect_drop` / `image.attach` 精确附件注册；当前实现是 `/hermes/upload` 后把本地路径写入 prompt。
- `config.set`；当前实现是每次 session/chat 请求携带 model，不改全局配置。
- slash command。

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
