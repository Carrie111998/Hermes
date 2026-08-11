// Build-time guard: turn a bare Windows dlopen error into an actionable one.
//
// Since electron 40.10.3, `@electron/get` unpacks Electron dists through
// `@electron-internal/extract-zip`, a napi addon. Both of its Windows
// prebuilds — win32-x64 and win32-arm64 — import `VCRUNTIME140.dll`, which
// ships with the Visual C++ 2015-2022 Redistributable and is *not* part of a
// clean Windows install. (The `api-ms-win-crt-*` imports beside it are the
// Universal CRT, which is an OS component from Windows 10 on, so those are
// always satisfied.) Without the redistributable, `LoadLibrary` fails and the
// only symptom is:
//
//     ERR_DLOPEN_FAILED loading index.win32-x64-msvc.node
//
// which names neither the missing DLL nor how to get it.
//
// **This is a backstop, not the first line of defence.** Electron's own
// postinstall unpacks through the same addon, so on a machine without the
// redistributable `npm ci` fails before any script in this repo runs. The
// README carries the prerequisite for that case; this catches the
// electron-builder path — a warm npm cache, or a dist download during
// packaging — and says the actionable sentence out loud.
//
// The load is attempted rather than probing System32 for the DLL: that tests
// the actual condition instead of a proxy for it, and stays correct wherever
// the runtime happens to live.

import { isMain } from "./utils.mjs"

const REDIST_URL = {
  x64: "https://aka.ms/vs/17/release/vc_redist.x64.exe",
  arm64: "https://aka.ms/vs/17/release/vc_redist.arm64.exe",
}

// Node sets `code` for a failed addon load; the message check is a fallback in
// case that ever stops being populated. Anything else — most importantly the
// addon simply not being installed — is not this guard's business.
export function isDlopenFailure(err) {
  if (!err) return false
  if (err.code === "ERR_DLOPEN_FAILED") return true
  return /ERR_DLOPEN_FAILED/i.test(String(err.message ?? err))
}

// Pure apart from the injected loader, so the decision can be unit tested off
// Windows — which is the only place this repo's CI runs.
export async function checkWindowsRuntime({ platform, arch, load }) {
  if (platform !== "win32") {
    return { ok: true, skipped: "not Windows" }
  }
  try {
    await load()
    return { ok: true }
  } catch (err) {
    if (!isDlopenFailure(err)) {
      return { ok: true, skipped: `unrelated load failure (${err?.code ?? "no code"})` }
    }
    const url = REDIST_URL[arch] ?? REDIST_URL.x64
    return {
      ok: false,
      error:
        "the Electron unpacker's native addon could not be loaded.\n" +
        "  @electron-internal/extract-zip links against the Microsoft Visual C++\n" +
        "  2015-2022 Redistributable, which is not installed by default.\n" +
        `  Install it (${arch}) and re-run:\n` +
        `    ${url}`,
    }
  }
}

async function main() {
  const result = await checkWindowsRuntime({
    platform: process.platform,
    arch: process.arch,
    load: () => import("@electron-internal/extract-zip"),
  })

  if (!result.ok) {
    console.error(`\n✗ assert-win-vcruntime: ${result.error}\n`)
    process.exit(1)
  }
}

if (isMain(import.meta.url)) {
  await main()
}

export default { checkWindowsRuntime, isDlopenFailure }
