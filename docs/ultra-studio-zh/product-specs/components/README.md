文档路径：docs/ultra-studio-zh/product-specs/components/README.md

# Ultra Studio 组件中文详细规格索引

状态：中文详细版索引  
日期：2026-06-11

## 组件地图

| 组件 | 定位 |
|---|---|
| [01 左侧导航外壳](01-left-nav-shell) | Ultra Studio 的固定左侧入口，承载 New task、Search、My office、Marketplace、Files、Memory、Tasks。它不是装饰栏，而是工作状态的主导航。 |
| [02 创作聊天界面](02-creative-chat-ui) | 用户描述创作目标、上传素材、回答澄清问题、审批动作、查看生成进度的主工作区。打开页面不能自动开始生成。 |
| [03 右侧检查器 / 实时面板](03-inspector-live-panel) | 右侧面板显示当前 job、选中资产、QA、下载、重试、转 Element、建角色。它不是第二个聊天窗口。 |
| [04 技能市场](04-marketplace) | 面向 Ultra Studio 的技能、模板、工作流和连接器目录。第一版可以是空态，但不能伪造市场数据。 |
| [05 记忆系统](05-memory) | 保存项目偏好、品牌设定、角色设定、用户选择和长期上下文。它要能被 agent 使用，也要能被用户操作记录。 |
| [06 文件 / 任务文件浏览器](06-files-task-file-browser) | 展示上传文件、任务工作目录、生成中间产物和可复用素材。它连接 chat upload、sandbox 文件和 asset library。 |
| [07 任务 / 会话历史](07-tasks-session-history) | 左侧任务列表和历史恢复能力，用户可以回到某次创作，看到 transcript、工具、文件、资产和结果。 |
| [08 资产库界面](08-asset-library-ui) | 统一浏览生成资产、上传素材、角色、合集、智能分组和散装资产。它是创作复用的核心页面，不只是图库。 |
| [09 资产服务](09-asset-service) | 资产库的后端实体和 API，负责 media_assets、来源链路、下载、索引、collections、characters。 |
| [10 媒体任务服务](10-media-job-service) | 把图片/视频生成从同步工具升级为可追踪 job：submit、poll、状态事件、产物入库、失败恢复。 |
| [11 技能注册表](11-skill-registry) | 管理 agent 可见技能、渐进加载、安装守卫、Ultra allowlist、评估和版本。它决定“做视频”时 agent 应该看到哪些技能。 |
| [12 工作流路由器](12-workflow-router) | 在真正生成前判断 intent、asset roles、缺失字段、workflow_skill 和下一步。它要先问清楚，而不是一进来就开跑。 |
| [13 提示词编译器](13-prompt-compiler) | 把用户自然语言、技能规则、模型约束、资产引用和安全边界编译成 provider payload。不是简单润色 prompt。 |
| [14 沙箱生命周期](14-sandbox-lifecycle) | 管理每个任务的执行环境：创建、挂载文件、运行工具、暂停、唤醒、销毁。Atlas-only P0 可以先弱化，但不能把它当已经完整。 |
| [15 人工审批网关](15-human-approval-gateway) | 对高风险动作、付费调用、删除、外发、重试消耗等操作做明确审批。要可恢复、可检查。 |
| [16 观察与溯源账本](16-observation-provenance-ledger) | 记录每次生成、工具调用、观察结果、资产来源、QA 证据和决策链。它用来防止 agent 空口声称完成。 |
| [17 TokenRouter 凭证与额度路由](17-tokenrouter) | 控制 provider 凭证、租户策略、额度、并发和计费。沙箱或浏览器不拿真实上游 key，只拿短期 Hermes token。 |
| [18 CometAPI 媒体网关](18-cometapi-media-gateway) | 处理长视频、外部社媒 URL、抽帧、转码、音频/字幕提取、多模态打包。它是媒体数据面，不是 TokenRouter。 |
| [19 模型目录与供应商约束](19-model-catalog-provider-constraints) | 记录 Atlas 图片/视频模型、输入能力、画幅、时长、参考图、价格/限制，供 router、compiler、UI 使用。 |

## 阅读顺序

1. 先读 `01-left-nav-shell`、`02-creative-chat-ui`、`03-inspector-live-panel`，理解产品表面。
2. 再读 `07-tasks-session-history`、`10-media-job-service`、`08/09-asset`，理解任务和资产闭环。
3. 接着读 `11-skill-registry`、`12-workflow-router`、`13-prompt-compiler`，理解 Agent 如何选择 Skill 和工具。
4. 最后读 `14-19`，理解 Sandbox、Approval、Ledger、TokenRouter、CometAPI、Model Catalog 这些基建边界。

## 状态说明

绿色/已实现表示有真实代码；黄色/部分实现表示有文档或相邻机制但未完整接入 runtime；红色/spec-only 表示需要新建实现。
