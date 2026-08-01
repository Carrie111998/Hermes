import { createRequire } from "module"
import { resolve, join } from "path"

const root = resolve(import.meta.dirname, "..", "..", "..")
const desktopRoot = resolve(import.meta.dirname, "..")
const requireFromDesktop = createRequire(join(desktopRoot, "package.json"))

try {
  requireFromDesktop.resolve("vite/package.json")
} catch {
  console.error(`Run from repo root: cd ${root} && npm ci`)
  process.exit(1)
}
