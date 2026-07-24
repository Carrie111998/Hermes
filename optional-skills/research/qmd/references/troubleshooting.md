# QMD Troubleshooting

---

## Troubleshooting

### "Models downloading on first run"
Normal — qmd auto-downloads ~2GB of GGUF models on first use.
This is a one-time operation.

### Cold start latency (~19s)
This happens when models aren't loaded in memory. Solutions:
- Use HTTP daemon mode (`qmd mcp --http --daemon`) to keep warm
- Use `qmd search` (BM25 only) when models aren't needed
- MCP stdio mode loads models on first search, stays warm for session

### macOS: "unable to load extension"
Install Homebrew SQLite: `brew install sqlite`
Then ensure it's on PATH before system SQLite.

### "No collections found"
Run `qmd collection add <path> --name <name>` to add directories,
then `qmd embed` to index them.

### Embedding model override (CJK/multilingual)
Set `QMD_EMBED_MODEL` environment variable for non-English content:
```bash
export QMD_EMBED_MODEL="your-multilingual-model"
```
