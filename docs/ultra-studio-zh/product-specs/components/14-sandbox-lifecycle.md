# 沙盒生命周期

状态：partial（部分实现）—— 多后端执行环境抽象（本地、Docker、Modal、Daytona、Singularity、SSH）以及文件同步和会话初始化已存在；产品级沙盒生命周期动词（`sandbox.create/attach/sleep/wake/recycle/restore_artifacts`）以及无静态密钥的凭证合约仅为规格定义。
日期：2026-06-11

来源：

- 文档：`docs/ultra-studio-product-specs/02-agent-runtime-contract.md`（§沙盒生命周期、§会话生命周期、§错误合约），`06-delivery-plan.md`（P2 事项 1-2、P2 门槛），`docs/hermes-tokenrouter-credential-flow.md`（§核心合约: sandbox token rules），本地配置文档 `/Users/lifcc/Desktop/code/work/infra/her/hermes-local-docker-sandbox.md`（仓库外）
- 代码（本次会话已验证）：`tools/environments/base.py`（`BaseEnvironment` 抽象基类、`ProcessHandle`、`get_sandbox_dir`、`init_session`、`cleanup`、`_run_bash`），`tools/environments/local.py`，`tools/environments/docker.py`，`tools/environments/daytona.py`，`tools/environments/modal.py`，`tools/environments/managed_modal.py`，`tools/environments/modal_utils.py`，`tools/environments/singularity.py`，`tools/environments/ssh.py`，`tools/environments/file_sync.py`，`tools/terminal_tool.py`，`tools/process_registry.py`，`docker/`（`entrypoint.sh`、`main-wrapper.sh`、`s6-rc.d/`、`cont-init.d/`、`stage2-hook.sh`、`SOUL.md`），`docker-compose.yml`

## 目的与范围

"沙盒是一台任务计算机，而非实现细节"（`02-agent-runtime-contract.md` §沙盒生命周期）。它是智能体运行命令、构建产物并累积任务文件的地方，其生命周期超越单个 WebSocket 连接。

凭证规则："沙盒不得持有静态提供商密钥。它接收一个短期、受限的令牌（如 `HF_JWT_TOKEN`），然后由 TokenRouter 处理凭证交换和提供商策略"（§沙盒生命周期；详情见 `17-tokenrouter.md`）。

范围：环境后端、会话级生命周期动词、产物恢复、资源限制以及凭证边界。任务文件语义见 `06-files-task-file-browser.md`；内部运行内容（工具/技能）不在本文范围内。

## 实现状态

| 状态 | 项目 | 引用 |
|---|---|---|
| 已实现（Implemented） | 环境抽象：带有进程句柄、bash 执行、临时目录、会话初始化和清理的抽象基类 | `tools/environments/base.py`（`BaseEnvironment`、`ProcessHandle`、`init_session`、`cleanup`） |
| 已实现（Implemented） | 本地后端（带沙盒目录约定的主机执行） | `tools/environments/local.py`、`base.py`（`get_sandbox_dir`） |
| 已实现（Implemented） | Docker 后端（容器隔离执行） | `tools/environments/docker.py` |
| 已实现（Implemented） | 远程/托管后端：Modal、托管 Modal、Daytona、Singularity、SSH | `tools/environments/modal.py`、`managed_modal.py`、`daytona.py`、`singularity.py`、`ssh.py` |
| 已实现（Implemented） | 主机<->环境文件同步 | `tools/environments/file_sync.py` |
| 已实现（Implemented） | 使用 s6 监管、初始化钩子、入口点的容器打包 | `docker/`（`entrypoint.sh`、`s6-rc.d/`、`cont-init.d/`、`stage2-hook.sh`）、`docker-compose.yml` |
| 已实现（Implemented） | 跨工具调用的进程跟踪 | `tools/process_registry.py`、`tools/terminal_tool.py` |
| 已记录配置，不在仓库中（Documented config） | 本地 Docker 沙盒加固方案（内存/网络限制、密钥卫生） | 工作区根目录的 `hermes-local-docker-sandbox.md` |
| 已规定，未构建（Specified, not built） | `sandbox.create / attach / sleep / wake / recycle / restore_artifacts` 操作 | `02-agent-runtime-contract.md` §沙盒生命周期；环境层之上不存在此类动词（rg 搜索 `sandbox.create|sandbox_attach` 等 —— 无结果） |
| 已规定，未构建（Specified, not built） | 会话状态中的沙盒 ID（`active sandbox id, if attached`） | `02-agent-runtime-contract.md` §会话生命周期 |
| 已规定，未构建（Specified, not built） | 短期令牌注入（`HF_JWT_TOKEN`）替代静态环境密钥 | `hermes-tokenrouter-credential-flow.md` §核心合约；当前提供商密钥位于服务器端环境/配置（例如通过 `plugins/video_gen/atlas/client.py` `resolve_credentials` 的 `ATLAS_API_KEY`）—— 服务器端，但是静态的 |
| 已规定，未构建（Specified, not built） | 带产物保留的睡眠/唤醒（跨回收） | `02-agent-runtime-contract.md`；`06-delivery-plan.md` P2 门槛"运行中的作业在工作者/会话中断后存活" |

