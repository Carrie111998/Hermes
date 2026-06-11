# Ultra Studio 交付计划

状态：执行计划  
日期：2026-06-10

## 原则

首先交付最小的真实创意代理循环：

```text
chat -> route -> upload/asset -> Atlas job -> status stream -> asset card -> inspector
```

不要构建虚假的演示路径。如果提供商、上传或任务失败，UI 应显示真实的阻塞原因。

## P0：真实循环

目标：从网页聊天实现一个真实的图像/视频生成流程。

构建内容：

1. Ultra 个人资料/允许列表引导。
2. 左侧导航栏外壳，包含 Tasks、Files、Memory、Marketplace 占位符。
3. 真实聊天上传到类型化的 `media_input`。
4. `workflow-router` 运行时连接。
5. `ultra_media_job_create/status/finalize`。
6. 通过真实工具集成 Atlas 图像/视频提供商。
7. 聊天中的流式任务状态。
8. 用于所选任务/资产的检查器（Inspector）。
9. 资产注册和下载。
10. 类型化错误（Typed errors）。

验收标准：

- 用户请求图像并接收真实生成的资产。
- 用户请求视频并获得真实任务或类型化的阻塞提示。
- 上传的图像可用作参考资产。
- 刷新页面不会伪造任务完成状态。
- 检查器显示任务/模型/输入/输出详情。

## P1：生产级创意工作流

目标：将真实循环转变为可复用的创意工作流。

构建内容：

1. 产品摄影技能（Product photoshoot skill）。
2. InfographicMD 工作流运行时。
3. ProductMD / UGC / 电影级工作流文档和首次实现。
4. 提示编译器和提供商约束注册表。
5. 媒体 QA 和提示修复流程。
6. 资产库详情视图。
7. 从选定资产创建 Element 和 Character。
8. Marketplace 本地目录。
9. 包含可见/可撤销条目的 Memory 页面。

验收标准：

- 模糊请求会询问一个有用的澄清问题。
- 明确的工作流请求创建结构化计划。
- 生成的资产可以成为 Element 或 Character。
- Marketplace 显示可用工作流和状态。
- Memory 可以影响后续请求且可被检查。

## P2：任务计算机（Task Computer）

目标：使产品表现得像真正的创意任务计算机。

构建内容：

1. Sandbox 生命周期管理器。
2. 任务文件浏览器（Task file browser）。
3. Artifact 包导出。
4. 浏览器上下文存储（Browser context store）。
5. 本地浏览器/桌面桥接。
6. 持久化工作流引擎（Durable workflow engine）。
7. 人工审批网关（Human approval gateway）。
8. 观察/溯源账本（Observation/provenance ledger）。
9. 协作/共享隐私边界。

验收标准：

- 运行中的任务在工作者/会话中断后仍然存活。
- 工作期间创建的文件可浏览。
- 浏览器/下载的 artifact 被捕获并附带溯源信息。
- 成本/隐私/发布操作需要审批。
- 共享会话不会泄露凭证或 sandbox 文件。

## P3：平台

目标：使系统可扩展且可运维。

构建内容：

1. 技能评估工具（Skill eval harness）。
2. Marketplace 发布流程。
3. 模型基准测试和模型配方质量报告。
4. 团队权限和项目策略。
5. 可观测性仪表板。
6. 计费/配额集成。
7. 定时周期性创意任务。

## 发布门槛（Launch Gates）

在以下情况满足前，不得公开发布演示：

- 没有虚假媒体 URL。
- 没有硬编码任务结果。
- 没有意外的 FAL/Comfy 回退。
- Atlas 凭证路径明确。
- 用户上传的是真实文件。
- 资产具有真实 ID 和下载路径。
- 失败的提供商调用保持可见。
- 可见的技能列表是聚焦的。

## 测试命令/检查

文档仅更改时的最低检查：

- 每文件行数低于 800
- markdown 链接扫描
- 同一表面没有重复文档

运行时更改的最低检查：

- Python 工具契约测试
- UI 的前端构建/类型检查
- 网关事件冒烟测试
- 一次真实上传冒烟测试
- 一次真实 Atlas 任务冒烟测试，或类型化的缺失凭证阻塞提示

## 开放问题

- Marketplace 在 MVP 阶段应仅为本地目录，还是由服务器目录支持？
- Memory 默认应按项目范围划分，还是按用户范围划分并带项目过滤器？
- Task Files 和 Asset Library 应共享存储键，还是使用单独的存储桶？
- 哪个 Atlas 视频模型作为 P0 默认？
- 首次发布前，哪些操作需要显式审批？
