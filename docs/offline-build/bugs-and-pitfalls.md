
---

## 坑16：vendor 瘦身全局清理可靠性问题（NTFS 超时）

**严重度：★★★☆☆（installer 体积不降、文件数不减）**

**现象：** prepare-offline.ps1 末尾的 10 步全局清理（删 `.map`/`.md`/test 目录/`.ts` 源码/`.bin` 等）用 `Get-ChildItem -Recurse` 遍历 80K+ 文件，每步一次完整遍历。在 NTFS 上可能超时或不完整执行——2026-07-08 实测 vendor 仍有大量 `.map` 文件残留（devDeps 砍除和 headless_shell 砍除生效，但全局清理部分未完成）。

**根因：** 11 次完整遍历（10 步清理 + 1 次统计）在 NTFS 上，每个文件需要元数据查询和权限检查。当文件数 > 50K 时，单次遍历耗时 2-3 秒，11 次 = 20-30 秒。如果 Defender 在扫描或磁盘 I/O 瓶颈，遍历可能超时或被 PowerShell 中断。

**实测数据（2026-07-08）：**

| 指标 | 基线（99K 文件） | 优化前（devDeps+headless_shell 砍除后） | 目标（全局清理生效） |
|------|----------------|--------------------------------------|-------------------|
| 文件数 | 99,184 | 80,406 | ~65-70K |
| vendor 大小 | 2303.9 MB | 2034.4 MB | ~1950-2000 MB |
| exe 大小 | 760.6 MB | 775 MB | ~680-700 MB |

**问题定位：**

```bash
# vendor/nm 仍有大量 .map 文件残留
find /mnt/c/Users/84673/qiji-fork/apps/desktop/build/vendor/nm -name "*.map" -type f | wc -l
# 输出：379（应为 0）
```

说明 10 步清理中的 `.map` 删除步骤没执行完。

**修复（2026-07-09）：** 把清理逻辑**前移到 robocopy 复制时一次排除**，不需要后置清理。

**4 处改动（prepare-offline.ps1）：**

1. **PortableGit 复制（L41-47）**：从 `Copy-Item -Recurse` 改为 `robocopy`，加内联排除：
   ```powershell
   robocopy $managedGit $vendorGit /E /XJ /XD "doc" "man" "info" "gtk-doc" /XF "*.vim" "*.adoc" "*.md" "*.markdown" /NJH /NJS /NFL /NDL /NP /R:1 /W:1 | Out-Null
   ```

2. **Node.js 复制（L49-54）**：从 `Copy-Item -Recurse` 改为 `robocopy`（可靠性修复）：
   ```powershell
   robocopy $managedNode $vendorNode /E /XJ /NJH /NJS /NFL /NDL /NP /R:1 /W:1 | Out-Null
   ```

