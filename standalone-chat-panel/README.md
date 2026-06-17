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
  - 里面仍有未迁移功能：gateway JSON-RPC、工具进度、审批/澄清 prompt、server-side upload、动态模型列表、真实 interrupt。
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
- 基础图片附件预览和请求携带。
- 助手输出中的图片、视频 URL 预览。
- 基础模型选择。
- 客户端停止当前 stream。

## 尚未迁移的旧面板能力

- 旧 gateway WebSocket 事件流和 JSON-RPC method 调用。
- `clarify.request`、`approval.request`、`sudo.request`、`secret.request` 这类交互式 pending prompt。
- `tool.started`、`tool.completed`、`tool.failed` 的完整工具卡片。
- `/hermes/upload` 的服务端上传路径，以及 `input.detect_drop` / `image.attach` 附件注册。
- 动态模型列表 `model.options` 和 `config.set`。
- `session.interrupt` 服务端中断。
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
