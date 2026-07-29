# 奇计 Agent 变更历史

> 从 qiji-agent/memory-bank/progress.md 精简而来。
> 详细的每日调试记录见 git log。

---

## 2026-07-26 自定义端点检测 + 启动进度条 + 更新检测修复

- **自定义端点"未配置"bug（第3次复现）：** onboarding 把 API key 写到 `model.api_key`，但设置页面只去 `providers[slug].api_key` 找，永远找不到。之前 7/24(commit 430145c93) 和 7/25(commit 7d5f574cb) 已修过，改为只检查 `base_url` 存在性——被上游合并引入的 `hasApiKey` 检查覆盖导致复现。修复：恢复 `hasEndpoint = Boolean(baseUrl)`，同时后端 `_normalize_config_for_web` 增加 `model_has_api_key` 布尔值（不泄露 key 明文）。
- **启动进度条消失：** `desktop-onboarding-overlay.tsx` 中 `configured===null`（启动中）的 early-return 返回了纯转圈，挡住了 `<Preparing>` 进度条。修复：删除 early-return，恢复 fall-through。
- **install.ps1 git init 假性失败：** `$ErrorActionPreference="Stop"` 把 git 的 stderr hint（如 `hint: Using 'master'...`）当致命错误，导致 git init/remote/commit 被中断。修复：git 操作块临时切 EAP 为 Continue。
- **更新检测逻辑：** checkUpdates 在 `.git` 不存在时走 HTTP fallback 而非报错；更新成功后刷新 bootstrap marker 的 pinnedCommit 防止假更新提示；PATH 加入 PortableGit；resolveHermesCliBinary 支持 venv python.exe fallback。
- **API key 表单一闪而过：** Picker 组件在 `providers===null`（未加载完）时直接 fall-through 到 ApiKeyForm。修复：加 loading 占位符。
- **涉及文件：** `providers-settings.tsx`, `web_server.py`, `desktop-onboarding-overlay.tsx`, `main.cjs`, `update-http-fallback.cjs`, `install.ps1`

## 2026-07-25 VM 启动 + 中转站 + 全量汉化

- **launcher3 未调 install.ps1 导致 config.yaml 缺失：** launcher3.exe 解压后直接启动 Qiji.exe，后端 fallback 到 DEFAULT_CONFIG(language=en)。修复：在 launcher3.cs 解压后、启动前用 C# 直接初始化数据目录（设 QIJI_HOME + 删旧 HERMES_HOME + 创建 config.yaml language=zh）。
- **VM 无系统 Git：** findGitBash/resolveGitBinary 硬编码 'hermes' 在 VM 上找不到。改用 HERMES_HOME 环境变量。
- **中转站默认地址补 /v1：** `https://www.aicps.vip` → `https://www.aicps.vip/v1`。
- **cmd 窗口不退出：** launcher3.cs 改 `UseShellExecute=true` + `/target:winexe` + AllocConsole/FreeConsole。
- **checkUpdates HTTP fallback：** git spawn 失败时 fallback 到 Gitee HTTP API。
- **provider 汉化：** model_catalog.py 清空 GFW 封锁的 raw.githubusercontent fallback URLs；全量汉化 credential-key-ui/model-settings/uninstall-section/onboarding。
- **涉及文件：** `launcher3.cs`, `main.cjs`, `install.ps1`, `onboarding-overlay.tsx`, `model_catalog.py`, 多个 i18n 文件

## 2026-07-24 提供方汉化 + 模型中文名 + UTF-8 编码修复

- **provider 显示名映射：** 新增 `providerDisplayName()` 映射表——OpenAI/Anthropic/谷歌/智谱/通义千问/月之暗面/百川/星火/混元/豆包等。
- **模型中文名映射：** `model-status-label.ts` 新增映射表——通义千问/智谱GLM/DeepSeek/文心一言/Kimi 等。
- **bootstrap-runner UTF-8 乱码：** install.ps1 在中文 Windows 上输出 GBK，但 Node stream 被设为 utf8 解码导致乱码。改为 `-Command` 模式先设 `[Console]::OutputEncoding=UTF8`。
- **涉及文件：** `model-settings.tsx`, `model-status-label.ts`, `main.cjs`, 多个 i18n 文件

## 2026-07-23 build.ps1 WSL UNC 路径 + asar 打包修复

- **WSL UNC 路径解析：** build.ps1 从 WSL 路径（`\\wsl.localhost\Ubuntu\...`）运行时，`wslRepoRoot` 被错误计算。改为检测 UNC 前缀直接提取 WSL 内部路径。
- **asar 只打包 dist 导致 Electron 退化默认页：** asar pack 只打包了 dist/（前端），漏了 electron/main.cjs 等主进程文件，Electron 找不到 package.json 的 main 入口。修复：创建 staging 目录（dist+electron+assets+public+package.json）后完整 pack。