## 用户入口点

无直接用户界面。通过以下方式访问：

- 智能体工具执行：终端/文件工具在活跃环境内运行（已实现；后端由配置选择）。
- 会话附加：恢复任务时应重新附加其沙盒或恢复产物（计划中；`07-tasks-session-history.md` 开放问题 4）。
- 检查器/实时面板显示当前运行的沙盒状态（计划中；`01-product-surface.md` 在产品形态中列出沙盒/任务文件系统）。
- 管理员/配置：部署配置中的后端选择和限制（`docker-compose.yml`、cli 配置）。

## 功能列表

| 功能 | 状态 |
|---|---|
| 单一抽象基类背后的可插拔执行后端 | 已实现（Implemented） |
| 每会话工作目录 / 沙盒目录约定 | 已实现（Implemented）（`get_sandbox_dir`、`base.py` 中的 cwd 标记） |
| 带轮询/终止/等待句柄的流式进程输出 | 已实现（Implemented）（`ProcessHandle`、`_ThreadedProcessHandle`） |
| 环境内外的文件同步 | 已实现（Implemented）（`file_sync.py`） |
| 带资源限制的容器隔离 | 已实现能力（Implemented）（Docker 后端 + compose）；限制策略是部署配置，非产品强制 |
| 命名生命周期：create/attach 作为显式网关操作 | 已规划（Planned） |
| Sleep/wake（暂停计费/资源，保留状态） | 已规划（Planned） |
| 带 `restore_artifacts` 的 Recycle（重建环境，恢复任务文件） | 已规划（Planned） |
| 会话状态中跟踪的沙盒 ID 并显示在检查器中 | 已规划（Planned） |
| 沙盒内部使用短期作用域令牌替代静态密钥 | 已规划（Planned）（依赖 TokenRouter） |
| 沙盒边界内的浏览器上下文存储 | 已规划（Planned）（`06-delivery-plan.md` P2 事项 4；浏览器工具存在 —— `tools/browser_*` —— 但上下文持久化作为产品状态不存在） |

## 状态机

计划中的产品生命周期（`02-agent-runtime-contract.md` §沙盒生命周期）：

```text
(none) -> created            sandbox.create
created -> attached          sandbox.attach (bound to session)
attached -> sleeping         sandbox.sleep (idle policy or explicit)
sleeping -> attached         sandbox.wake
attached|sleeping -> recycled  sandbox.recycle (env destroyed)
recycled -> attached         sandbox.restore_artifacts (new env, restored files)
any -> failed                backend error -> `sandbox_unavailable`
```

当前实现：环境由工具层按运行/会话创建（`init_session`）并通过 `cleanup` 销毁；不存在 sleep/wake 或 restore —— 被销毁环境的状态仅通过同步文件存活。

规则：

- 回收绝不可静默丢失任务文件：restore_artifacts 是定义的返回路径。
- 媒体作业不得依赖沙盒存活 —— 作业在媒体作业服务中是持久的，沙盒是一次性的。
- Sleep/wake 转换对队列中的审批必须不可见（审批暂停智能体，而非沙盒合约）。

## API 与事件

已实现（内部）：`BaseEnvironment` 接口 —— 使用 cwd/timeout/env 构造、`_run_bash` 执行、`init_session`、`cleanup`、临时目录访问；通过配置选择后端；文件同步辅助函数。

计划中（网关操作，引自运行时合约）：`sandbox.create`、`sandbox.attach`、`sandbox.sleep`、`sandbox.wake`、`sandbox.recycle`、`sandbox.restore_artifacts`。

