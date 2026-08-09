// Build-time guard: refuse to hand a half-built renderer to electron-builder.
//
// `npm run pack` / `npm run dist*` are `npm run build && npm run builder`.
// If the `build` step (tsc -b && vite build) fails but packaging proceeds
// anyway — a stale checkout that fails typecheck, an interrupted vite build,
// or npm not short-circuiting `&&` in some shells — electron-builder happily
// packages an app with an empty or missing `dist/`. The result launches but
// blank-pages with `ERR_FILE_NOT_FOUND` for dist/index.html, with no clue why.
//
// This runs at the tail of `build`, after vite build, so any packaging path
// inherits it. It fails loud and early instead of shipping a broken bundle.
// See issues #39484 (renderer blank page) and #41327 / #39472 (dashboard 404).

import { existsSync, statSync, readdirSync, readFileSync } from "fs"
import { join, resolve } from "path"
import { isMain } from "./utils.mjs"

// Pure check — returns { ok: true } or { ok: false, error: "..." }.
// Kept side-effect-free so it can be unit tested without spawning a process.
export function checkDistBuilt(distDir) {
  if (!existsSync(distDir) || !statSync(distDir).isDirectory()) {
    return { ok: false, error: `no dist directory at ${distDir}` }
  }

  const indexHtml = join(distDir, "index.html")
  if (!existsSync(indexHtml) || !statSync(indexHtml).isFile()) {
    return { ok: false, error: `dist/index.html is missing at ${indexHtml}` }
  }
  if (statSync(indexHtml).size === 0) {
    return { ok: false, error: `dist/index.html is empty at ${indexHtml}` }
  }

  // index.html alone isn't enough — vite emits hashed JS into dist/assets.
  // An index.html with no script bundle still blank-pages.
  const assetsDir = join(distDir, "assets")
  let jsFiles = []
  try {
    if (existsSync(assetsDir) && statSync(assetsDir).isDirectory()) {
      jsFiles = readdirSync(assetsDir).filter(name => name.endsWith(".js"))
    }
  } catch (err) {
    return { ok: false, error: `failed reading assets directory at ${assetsDir}: ${err.message}` }
  }

  if (jsFiles.length === 0) {
    return { ok: false, error: `dist/assets has no built JS bundle (expected vite output under ${assetsDir})` }
  }

  // Packaging invariant: react-router must not be duplicated across multiple JS chunks (#82696).
  // When react-router is split across chunks, non-entry chunks get an isolated Router context
  // causing `useLocation()` to throw "may be used only in the context of a <Router> component".
  const routerInvariant = "may be used only in the context of a"
  const chunksWithRouter = jsFiles.filter(name => {
    try {
      const content = readFileSync(join(assetsDir, name), "utf8")
      return content.includes(routerInvariant)
    } catch {
      return false
    }
  })

  if (chunksWithRouter.length === 0) {
    return {
      ok: false,
      error: `react-router context invariant not found in any JS chunk in dist/assets. Ensure the router invariant string is present in the built bundle (#82696)`
    }
  }

  if (chunksWithRouter.length > 1) {
    return {
      ok: false,
      error: `react-router context invariant emitted into ${chunksWithRouter.length} separate chunks (${chunksWithRouter.join(", ")}). Ensure react-router is in vendor-react and resolve.dedupe in vite.config.ts (#82696)`
    }
  }

  return { ok: true }
}

function main() {
  const desktopRoot = resolve(import.meta.dirname, "..")
  const distDir = join(desktopRoot, "dist")
  const result = checkDistBuilt(distDir)

  if (!result.ok) {
    console.error(`\n✗ assert-dist-built: ${result.error}`)
    console.error("  The renderer bundle is missing or incomplete, so packaging")
    console.error("  would produce an app that launches to a blank page.")
    console.error("  Re-run the build and check the tsc/vite output above for the")
    console.error("  real failure, then package again:")
    console.error(`    cd ${desktopRoot} && npm run build\n`)
    process.exit(1)
  }

  console.log("✓ assert-dist-built: dist/index.html + assets present")
}

if (isMain(import.meta.url)) {
  main()
}

export default { checkDistBuilt }
