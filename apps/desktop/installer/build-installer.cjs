/**
 * 奇计离线安装器构建脚本
 * 
 * 用法（在 Windows PowerShell 中运行）：
 *   cd apps/desktop/installer
 *   node build-installer.js
 * 
 * 前置条件：
 *   1. apps/desktop/release/win-unpacked/ 已存在（npm run dist:win:dir 产物）
 *   2. apps/desktop/release/7zr.exe 已存在（从 https://www.7-zip.org/a/7zr.exe 下载）
 *
 * 一键编译：
 *   npm run dist:win:sfx
 * （= tsc+vite build → electron-builder --dir → build-installer.cjs）
 * 
 * 产物：
 *   apps/desktop/release/Qiji-0.17.0-Setup.exe（~649MB）
 * 
 * 架构：
 *   launcher3.exe（597KB，含 admin manifest + 内嵌 7zr.exe）
 *   + qiji-portable.7z（LZMA2 压缩的 win-unpacked）
 *   = 单文件安装器
 *   
 *   7z 原生支持在 exe 中查找嵌入的 .7z 签名，不需要 footer 或临时文件。
 *   admin manifest 使安装器启动时弹 UAC，获得权限后自动 Add-MpPreference 跳过 Defender 扫描。
 */
const { execSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const ROOT = path.resolve(__dirname, '..')
const RELEASE = path.join(ROOT, 'release')
const INSTALLER = __dirname

// Config
const APP_NAME = 'Qiji'
const VERSION = '0.17.0'
const PRODUCT_NAME_ZH = '奇计'

// Paths
const winUnpacked = path.join(RELEASE, 'win-unpacked')
const sevenZip = path.join(RELEASE, '7zr.exe')
const payload = path.join(RELEASE, 'qiji-portable.7z')
const launcherSrc = path.join(INSTALLER, 'launcher3.cs')
const uninstallSrc = path.join(INSTALLER, 'uninstall.cs')
const manifest = path.join(INSTALLER, 'app.manifest')
const uninstallExe = path.join(RELEASE, 'uninstall.exe')
const launcherExe = path.join(RELEASE, 'launcher3.exe')
const output = path.join(RELEASE, `${APP_NAME}-${VERSION}-Setup.exe`)

const CSC = 'C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319\\csc.exe'

function step(n, msg) { console.log(`\n[${n}] ${msg}`) }

// ---- Pre-flight ----
step(1, 'Pre-flight checks')
if (!fs.existsSync(winUnpacked)) {
  console.error(`ERROR: ${winUnpacked} not found. Run "npm run dist:win:dir" first.`)
  process.exit(1)
}
if (!fs.existsSync(sevenZip)) {
  console.error(`ERROR: ${sevenZip} not found. Download from https://www.7-zip.org/a/7zr.exe`)
  process.exit(1)
}
console.log('  win-unpacked: OK')
console.log('  7zr.exe: OK')

// ---- Compress payload ----
step(2, 'Compressing win-unpacked to qiji-portable.7z ...')
if (fs.existsSync(payload)) fs.unlinkSync(payload)
execSync(`"${sevenZip}" a -t7z -mx=5 -mmt=on "${payload}" "${winUnpacked}\\*"`, { stdio: 'inherit' })
const payloadSize = (fs.statSync(payload).size / 1024 / 1024).toFixed(1)
console.log(`  Payload: ${payloadSize} MB`)

// ---- Compile uninstall.exe ----
step(3, 'Compiling uninstall.exe ...')
if (fs.existsSync(uninstallExe)) fs.unlinkSync(uninstallExe)
execSync(`"${CSC}" /nologo /optimize /target:exe /out:"${uninstallExe}" "${uninstallSrc}"`, { stdio: 'inherit' })
const uninstallSize = (fs.statSync(uninstallExe).size / 1024).toFixed(1)
console.log(`  uninstall.exe: ${uninstallSize} KB`)

// ---- Compile launcher3.exe ----
// /target:winexe = GUI subsystem (no console window by default).
// launcher3.cs calls AllocConsole() at startup to show install progress,
// then FreeConsole() + Environment.Exit(0) before launching Qiji so the
// console window closes immediately instead of lingering as a parent of Qiji.
step(4, 'Compiling launcher3.exe (admin manifest + embedded 7zr.exe + uninstall.exe) ...')
if (fs.existsSync(launcherExe)) fs.unlinkSync(launcherExe)
execSync(`"${CSC}" /nologo /optimize /target:winexe /win32manifest:"${manifest}" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /resource:"${sevenZip}" /resource:"${uninstallExe}" /out:"${launcherExe}" "${launcherSrc}"`, { stdio: 'inherit' })
const launcherSize = (fs.statSync(launcherExe).size / 1024).toFixed(1)
console.log(`  launcher3.exe: ${launcherSize} KB`)

// ---- Concatenate ----
step(5, 'Concatenating launcher3.exe + qiji-portable.7z ...')
if (fs.existsSync(output)) fs.unlinkSync(output)
const launcherBuf = fs.readFileSync(launcherExe)
const payloadBuf = fs.readFileSync(payload)
const combined = Buffer.concat([launcherBuf, payloadBuf])
fs.writeFileSync(output, combined)
const outputSize = (fs.statSync(output).size / 1024 / 1024).toFixed(1)
console.log(`  Output: ${output}`)
console.log(`  Size: ${outputSize} MB`)

// ---- Verify ----
step(6, 'Verifying embedded 7z archive ...')
try {
  execSync(`"${sevenZip}" t "${output}"`, { stdio: 'inherit', timeout: 30000 })
  console.log('\n  Verification: PASSED')
} catch (e) {
  console.error('\n  Verification: FAILED - 7z could not find embedded archive')
  process.exit(1)
}

console.log(`\n============================================`)
console.log(`  Installer built successfully!`)
console.log(`  ${output}`)
console.log(`  ${outputSize} MB`)
console.log(`============================================`)
