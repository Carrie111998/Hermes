
import { pathToFileURL } from 'node:url';

// returns true if the passsed file is being invoked from node,
// not imported.
export function isMain(importMetaUrl) {
    if (!process.argv[1]) return false
    return importMetaUrl === pathToFileURL(process.argv[1]).href
}