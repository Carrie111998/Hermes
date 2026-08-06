import { pathToFileURL } from 'node:url'

// Returns true when the given module URL is the Node.js entrypoint rather than
// an imported module. Some loaders do not populate process.argv[1], so treat
// that state as imported instead of throwing while the module is evaluated.
export function isMain(importMetaUrl) {
  const entrypoint = process.argv[1]
  return typeof entrypoint === 'string' && importMetaUrl === pathToFileURL(entrypoint).href
}