3. **node_modules 复制（L170-173）**：扩展 robocopy `/XD` 和 `/XF` 排除：
   ```powershell
   robocopy $nmPath $vendorNM /E /XJ `
       /XD "build" "dist" "release" ".git" "test" "tests" "__tests__" "spec" ".github" ".vscode" ".idea" ".circleci" `
       /XF "*.map" "*.md" "*.markdown" "CHANGELOG*" "changelog*" "*.ts" ".editorconfig" ".eslintrc*" ".prettierrc*" ".eslintignore" ".npmignore" ".mocharc*" `
       /NJH /NJS /NFL /NDL /NP /R:1 /W:1 | Out-Null
   ```

4. **后置清理全删（L247-259）**：原 10 步清理（244-308 行）替换为：
   ```powershell
   # .bin 包含 dev tool wrappers（vite、tsc、eslint）— 运行时不需要
   $nmBin = Join-Path $VendorDir "nm\.bin"
   if (Test-Path $nmBin) { Remove-Item $nmBin -Recurse -Force -EA SilentlyContinue }

   # 报告（一次遍历而非 11 次）
   $allFiles = Get-ChildItem $VendorDir -Recurse -File -EA SilentlyContinue
   $totalFiles = if ($allFiles) { $allFiles.Count } else { 0 }
   $totalSize = if ($allFiles) { [math]::Round(($allFiles | Measure-Object Length -Sum).Sum / 1MB) } else { 0 }
   Write-Host ("Total vendor: {0:N0} MB ({1:N0} files)" -f $totalSize, $totalFiles) -ForegroundColor Yellow
   ```

**预期收益：**

| 指标 | 改动前 | 改动后（预期） |
|------|--------|--------------|
| prepare-offline 总时间 | ~10min | ~4-5min |
| .map 文件数 | 379 | 0 |
| vendor 文件数 | 80,406 | ~65-70K |
| vendor 大小 | 2034.4 MB | ~1950-2000 MB |

**验证（编译后）：**

```bash
# .map 应为 0
find /mnt/c/Users/84673/qiji-fork/apps/desktop/build/vendor/nm -name "*.map" -type f | wc -l
```

如果仍 > 0，说明 vendor 是旧的（没删掉重新 prepare-offline）。

**风险与防御：**

- `*.ts` 全排除含 `*.d.ts`（类型声明文件）。Electron 生产模式只加载编译后 `.js/.cjs/.mjs`，理论上安全。如出问题 `git reset --hard ef7a4415a` 回退。
- robocopy 的 `/XF` 是文件名模式匹配，不是路径匹配。如果有某个 npm 包的运行时依赖 .ts 文件（极罕见），会出问题。但从观测看，所有 npm 包发布时都是编译后的 JS，TS 文件只是开发时的源码。

**同时排除 ffmpeg（217MB）：**

prepare-offline.ps1 第4步（L57-63）改为只拷 `rg.exe`：
```powershell
New-Item -ItemType Directory -Force -Path $vendorTools | Out-Null
$rgSrc = Join-Path $managedTools "rg.exe"
if (Test-Path $rgSrc) {
    Copy-Item $rgSrc $vendorTools -Force
    Write-Host "[4/8] Tools (rg.exe only, ffmpeg excluded) ✅" -ForegroundColor Cyan
}
```

**教训：**

1. **"先全量复制再删"的模式在 NTFS 大文件场景下不可靠。** 11 次遍历 = 11 倍 I/O，任何一次超时都导致清理不完整。
2. **robocopy 的 `/XF`/`/XD` 是内联排除，复制时就跳过。** 一次遍历完成复制+过滤，效率高且可靠。
3. **PortableGit、Node.js 这些 >50MB 的目录复制必须用 robocopy，不能用 Copy-Item。** 见坑1、坑9b、坑19 的教训。
4. **ffmpeg 被排除需要同步更新 install.ps1 的 staging 逻辑。** 已经更新：install.ps1 L207-220 添加注释说明 ffmpeg 是可选依赖（video_gen 技能需要时再下载），不会影响安装。install.ps1 的 ffmpeg 检查逻辑（L1568-1712）保持不变，用户如果需要 TTS 功能可以手动安装 ffmpeg（scoop/choco/winget）。

---

## 坑20：install.ps1 git init 假性失败（ErrorActionPreference="Stop"）

**严重度：★★☆☆☆（更新功能失效，但不影响安装和正常使用）**

**现象：** install.ps1 在执行 `git init` + `git remote add` + `git commit` 三步时，第一步 `git init` 就抛出异常终止，后续步骤被跳过。日志显示 `[vendor] WARNING: Git init failed at step 'init'`。但 git init 实际上已经成功执行了——只是它的 stderr hint 被误判为致命错误。

**根因：** install.ps1 的 `$ErrorActionPreference = "Stop"`（脚本默认设置）导致 PowerShell 把**任何** stderr 输出当作 terminating error。git 在 init 时会往 stderr 写 hint 文本：

```
hint: Using 'master' as the name for the initial branch. ...
```

这是 git 的正常提示（建议改名 default 分支），不是错误。但 PowerShell 在 `Stop` 模式下把它当异常抛出，中断了 `git init` 后面的 `remote add` 和 `commit`。

**修复：** git 操作块内临时切换 ErrorActionPreference：

```powershell
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    git init ...
    git remote add ...
    git commit ...
} catch {
    $gitInitOk = $false
} finally {
    $ErrorActionPreference = $prevEAP
}
```

**教训：** 在 `$ErrorActionPreference="Stop"` 的 PowerShell 脚本中调用原生命令（git/curl/robocopy）时，必须临时切回 `Continue`，否则命令的正常 stderr 输出会被误判为致命错误。

---

## 坑21：自定义端点"未配置"bug 反复复现（api_key 存储路径不一致）

**严重度：★★★☆☆（用户配了 key 但设置页显示未配置，影响信任）**

**现象：** 用户通过 onboarding 配置中转站 API key 后，设置→提供方→账号的"自定义端点"卡片仍显示"未配置自定义端点"。此 bug 已复现 3 次（7/24、7/25、7/26）。

**根因：** onboarding 和设置页面对 API key 的存储/读取路径不一致：

| 操作 | 写入位置 | 读取位置 |
|------|---------|---------|
| onboarding 保存 | `model.api_key`（通过 `/api/model/set`） | — |
| 设置页面检测 | — | `providers[slug].api_key`（永远找不到） |

onboarding 的 `saveOnboardingLocalEndpoint` 调用 `setModelAssignment({ provider: 'custom', api_key })`，key 被写到 `config.yaml` 的 `model.api_key`。但 `CustomEndpointCard` 组件检查 key 时去 `providers` 块下找——两个路径完全不同。

**修复历史：**

1. **7/24 commit 430145c93：** 改为只检查 `base_url` 存在性（`hasEndpoint = Boolean(baseUrl)`），不检查 key。
2. **7/25 commit 7d5f574cb：** 从 `/api/config` 的扁平化字段 `model_base_url` 读取。
3. **7/26 commit 854b65ceb：** 上游合并引入了 `hasApiKey` 检查，导致 bug 复现。再次恢复为 `hasEndpoint = Boolean(baseUrl)`。

**最终修复：**
- 前端：`hasEndpoint = Boolean(baseUrl)`——只要有 base_url 就算已配置（onboarding 时 base_url + key 一起写）
- 后端：`_normalize_config_for_web` 增加 `model_has_api_key` 布尔值，供前端参考
- 本地 endpoint（Ollama 等不需要 key）也能正确显示为"已配置"

**教训：** 上游合并时，如果上游代码引入了与白标定制冲突的逻辑（如新增 `hasApiKey` 检查），合并后必须验证白标功能是否被覆盖。这个 bug 已经因为上游合并复现了 3 次，后续合并要特别注意 `providers-settings.tsx` 的 `CustomEndpointCard`。

---
