
import { pathToFileURL } from 'node:url';

// returns true if the passed file is being invoked from node,
// not imported.
//
// process.argv[1] is absent whenever node was not given a script to run —
// `node --input-type=module -e '...'`, `node --eval`, the REPL. Scripts here
// are increasingly imported as modules too (vitest globalSetup, unit tests),
// and pathToFileURL(undefined) throws ERR_INVALID_ARG_TYPE, which turned a
// "was I run directly?" question into a crash at import time. No entry script
// means nothing was invoked from node, so the answer is false.
export function isMain(importMetaUrl) {
    if (!process.argv[1]) return false;
    return importMetaUrl === pathToFileURL(process.argv[1]).href;
}