---

## 2026-07-08 客户安装时间优化 (NTFS Move + Vendor 瘦身)

- **目标：** 减少客户机器上的安装时间（用户原话"客户体验感比我自己重要"）
- **改动 1 — install.ps1 Move-OrCopy-Dir：** 新增 NTFS Directory.Move() helper 函数，Stage-VendorFiles 的 7 处 robocopy 全部替换。同卷 move 是 MFT 元数据操作（<1ms），不再逐文件物理复制 2.3GB 数据。跨卷或目标已存在时自动 fallback 到 robocopy
- **改动 2 — install.ps1 vendor 目录清理：** staging 后删除 resources\vendor\ 空壳目录
- **改动 3 — prepare-offline.ps1 砍 headless_shell：** 跳过 chromium_headless_shell（270MB），完整 chromium 已支持 headless 模式
- **改动 4 — prepare-offline.ps1 砍 devDependencies：** robocopy 后删除 typescript/eslint/vitest/electron-builder/prettier 等 12+ 个 dev-only 包（scoped packages: @babel/@types/@vitejs/@vitest/@esbuild/@eslint/@testing-library 等）。客户运行时不需要编译/lint/测试工具
- **改动 5 — prepare-offline.ps1 清 __pycache__/.pyc：** site-packages 拷贝后删除 Python 缓存文件，省 30-50MB + 减少数千小文件
- **编译验证：** 21.6 min → 12.3 min (快 43%)，NSIS 908s → 477s (快 48%)，exe 760MB → 682MB
- **客户安装理论提升：** 磁盘写入从 4.6GB 降到 2.0GB（NTFS Move 消除了二次复制）。需在干净机器上实测验证
- **涉及文件：** `scripts/install.ps1`（+`/mnt/c/` 同步副本）、`scripts/prepare-offline.ps1`

## 2026-07-08 python.exe 缺 DLL 修复 (STATUS_DLL_NOT_FOUND)