计划中的事件：沙盒状态变更通过 `status.update` 呈现（事件流没有专用沙盒事件；阶段变更是 `02-agent-runtime-contract.md` §事件流 中定义的高级状态）。

凭证注入（计划中）：环境环境变量表面仅携带 `HF_JWT_TOKEN` 类作用域令牌；静态提供商密钥保留在外部（见 `17-tokenrouter.md` 四阶段流程）。

## 数据模型

已实现：带有 cwd、timeout、env 映射的进程内环境对象；沙盒目录下的 cwd 标记/状态 JSON 存储（`base.py` `_load_json_store`/`_save_json_store`）；进程注册表条目。

计划中：

```text
sandboxes
- sandbox_id
- session_id (current binding; nullable when sleeping)
- backend: local | docker | modal | daytona | singularity | ssh
- state: created | attached | sleeping | recycled | failed
- task_files_root
- resource_profile (cpu/mem/net limits)
- created_at, last_active_at

sandbox_artifacts (for restore)
- sandbox_id
- manifest of synced paths -> object storage keys
- captured_at
```

## UI 行为

- 检查器/实时面板显示当前会话的沙盒状态（后端、attached/sleeping、最后活动）—— 沙盒作为可见的任务计算机，而非隐藏的基础设施。
- 唤醒延迟诚实显示（正在唤醒的沙盒显示"waking"，而非冻结的转圈）。
- 当用户发起时，Recycle/restore 是显式的、可确认的操作；自动回收（空闲策略）必须在任务时间线中可见。
- 共享会话绝不向查看者暴露沙盒文件内容或环境（"共享对话不意味着共享沙盒或凭证"，`05-memory-marketplace-files.md` §访问控制）。

## 权限与错误处理

- 沙盒操作是会话所有者操作；服务账户（例如调度器）需要显式作用域。
- 类型化错误：`sandbox_unavailable`（`02-agent-runtime-contract.md` §错误合约）—— 当 attach/create/wake 失败时呈现；智能体必须报告它，而非降级为假装执行已发生。
- 凭证边界检查（TokenRouter 之后）：环境的环境映射不得包含静态提供商密钥 —— 验收测试 grep 沙盒环境和挂载文件（"沙盒环境和挂载文件不包含真实提供商密钥"，`hermes-tokenrouter-credential-flow.md` §MVP 验收检查）。
- 沙盒内部资源耗尽（OOM、磁盘）必须映射为带有后端证据的可见工具错误，绝不可静默截断输出。

## 验收标准

- 切换后端配置（local -> docker）改变执行隔离而不改变工具行为（抽象基类合约成立）。
- 会话中写入的文件在环境拆除后通过同步存在，并且（P2 之后）在通过 `restore_artifacts` 回收后存在。
- 恢复的会话要么重新附加其沙盒，要么恢复产物 —— 并告知用户发生了哪一种。
- 在媒体作业中途终止环境不会终止作业（一旦存在持久作业，持久性边界成立）。
- 集成 TokenRouter 后：任何沙盒后端内部都读取不到静态提供商密钥；过期令牌失败闭合。
- `sandbox_unavailable` 作为类型化、可操作的错误到达 UI。

## 非目标

- P0-P2 中的多租户容器编排（K8s 调度、自动扩缩容）—— 仅单部署后端。
- 沙盒作为针对部署操作员的安全边界（它保护提供商/凭证并隔离任务执行；它在 MVP 中不是敌对多租户监狱）。
- GUI/VNC 桌面流（本地浏览器/桌面桥是 P2 事项 5，单独规格定义）。
- 拥有任务文件产品语义（文件组件）或作业持久性（媒体作业服务）。

## 开放问题

1. 托管产品的默认后端：每会话 Docker vs Modal/托管 —— 成本和冷启动权衡尚未决定。
2. 每后端的睡眠实现：Docker pause vs checkpoint vs 拆除+恢复；哪些后端能真正支持 `sleep` vs 仅支持 `recycle`？
3. 空闲策略：谁决定睡眠（网关定时器？成本预算？）以及默认 TTL 是多少？
4. `restore_artifacts` 范围：完整任务根 vs 清单选择的路径；产物暂存于何处（按计划的 `sandbox_artifacts` 表的对象存储密钥）？
5. 每会话一个沙盒，还是跨项目会话可共享（运行时合约按会话绑定；产品措辞"任务计算机"暗示每任务）？
6. 现有的 `tools/environments` 配置表面如何映射到每会话产品选择 —— 后端是否对用户可见/可选择？
