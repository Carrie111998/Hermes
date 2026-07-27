// Resolve electronDist at runtime (#38673, #47917): electron-builder 26.8.x can
// re-unpack a broken Electron.app; reusing the installed dist dodges that.
// npm workspace hoisting is non-deterministic — require.resolve finds electron
// wherever it landed. Dist present → -c.electronDist=<abs>/dist; absent → let
// electron-builder fetch via @electron/get (electronVersion + ELECTRON_MIRROR).

import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { spawnSync } from "node:child_process"
import { createRequire } from "node:module"

const require = createRequire(import.meta.url)

function electronDistDir() {
  try {
    return path.join(path.dirname(require.resolve("electron/package.json")), "dist")
  } catch {
    return null
  }
}

function distBinary(dist) {
  if (process.platform === "darwin") {
    return path.join(dist, "Electron.app", "Contents", "MacOS", "Electron")
  }
  if (process.platform === "win32") {
    return path.join(dist, "electron.exe")
  }
  return path.join(dist, "electron")
}

function electronBuilderCli() {
  const pkgJson = require.resolve("electron-builder/package.json")
  const bin = require(pkgJson).bin
  const rel = typeof bin === "string" ? bin : bin["electron-builder"]
  return path.join(path.dirname(pkgJson), rel)
}

const dist = electronDistDir()
const args = []
if (dist && fs.existsSync(distBinary(dist))) {
  args.push(`-c.electronDist=${dist}`)
} else {
  console.warn(
    "[run-electron-builder] no local electron dist; electron-builder will fetch " +
      "via @electron/get (electronVersion + ELECTRON_MIRROR)."
  )
}
args.push(...process.argv.slice(2))

const builderEnv = { ...process.env }
let builderTemp = null

if (process.platform === "win32") {
  // NSIS creates short-lived `nst*.tmp` include files while compiling the
  // installer. A machine-wide TEMP (for example C:\Windows\Temp) is vulnerable
  // to concurrent cleanup and security tooling deleting those files between
  // creation and !include. Give each build an isolated user-scoped directory.
  const userTempBase = process.env.LOCALAPPDATA
    ? path.join(process.env.LOCALAPPDATA, "Temp")
    : os.tmpdir()
  fs.mkdirSync(userTempBase, { recursive: true })
  builderTemp = fs.mkdtempSync(path.join(userTempBase, "hermes-electron-builder-"))
  builderEnv.TEMP = builderTemp
  builderEnv.TMP = builderTemp
  builderEnv.TMPDIR = builderTemp
  console.log(`[run-electron-builder] isolated Windows temp: ${builderTemp}`)
}

const result = spawnSync(process.execPath, [electronBuilderCli(), ...args], {
  env: builderEnv,
  stdio: "inherit",
})

if (builderTemp) {
  fs.rmSync(builderTemp, { recursive: true, force: true })
}

if (result.error) {
  console.error(`[run-electron-builder] spawn failed: ${result.error.message}`)
  process.exit(1)
}
process.exit(result.status == null ? 1 : result.status)
