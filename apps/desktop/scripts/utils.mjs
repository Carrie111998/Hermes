import { pathToFileURL } from 'node:url'

// Returns true if the passed file is being invoked from Node,
// not imported.
export function isMain(importMetaUrl) {
  const entrypoint = process.argv[1]
  return typeof entrypoint === 'string' && importMetaUrl === pathToFileURL(entrypoint).href
}
