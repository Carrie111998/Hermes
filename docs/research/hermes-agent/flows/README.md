# 端到端运行链路

本目录按真实用户场景追踪跨模块行为，而不是重复模块职责。每条链路必须标出：

- 参与进程；
- 调用入口；
- 状态 owner；
- 持久化点；
- Prompt/cache 边界；
- 权限边界；
- 失败和恢复路径；
- 对应行为测试。

首批链路：CLI Tool Turn、Gateway Message、Context Compression、Background Learning、Delegated Task 和 Cron Delivery。

## 已建立链路

- [Classic CLI 最小回合](./canonical-cli-turn.md)：从 prompt-toolkit 输入，经同步 Agent loop，到 canonical persistence 与最终展示投影。
