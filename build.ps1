<#
.SYNOPSIS
  奇计离线安装包一键编译脚本（7z SFX 方案，含全量质量检查）

.DESCRIPTION
  基于 docs/offline-build/BUILD-GUIDE.md 的完整流程，整合所有实战质量检查：
  - 编译前：git 工作区检查（防意外回退）+ vendor 生成 + 品牌化验证 + 完整性检查
  - 编译中：tsc+vite → electron-builder --dir → 7z SFX 拼接
  - 编译后：文件大小检查 + dist 关键功能 grep 验证

  必须在 Windows PowerShell 5.1+ 运行（node-pty/csc.exe/7zr.exe 均为 Windows 原生）。

.PARAMETER SkipVendor
  跳过 vendor 生成。仅当"只改了前端代码、vendor 未变"时使用。

.PARAMETER FastRepack
  快速重打包模式：vendor 不变 + win-unpacked 已存在，只重跑 7z 压缩。
  适合纯前端改动（.tsx/.ts），约 5 分钟出包。
  注：此模式在 WSL 编译前端，在 PowerShell 跑 7z 拼接。

.PARAMETER HermesHome
  Hermes CLI 安装目录（vendor 数据源）。默认 %LOCALAPPDATA%\hermes

.EXAMPLE
  .\build.ps1                 # 完整编译（推荐）
  .\build.ps1 -SkipVendor     # vendor 已存在，跳过生成
  .\build.ps1 -FastRepack     # 只改前端，快速重打包
