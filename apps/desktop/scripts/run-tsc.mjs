// Spawn the resolved `tsc` binary with the args this script received.
//
// npm scripts run via `cmd.exe /d /s /c <script>` on Windows, which prepends
// only `apps/desktop/node_modules/.bin` to PATH. When npm hoists
// `typescript` to the monorepo root the shim lives at
// `../../node_modules/.bin/tsc.cmd` and is invisible to `npm run` (issue
// #94796). Going through `node` here means the resolution is unambiguous
// and the user's NODE_ENV / shell PATH quirks can't break it.
//
// We resolve via `resolve-bin.mjs` (which also handles the .cmd shim on
// Windows) and then `spawnSync` with `stdio: 'inherit'` so output streams
// through unchanged. Exit code is propagated.
//
// Usage in package.json:
//   "clean:e2e": "node scripts/run-tsc.mjs --build tsconfig.e2e.json --clean"
//   "typecheck": "node scripts/run-tsc.mjs -p . --noEmit"

import { spawnSync } from "node:child_process"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

import { resolveBin } from "./resolve-bin.mjs"
import { isMain } from "./utils.mjs"

function main() {
  const fromDir = resolve(dirname(fileURLToPath(import.meta.url)), "..")
  let tscPath
  try {
    tscPath = resolveBin("tsc", { from: fromDir })
  } catch (err) {
    // resolveBin already throws an actionable error; just translate to a
    // friendlier prefix so the user sees WHICH script is missing tsc.
    console.error(
      `\nrun-tsc: cannot locate the TypeScript compiler binary.\n` +
        `  invoked from: ${fromDir}\n` +
        `  underlying error: ${err && err.message ? err.message : err}\n` +
        `  Re-run \`assert-tsc-available.mjs\` for the full search trail and fix hints.\n`
    )
    process.exit(err && typeof err.exitCode === "number" ? err.exitCode : 1)
  }

  const args = process.argv.slice(2)
  // On Windows, `.cmd` / `.bat` shims cannot be spawned directly with
  // CreateProcess -- Node returns EINVAL. The reliable workaround is to
  // invoke them through cmd.exe explicitly with `/d /s /c`. We bypass
  // the shell PATH search by quoting the absolute path on the cmdline,
  // which sidesteps the very PATH ambiguity we're working around.
  //
  // On POSIX the resolved path is a real ELF binary; spawn it directly
  // with no shell so we don't reintroduce a search path.
  let result
  if (process.platform === "win32") {
    // Bare path, NO surrounding quotes. spawnSync already handles the
    // CreateProcess quoting for us, and adding extra quotes around the
    // path makes cmd.exe try to find a command literally named
    // `"D:\path\tsc.cmd"` (with the quote characters as part of the
    // name), which is never what we want.
    result = spawnSync("cmd.exe", ["/d", "/s", "/c", tscPath, ...args], {
      stdio: "inherit",
      shell: false,
      windowsHide: true
    })
  } else {
    result = spawnSync(tscPath, args, {
      stdio: "inherit",
      shell: false,
      windowsHide: true
    })
  }
  if (result.error) {
    console.error(`run-tsc: failed to spawn ${tscPath}: ${result.error.message}`)
    process.exit(1)
  }
  // spawnSync returns null signal when the child exited on its own.
  const code = typeof result.status === "number" ? result.status : 1
  process.exit(code)
}

if (isMain(import.meta.url)) {
  main()
}

export default {}