- **根因：** 坑14（uv trampoline 硬编码路径）的修复把 `python.exe` 复制到 `venv\Scripts\` 但漏了依赖的 DLL。`python311.dll` 是静态导入（objdump PE 导入表验证），OS 加载器在进程启动时就要找它，早于 pyvenv.cfg 解析
- **修复：** install.ps1 d.2 步骤现在额外复制 4 个 DLL（`python311.dll`, `python3.dll`, `VCRUNTIME140.dll`, `vcruntime140_1.dll`）到 `venv\Scripts\`
- **已装机热修命令：** 无需重编译，目标机器 PowerShell 跑一行 `Copy-Item` 即可
- **影响范围：** 所有使用坑14 修复后的离线包（2026-07-06~07）安装的机器

## 2026-06-30 隔离编译架构 + vendor 修复

- **隔离编译**：编译目录从 `AppData\Local\hermes` 迁移到独立的 `C:\Users\84673\qiji-fork`，不再覆盖运行中的 Hermes 实例
- **vendor 无限嵌套修复**：根因是 `Copy-Item -Recurse` 跟随 `node_modules\hermes` junction（指向 `apps\desktop`，含 `build\vendor`）。改用 `robocopy /XJ`
- **GITHUB_SHA 缺失修复**：robocopy 排除了 `.git`，编译脚本找不到 commit hash。编译前设 `$env:GITHUB_SHA`
- **install.ps1 git 防御性检查**：Install-Git 新增检查 `$HermesHome\git` 目录（vendor 复制的 PortableGit），不再依赖跨进程 PATH
- **产物**：Qiji-0.17.0-win-x64.exe (923 MB)，vendor 完整，奇计化通过

## 2026-06-28 GEO Skill 全面测试

- 网页端 6 命令测试（login/rights/keywords/articles/titles/report）
- 桌面客户端 6 命令测试（status/start/platforms/stats/accounts/stop）
- 修复 `accounts` 命令：参数错误（缺 `_resolve_credentials()`）+ 输出泄露 cookies（解析路径错误）
- 已知遗留：`articles` 数据重复 + 列偏移（Bootstrap Table 选择器问题）

## 2026-06-27 GEO Skill v1.4.0 + 增量更新系统

### Bug 修复（3个关键 bug）
1. **my_headless 参数名是反的**：True=可见，False=无头。之前传反了
2. **PowerShell GBK 编码导致 Python 崩溃**：subprocess 改为 `decode('gbk', errors='replace')`
3. **缺凭证导致 push 报网络异常**：新增 `_resolve_credentials()` 自动获取 uid + api_url

### 功能补全
- 网页端新增 9 命令（titles/galleries/knowledge/instructions/categories/write-tasks/batch-fuken/dashboard/consumption）
- 客户端新增 4 命令（stats/delete-account/media-login/ai-auth）
- 功能覆盖率：网页端 40%→100%，客户端 55%→91%

### 增量更新系统
- 路径 A（Python/skills/.md 改动）：跳过 rebuild，~30s
- 路径 B（前端 .ts/.tsx 改动）：tsc+vite+asar，~60s
- 路径 C（Electron 核心改动）：完整 rebuild，~3-4min
- 安全兜底：任何失败自动 fallback 完整 rebuild

### 更新流程三大 bug 修复
1. i18n 类型定义不同步（TS2353 错误）
2. 白标 exe 名不匹配：`_desktop_packaged_executable()` 写死找 `Hermes.exe` → 改为 glob 搜索
3. 更新卡死无提示：build 失败/成功时弹原生对话框

## 2026-06-26 更新体验全面修复（7个问题）

1. bootstrap-runner.cjs 语法错误
2. Windows 应用内更新（不再显示"从终端更新"）
3. 系统托盘（点 X 隐藏到后台）
4. 奇计品牌任务栏图标
5. 桌面图标恢复窗口（second-instance show）
6. 更新进度条卡住 + 自动重启（done 加入终态列表 + app.relaunch）
7. 每日检查更新（30分钟→24小时）

## 2026-06-25 Gitee 镜像 + HTTP Fallback

- Gitee 镜像方案落地（更新源全切 Gitee）
- 基线 tag `v1.0.0-baseline`
- **阻断发现**：360+火绒+Defender 三安全软件共存时 git.exe 被注入 hook 导致崩溃（`STATUS_ENTRYPOINT_NOT_FOUND`）
- HTTP fallback 实现（`update-http-fallback.cjs`）：git.exe 不可用时走 Gitee REST API 检测更新
- `.git` 检查顺序修复 + bootstrap marker SHA fallback

## 2026-06-24 Fork 仓库 + 自主更新 + 离线包 + 知识库 Skill

- Fork 仓库建立（`blank-knight/QIJI-agent`，origin=fork, upstream=NousResearch）
- 全量去 Hermes 化（26文件，+732/-440行）：i18n 376处、main.cjs、package.json、图标
- 离线包支持搬入 fork（8文件）：vendor + bundled install.ps1
- `qiji-knowledge-base` skill 创建（品牌知识库 + 批量导入）
- `qiji-geo` skill 放入 fork `preinstalled/skills/`

## 2026-06-23 离线包完成

- vendor/ 目录 2.58GB（Python+site-packages+node_modules+tools+chromium）
- install.ps1 加 vendor 跳过逻辑（检测到 vendor 内容时跳过网络下载）
- Qiji-1.0.0-win-x64.exe = 524.8MB（离线包）

## 2026-06-22 小白一键安装

- **根因**：install-stamp.json 含假 commit hash → bootstrap 去 GitHub 下载 install.ps1 → 404
- **修复**：install.ps1 打包进 app resources，bootstrap 优先从本地找
- 首启死循环修复：`isBootstrapComplete()` 允许 `pinnedCommit === null`
- 产物：Qiji-1.0.0-win-x64.exe (110.6MB)

## 2026-06-21 技能中文化 + 品牌化第二批

- 80+ 技能中文翻译 + 40+ 工具集翻译（`translations.ts`）
- 卸载区/网关设置/消息平台 全量中文化

## 2026-06-20 全量品牌化 + 编译

- 4 语言 i18n 文件全量去 Hermes（zh/en/zh-hant/ja）
- Logo 替换（放大镜+"奇"字）
- **build 脚本大坑**：step-build3.ps1 从 staging 覆盖文件 → 改 i18n 必须同步三处副本
- 产物：Qiji-1.0.0-win-x64.exe (110.5MB)

## 2026-06-19 全量品牌去 Hermes 化（第一轮）

- 67 个源文件用户可见 "Hermes" → "奇计"
- 4 个语言文件 + Electron 后端 + About 页面 + 图标
- 原则：代码标识符（HermesGateway、HERMES_HOME 等）不改

## 2026-06-18 项目启动

- Logo 生成（SVG 放大镜+AI神经网络）
- qiji-geo Skill 完成（8个操作命令，实测通过）
- 品牌定制（package.json/index.html/main.cjs/presets.ts/icon）
- Windows 编译成功：QijiGEO-1.0.0-win-x64.exe (111MB)
