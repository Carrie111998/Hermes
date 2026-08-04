# DESIGN: Dashboard Chat Tab Switch Lag Fix

## Symptom

用户从其他 Chrome tab 切回 Hermes dashboard 时，对话终端有明显卡顿（数秒级）。修复增量重放后，新的体感问题出现：等待加载中切走再切回，界面一直黑屏（遮罩永不撤），分屏操作尤其严重。

## Root Cause

### 1. 重连后 RingBuffer 全量重放 → xterm.js 解析阻塞（原卡顿）

- `pty_session.py`: `attach()` 一次性 `send_bytes(snapshot)` 推送最多 1MB
- 但 xterm.js 终端缓冲在 tab 隐藏期间从未丢失（ChatPage 持久挂载 + CSS 隐藏）
- 用"重传一切"恢复一个"客户端从未丢失"的状态 → 全部是浪费

### 2. 增量重放引入的边沿触发黑屏（本 PR 核心）

前端 resume 加载遮罩的撤除是**边沿触发**——收到"第一帧 sanitized PTY payload"才撤：

```
增量重放前：resume 全量重放 ~1MB → 总能收到帧 → 遮罩必撤（慢但不黑屏）
增量重放后：?offset=N 且 N 已最新 → 服务端一帧不发 → "第一帧"永远不来 → 遮罩卡死
```

叠加 Chrome 后台节流：切回时遮罩武装 → 切走（JS 冻结，边沿事件丢失）→ 切回 → 无人重新触发撤除 → 永久黑屏。分屏反复 focus/blur 每次都重新武装，最严重。

## Constraints

- 不能破坏 prompt caching（agent 核心不变）
- 不能引入花屏风险（否决"只重放最后一帧"启发式）
- 增量 offset 必须基于原始字节（非解码字符，防 UTF-8 错位）
- offset 滚出 RingBuffer 窗口时回退全量重放
- 本 PR 不含 loopback ws-ping 改动——上游 #53773 的 event-loop stall 论证（kshitijk4poor, 2026-07-03）与"Chrome 后台 tab 客户端假活"是两个不同场景，需单独论证，转 issue 讨论

## Decisions

### P0-a: RingBuffer 增量重放

- RingBuffer 增加 `total_appended: int` 全局字节偏移
- `attach(ws, client_offset=None)`: 只推送 `total_appended - client_offset` 范围内的增量
- `client_offset` 超出窗口 → 回退全量
- pty_ws 从 query param `?offset=N` 读取 client offset（本 PR 服务端半）

### P0-b: 前端记录 offset

- ChatPage 在 `ws.onmessage` 中累计收到的原始字节数 → `ptyByteOffsetRef`
- `reconnectPty()` 时将 offset 作为 `?offset=` query param 附在 WS URL 上
- 首次连接不带 offset（全量）

### P1: attach 分帧推送 + 哨兵帧

- snapshot 超过 16KB 时分帧推送，每帧间 `asyncio.sleep(0)` 让出事件循环
- **零 delta 增量重放**（offset 已最新）→ 仍发一帧 `\x1b[0m`（SGR reset，xterm 无可见副作用）
- **空缓冲首次 attach** → 同样发哨兵帧
- 保证客户端"第一帧"永不饿死——hydration 门不会卡

### P2: 前端 hydration watchdog + 增量不武装

- `PTY_RESUME_HYDRATION_WATCHDOG_MS = 2000`：onopen 后 2s 无帧强制撤遮罩（旧 server 无哨兵帧时兜底）
- `offset > 0`（客户端已有终端内容）时跳过 hydration 武装——分屏切换不再弹 loading 遮罩（根治分屏痛点）

### P3: visible 重绘

- `visibilitychange → visible` 时 `term.refresh(0, rows-1)`
- Chrome 后台 tab 丢弃 canvas 后强制重绘，防 xterm 黑帧

## Rejected Approaches

| 方案 | 否决原因 |
|------|----------|
| 客户端应用层心跳 | JS 定时器被 Chrome 后台节流，发不出去 |
| 只重放最后一帧（ANSI 启发式匹配） | 花屏风险，违反 "fixes that destroy the feature" |
| 缩短 sanitize 窗口 | 治标不治本，重放字节数不减 |
| 只做前端 watchdog 不做哨兵帧 | 旧 server 兼容需要双保险；哨兵帧是服务端最小根治 |
| loopback 启用 30s/60s ping | 翻转上游 #53773 决策（226s stall 实测），无证据支撑，转 issue |

## Files Touched

1. `hermes_cli/web_server.py` — pty_ws 传 ?offset= 给 attach
2. `hermes_cli/pty_session.py` — RingBuffer offset + attach 增量/哨兵帧/分帧
3. `web/src/pages/ChatPage.tsx` — 记录 offset + watchdog + 增量不武装 + visible 重绘
4. `web/src/lib/pty-resume-loading.ts` — watchdog 常量
5. `tests/test_pty_session.py` — snapshot_from / 哨兵帧 / 增量重放行为契约测试

## Validation

单元/集成（已执行，全部通过）：
- `tests/test_pty_session.py` 11 passed（含 4 个新测试）
- `web/src/lib/pty-resume-loading.test.ts` 9 passed（vitest）

黑盒（真实浏览器 + 真实 dashboard）：
- 首次 attach：遮罩数秒内消失，终端内容完整渲染
- 模拟分屏（visibilitychange+focus）：遮罩不出现、内容保留（P2 生效）
- `gui.log` 无新增 code=1005（后台 tab 断连指纹）

黑盒（需用户实测）：真实 Chrome 分屏切换 5 分钟，对比 ws closed 频率和体感。

注：delegate 子代理不劫持 session resume 是独立修复，见 PR #78368。
