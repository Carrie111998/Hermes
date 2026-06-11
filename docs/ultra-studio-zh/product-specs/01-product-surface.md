文档路径：docs/ultra-studio-product-specs/01-product-surface.md

# Ultra Studio 产品界面

状态：产品与 UX 规范  
日期：2026-06-10

## 产品意图

Ultra Studio 是一个创意代理 UI，用于通过真实的 Hermes 工具和 Atlas 模型制作图像、视频、动态图形、广告、可复用资产和角色。

该产品应该更像一台创意任务计算机，而非聊天包装器。聊天只是其中一个界面。用户还需要文件、记忆、市场、任务、资产和实时检查器。

## 主要用户

1. 独立创作者制作短视频、产品片段、UGC 广告和 Reels。
2. 品牌/运营者复用产品、人物、参考图、Logo 和风格系统。
3. 高级用户迭代提示词、对比模型、检查故障并收集可复用资产。

## 主要任务

| 任务 | 用户表述 | 产品职责 |
|---|---|---|
| Generate media | "Make a cat video", "Generate a product photo" | 路由、询问缺失字段、运行 Atlas 任务、展示媒体卡片。 |
| Edit with references | "Use this image as style", "Animate this product" | 上传、分类资产角色、编译提供商负载。 |
| Reuse assets | "Use the same character", "Use this logo" | 选择器、资产引用、元素/角色关联。 |
| Inspect output | "Why did this fail?", "Download this" | 检查器显示任务、状态、错误、QA、下载。 |
| Build workflow | "Make UGC", "Make infographicMD" | 市场/技能展示可用工作流。 |
| Continue work | "Open the previous cat task" | 任务和历史恢复会话及产物。 |

## 信息架构

```text
Ultra Studio
├── New task
├── Search
├── My office
├── Marketplace
├── Files
├── Memory
├── Tasks
│   └── recent sessions / projects / jobs
└── Pricing / account
```

### 左侧导航外壳

左侧导航不是装饰性边栏。它是用户访问无法容纳在单个聊天记录中的产品状态的方式。

必需条目：

- `New task`：开始新的创意会话。
- `Search`：搜索会话、文件、资产、记忆和市场项目。
- `My office`：工作区主页、最近工作、共享项目。
- `Marketplace`：技能、模板、工作流包、模型配方。
- `Files`：上传的媒体、任务文件、生成的产物。
- `Memory`：持久化的项目/用户记忆、品牌事实、偏好设置。
- `Tasks`：会话和运行中/已完成的创意任务。

### 中心：创意会话

中心区域是对话和生成工作区。

它必须支持：

- 流式助手文本。
- 用户文本输入。
- 文件上传。
- 模型选择器。
- 工具状态。
- 媒体卡片。
- 询问用户问题卡片。
- 带有可操作恢复的错误卡片。

会话不得在打开时自动生成媒体。用户意图驱动执行。

### 右侧：检查器/实时面板

检查器是用于当前选中的任务、资产或工具运行的上下文面板。

它应该显示：

- 当前任务状态和进度。
- 提供商/模型和输入约束。
- 选中资产预览。
- 提示词、种子、尺寸、时长和来源链路。
- QA 结果和观察到的证据。
- 下载/导出操作。
- 转换为 Element。
- 创建 Character。
- 生成失败时的重试/修复计划。

检查器不是第二个聊天窗口。它更接近 IDE 检查器或 Figma 属性面板。

## 必需状态

| 状态 | 中心行为 | 检查器行为 |
|---|---|---|
| Empty | 提示词输入和建议任务。 | 未选中任何内容。 |
| Thinking | 流式传输路由/规划文本。 | 显示当前推理阶段。 |
| Waiting for user | 渲染结构化问题。 | 显示缺失字段上下文。 |
| Creating | 显示带进度的任务卡片。 | 显示任务详情、模型、输入。 |
| Complete | 显示媒体卡片和摘要。 | 显示资产详情和操作。 |
| Failed | 显示类型化错误和重试选项。 | 显示提供商错误、日志、修复计划。 |

## 非目标

- 不要在主 UI 中暴露原始提供商仪表板。
- 默认情况下不要显示内部提示词模板。
- 不要使用与 Hermes 事件断开的虚假运行/状态面板。
- 不要将 Marketplace、Memory 和 Files 合并到一个通用的 "Assets" 页面中。