#>
param(
    [switch]$SkipVendor,
    [switch]$FastRepack,
    [string]$HermesHome = "$env:LOCALAPPDATA\hermes"
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$desktopDir = Join-Path $repoRoot "apps\desktop"
$releaseDir = Join-Path $desktopDir "release"
$vendorDir = Join-Path $desktopDir "build\vendor"

# WSL 路径（用于调用 wsl.exe 跑 grep/前端编译）
if ($repoRoot -match '^\\\\wsl\.localhost\\([^\\]+)\\(.*)$') {
    # UNC path \\wsl.localhost\<distro>\<path> → /<path>
    $wslRepoRoot = '/' + ($matches[2] -replace '\\', '/')
} elseif ($repoRoot -match '^\\\\wsl$\\([^\\]+)\\(.*)$') {
    $wslRepoRoot = '/' + ($matches[2] -replace '\\', '/')
} else {
    $repoRootWin = $repoRoot -replace '\\', '/'
    $drive = $repoRootWin.Substring(0, 1).ToLower()
    $pathRest = $repoRootWin.Substring(2)
    $wslRepoRoot = "/mnt/$drive$pathRest"
}

# ============================================================
# 辅助函数
# ============================================================

function Write-Step($n, $msg) {
    Write-Host "`n[$n] $msg" -ForegroundColor Cyan
}

function Write-Ok($msg) { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "  [X]  $msg" -ForegroundColor Red }

function Stop-Build($msg) {
    Write-Err $msg
    Write-Host "`n编译中止。修复上述问题后重新运行。`n" -ForegroundColor Red
    exit 1
}

function Check-Path($path, $name) {
    if (Test-Path $path) {
        Write-Ok "$name"
        return $true
    } else {
        Write-Err "$name — 缺失: $path"
        return $false
    }
}

# ============================================================
# 开始
# ============================================================

$mode = if ($FastRepack) { "快速重打包" } elseif ($SkipVendor) { "跳过 vendor" } else { "完整编译" }
Write-Host "`n============================================" -ForegroundColor Green
Write-Host "  奇计离线包编译（7z SFX）— $mode" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host "  仓库: $repoRoot"
Write-Host "  HermesHome: $HermesHome"

# ============================================================
# [0] 编译前检查清单（防实战踩坑）
# ============================================================

Write-Step 0 "编译前检查清单"

# 0a. 基础工具链
$allOk = $true
$allOk = $allOk -and (Check-Path "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe" "csc.exe (.NET 编译器)")
$allOk = $allOk -and (Check-Path (Join-Path $releaseDir "7zr.exe") "7zr.exe (需手动下载到 release/)")

if ($FastRepack) {
    $allOk = $allOk -and (Check-Path (Join-Path $releaseDir "win-unpacked") "win-unpacked (快速模式必需)")
} else {
    # 完整模式需要 Hermes CLI 工具链
    $allOk = $allOk -and (Check-Path (Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe") "Hermes venv python")
    $allOk = $allOk -and (Check-Path (Join-Path $HermesHome "bin\uv.exe") "uv.exe")
    $allOk = $allOk -and (Check-Path (Join-Path $HermesHome "git\cmd\git.exe") "PortableGit")
    $allOk = $allOk -and (Check-Path (Join-Path $HermesHome "node\node.exe") "Node.js")
}

# 0b. git 工作区检查（pitfall #99：意外回退导致功能缺失）
# Windows git.exe 可能损坏（STATUS_ENTRYPOINT_NOT_FOUND）。
# git 操作在编译前由 WSL 侧预写入 .build/git-status.txt 和 .build/git-sha.txt
Write-Host "`n  --- Git 工作区状态 ---" -ForegroundColor DarkCyan
$gitStatusFile = Join-Path $repoRoot ".build\git-status.txt"
$gitShaFile = Join-Path $repoRoot ".build\git-sha.txt"

if (Test-Path $gitStatusFile) {
    $gitStatus = Get-Content $gitStatusFile
    $gitExit = 0
} else {
    # Fallback: 尝试 Windows git（如果可用）
    $gitStatus = git -C $repoRoot status --short 2>&1
    $gitExit = $LASTEXITCODE
    if ($gitExit -ne 0) {
        Write-Warn "Windows git 不可用（exit $gitExit），且未找到预生成的 .build/git-status.txt"
        Write-Warn "请在 WSL 中运行: bash scripts/prepare-build.sh"
        $gitStatus = @()  # 不阻塞，但跳过工作区检查
    }
}

# 检查危险信号：D（deleted）文件
$deletedFiles = $gitStatus | Where-Object { $_ -match '^\s*D' }
if ($deletedFiles) {
    Write-Warn "检测到已删除文件（可能是意外回退）："
    $deletedFiles | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    Write-Host "`n  如果不是你有意删除，请检查:" -ForegroundColor Yellow
    Write-Host "    git -C '$repoRoot' diff HEAD --name-status" -ForegroundColor DarkGray
    Write-Host "  恢复: git -C '$repoRoot' checkout HEAD -- <file>`n" -ForegroundColor DarkGray
    $confirm = Read-Host "  继续? (y/N)"
    if ($confirm -ne 'y' -and $confirm -ne 'Y') {
        Stop-Build "用户中止（工作区有未处理的删除）"
    }
} else {
    Write-Ok "Git 工作区无删除文件"
}

# 显示所有改动
$modified = ($gitStatus | Where-Object { $_ -match '^\s*[MARC?]' })
if ($modified) {
    Write-Host "  已修改/未跟踪文件:" -ForegroundColor DarkGray
    $modified | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
} else {
    Write-Ok "工作区干净"
}

if (-not $allOk) {
    Stop-Build "基础检查未通过"
}

# ============================================================
# [1] 修复 PowerShell 脚本编码（LF → CRLF + UTF-8 BOM）
# ============================================================

if (-not $FastRepack) {
    Write-Step 1 "修复 .ps1 编码"
    foreach ($f in @("prepare-offline.ps1", "install.ps1")) {
        $path = Join-Path $repoRoot "scripts\$f"
        if (Test-Path $path) {
            $raw = [System.IO.File]::ReadAllText($path)
            $crlf = $raw -replace "`r`n", "`n" -replace "`n", "`r`n"
            $utf8bom = New-Object System.Text.UTF8Encoding($true)
            [System.IO.File]::WriteAllText($path, $crlf, $utf8bom)
            Write-Ok "Fixed: $f"
        }
    }
}

# ============================================================
# [2] 生成 / 验证 vendor
# ============================================================

if ($FastRepack) {
    Write-Step 2 "快速模式：跳过 vendor（直接用现有 vendor + win-unpacked）"
} elseif ($SkipVendor) {
    Write-Step 2 "跳过 vendor 生成（-SkipVendor）"
    if (-not (Test-Path $vendorDir)) {
        Stop-Build "vendor 目录不存在，不能跳过。去掉 -SkipVendor 重新运行。"
    }
    Write-Warn "使用现有 vendor，假设内容正确"
} else {
    Write-Step 2 "生成 vendor"
    if (Test-Path $vendorDir) {
        Remove-Item -Recurse -Force $vendorDir
        Write-Host "  清理旧 vendor"
    }
    $prepareScript = Join-Path $repoRoot "scripts\prepare-offline.ps1"
    & $prepareScript -HermesHome $HermesHome -VendorDir $vendorDir
    # PowerShell .ps1 不像 .exe 有可靠的 exit code —— 如果脚本没显式 exit，
    # $LASTEXITCODE 保留的是最后一条外部命令的 exit code（可能是非 0）。
    # 改为验证产物是否存在，而非依赖 exit code。
    if (-not (Test-Path (Join-Path $vendorDir "hermes-agent"))) {
        Stop-Build "prepare-offline.ps1 失败 — vendor/hermes-agent 不存在"
    }
}

# Vendor 完整性检查（完整模式才做）
if (-not $FastRepack -and (Test-Path $vendorDir)) {
    Write-Step "2b" "Vendor 完整性检查"
    $checks = @(
        @{ Name="uv.exe";          Path="bin\uv.exe" },
        @{ Name="git.exe";         Path="git\cmd\git.exe" },
        @{ Name="node.exe";        Path="node\node.exe" },
        @{ Name="python.exe";      Path="python\python.exe" },
        @{ Name="site-packages";   Path="site-packages" },
        @{ Name="node_modules";    Path="nm" },
        @{ Name="hermes-agent";    Path="hermes-agent" },
        @{ Name="tools (rg)";      Path="tools\rg.exe" },
        @{ Name="venv-scripts";    Path="venv-scripts" },
        @{ Name="chromium";        Path="chromium" }
    )
    $vendorOk = $true
    foreach ($c in $checks) {
        $full = Join-Path $vendorDir $c.Path
        if (Test-Path $full) {
            Write-Ok $c.Name
        } else {
            Write-Err "$($c.Name) — 缺失!"
            $vendorOk = $false
        }
    }
    if (-not $vendorOk) {
        Stop-Build "vendor 不完整，请检查 Hermes CLI 安装（第 3.2 步）"
    }

    # 品牌化验证（防 vendor 里混入上游原版源码）
    $webServer = Join-Path $vendorDir "hermes-agent\hermes_cli\web_server.py"
    if (Test-Path $webServer) {
        Write-Host "`n  --- 品牌化检查 ---" -ForegroundColor DarkCyan
        $branded = Select-String "Run QiJi" $webServer -Quiet
        $upstream = Select-String "Run Hermes" $webServer -Quiet
        if ($upstream -and -not $branded) {
            Write-Err "vendor 里的后端源码是上游原版（Run Hermes），非 fork！"
            Write-Err "重新生成 vendor 前，确认 prepare-offline.ps1 从仓库根复制"
            Stop-Build "品牌化验证失败"
        } elseif ($branded) {
            Write-Ok "品牌化正确（Run QiJi）"
        } else {
            Write-Warn "未找到 Run QiJi/Hermes 标记，请手动确认品牌化"
        }
    }
}

# ============================================================
# [3] 清缓存 + 设置 commit hash
# ============================================================

Write-Step 3 "清缓存 + 设置 commit hash"
$viteCache = Join-Path $desktopDir "node_modules\.vite"
if (Test-Path $viteCache) {
    Remove-Item -Recurse -Force $viteCache
    Write-Ok "清 Vite 缓存"
} else {
    Write-Ok "无 Vite 缓存"
}
# git SHA：优先读预生成文件，否则 fallback Windows git
if (Test-Path $gitShaFile) {
    $env:GITHUB_SHA = (Get-Content $gitShaFile -First 1).Trim()
} else {
    $env:GITHUB_SHA = git -C $repoRoot rev-parse HEAD 2>$null
}
if (-not $env:GITHUB_SHA) {
    Stop-Build "无法获取 commit hash。请在 WSL 中运行: bash scripts/prepare-build.sh"
}
Write-Host "  GITHUB_SHA: $env:GITHUB_SHA"

# ============================================================
# [4] 编译
# ============================================================

if ($FastRepack) {
    # ---- 快速重打包：WSL 编译前端 + PowerShell 跑 7z ----
    Write-Step 4 "快速重打包（前端重编 + 7z 拼接）"

    Write-Host "`n  [4a] 从 WSL 重新编译前端..." -ForegroundColor DarkCyan
    # wsl.exe 的 vite build 会把 PLUGIN_TIMINGS 写到 stderr，PowerShell
    # ErrorActionPreference=Stop 会把它当致命异常抛出。临时降为 Continue。
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $wslOutput = wsl.exe -e bash -lc "cd '$wslRepoRoot/apps/desktop' && npx tsc -b && npx vite build" 2>&1
    $wslExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
    $wslOutput | ForEach-Object { Write-Host $_ }
    if ($wslExit -ne 0) {
        Stop-Build "WSL 前端编译失败 (exit $wslExit)"
    }
    Write-Ok "前端编译完成"

    Write-Host "`n  [4b] 用新 dist 覆盖 app.asar...`n  （asar 需包含 dist + electron + assets + public + package.json）" -ForegroundColor DarkCyan
    $asarTool = Join-Path $desktopDir "node_modules\.bin\asar.CMD"
    # asar pack 必须打包整个 app 目录（dist + electron + assets + public + package.json），
    # 不能只打包 dist。如果只打包 dist，electron main.cjs 入口会丢失，
    # 导致安装后退化成 Electron 默认欢迎页（版本号画面）。
    # 解决方案：创建临时 staging 目录 → pack → 验证。
    $asarBin = Join-Path $repoRoot "node_modules\@electron\asar\bin\asar.js"
    $asarResolved = if (Test-Path $asarTool) { $asarTool } elseif (Test-Path $asarBin) { "node `"$asarBin`"" } else { $null }
    if ($asarResolved) {
        $stagingDir = Join-Path $env:TEMP "qiji-asar-staging"
        if (Test-Path $stagingDir) { Remove-Item -Recurse -Force $stagingDir }
        New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null
        foreach ($sub in @("dist", "electron", "assets", "public", "package.json")) {
            $src = Join-Path $desktopDir $sub
            if (Test-Path $src) {
                Copy-Item -Path $src -Destination $stagingDir -Recurse -Force
            }
        }
        Push-Location $stagingDir
        if ($asarResolved -eq $asarTool) {
            & $asarResolved pack . (Join-Path $releaseDir "win-unpacked\resources\app.asar")
        } else {
            & node $asarBin pack . (Join-Path $releaseDir "win-unpacked\resources\app.asar")
        }
        $asarExit = $LASTEXITCODE
        Pop-Location
        Remove-Item -Recurse -Force $stagingDir -ErrorAction SilentlyContinue
        if ($asarExit -ne 0) { Stop-Build "asar pack 失败" }
        Write-Ok "app.asar 已更新（含完整 app 目录）"
    } else {
        Stop-Build "asar 工具不可用（node_modules/.bin/asar.CMD 和 @electron/asar 均缺失）。FastRepack 无法更新 app.asar，前端改动不会生效。用完整编译（不带 -FastRepack）代替。"
    }

    Write-Host "`n  [4c] 7z 压缩 + launcher3 拼接..." -ForegroundColor DarkCyan
    $installerDir = Join-Path $desktopDir "installer"
    Push-Location $installerDir
    $oldPayload = Join-Path $releaseDir "qiji-portable.7z"
    if (Test-Path $oldPayload) { Remove-Item $oldPayload -Force }
    node build-installer.cjs
    $exitCode = $LASTEXITCODE
    Pop-Location
} else {
    # ---- 完整编译：tsc+vite → electron-builder --dir → 7z SFX ----
    Write-Step 4 "编译（tsc+vite → electron-builder --dir → 7z SFX）"

    Push-Location $desktopDir
    # npm run dist:win:sfx 内部 vite build 会把 PLUGIN_TIMINGS 写到 stderr，
    # ErrorActionPreference=Stop 会误杀。临时降为 Continue。
    $prevEAP3 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    npm run dist:win:sfx 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP3
    Pop-Location
}

# ============================================================
# [5] 编译后验证
# ============================================================

Write-Step 5 "编译后验证"

if ($exitCode -ne 0) {
    Stop-Build "编译失败（npm exit code: $exitCode）"
}

# 5a. 产物大小
$setupExe = Get-ChildItem (Join-Path $releaseDir "Qiji-*-Setup.exe") -ErrorAction SilentlyContinue | Select-Object -First 1
if ($setupExe) {
    $sizeMB = [math]::Round($setupExe.Length / 1MB)
    Write-Host "`n  产物: $($setupExe.Name)" -ForegroundColor Cyan
    Write-Host "  大小: $sizeMB MB" -ForegroundColor Cyan
    if ($sizeMB -lt 500) {
        Stop-Build "产物仅 $sizeMB MB，小于 500MB — vendor 很可能不完整！"
    }
    Write-Ok "文件大小正常（$sizeMB MB >= 500MB）"
} else {
    Stop-Build "未找到 Qiji-*-Setup.exe"
}

# 5b. dist 关键功能 grep（防功能缺失）
Write-Host "`n  --- dist 关键功能检查 ---" -ForegroundColor DarkCyan
# 用 WSL grep 检查 dist/assets 里的 JS 产物。
# 必须 -E（扩展正则）：不加的话 | 是字面量不是 OR，导致永远匹配不到。
$distAssets = "$wslRepoRoot/apps/desktop/dist/assets"
$grepChecks = @(
    @{ Pat="skill-picker|SkillPicker|onSelectSkill"; Desc="技能选择器" },
    @{ Pat="qiji-relay|aicps";                        Desc="中转站选项" },
    @{ Pat="中转站";                                   Desc="中转站文案" }
)
foreach ($g in $grepChecks) {
    $prevEAP2 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $result = wsl.exe -e bash -lc "grep -lE '$($g.Pat)' '$distAssets'/*.js 2>/dev/null" 2>&1
    $ErrorActionPreference = $prevEAP2
    if ($result -and $result.ToString().Trim()) {
        Write-Ok "$($g.Desc) 已编译进 dist"
    } else {
        Write-Err "$($g.Desc) 未找到！dist 可能缺少功能"
        Write-Host "    grep pattern: $($g.Pat)" -ForegroundColor DarkGray
    }
}

# ============================================================
# 完成
# ============================================================

Write-Host "`n============================================" -ForegroundColor Green
Write-Host "  [OK] 编译完成！" -ForegroundColor Green
if ($setupExe) {
    Write-Host "  $($setupExe.FullName)" -ForegroundColor Cyan
    Write-Host "  $sizeMB MB" -ForegroundColor Cyan
}
Write-Host "============================================`n" -ForegroundColor Green
