# Obsidian Memory Duo

Memory Duo stores curated deep memory beside the remote Hermes backend using
SQLite/FTS5 and ordinary Markdown under the configured `Hermes Memory` folder.
It is opt-in through `memory.provider: obsidian_duo` and does not require a
local model, vector database, or broker daemon.

The plugin-native diagnostics are:

```text
hermes obsidian_duo status
hermes obsidian_duo doctor
hermes obsidian_duo rebuild-index
hermes obsidian_duo reconcile
hermes obsidian_duo pending
hermes obsidian_duo conflicts
hermes obsidian_duo stats
```
