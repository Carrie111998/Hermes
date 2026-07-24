# Scope Control & Re-Run / Update

## Scope Control

Generating a full wiki for a 500K-LOC monorepo is wildly token-expensive. Default to bounded scope:

- Initial scan: max depth 3 directories
- Per-module docs: cap at 10 modules unless user expands scope
- Per-file reads: prefer `search_files` for symbols + `read_file` with `offset`/`limit` over full reads
- Skip vendored code (`vendor/`, `third_party/`, generated code, `_pb2.py`, `.min.js`)

If the user says "do the whole thing exhaustively", believe them — but ballpark the cost first: "this repo has ~340 source files, comprehensive coverage will be expensive — confirm?"

## Re-Run / Update

If `.codewiki-state.json` already exists at the target path:

- Read it for previous SHA and module list
- If source SHA matches: ask user if they want to regenerate or skip
- If SHA differs: offer to regenerate only modules with changed files (`git diff --name-only <old-sha> HEAD`)

Full incremental-regeneration is a future enhancement — for now, regenerating the whole thing is acceptable.
