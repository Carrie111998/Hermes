# Semantic Graph Memory

Typed provenance graph for Hermes Agent memory, evidence, artifacts, and output evaluation.

## Enable

```bash
hermes plugins enable semantic-graph
```

Config (`config.yaml`):

```yaml
plugins:
  enabled:
    - semantic-graph
  entries:
    semantic-graph:
      config:
        capture_turns: true
        capture_tool_events: false
        auto_extract: explicit
        retrieval_enabled: true
```

## Tools

- `semantic_graph_status` / `begin_run` / `ingest` / `submit_fragment`
- `semantic_graph_search` / `get` / `finalize`
- `semantic_graph_evaluate_output` / `feedback` / `export`

Physical purge is **CLI-only**: `hermes semantic-graph purge --before YYYY-MM-DD --confirm PURGE`.

## Privacy

- Secrets are redacted before DB write.
- Recall is wrapped as data-only context (not instructions).
- Hidden chain-of-thought is never stored.
- `auto_extract=all` costs an extra LLM call per turn.

## Windows

DB lives under `%HERMES_HOME%\semantic-graph\semantic_graph.db` (profile-aware via `get_hermes_home()`).

For the operator-managed local embedding endpoint, keep the launch details in the
same plugin entry. The Go watchdog reads this optional mapping only when it is
started by `scripts\windows\Start-HermesGoWatchdog.ps1`; Hermes itself never
downloads a model or starts the server.

```yaml
plugins:
  entries:
    semantic-graph:
      config:
        embedding:
          enabled: true
          endpoint: http://127.0.0.1:8082
          model: nsfw-bge-m3-v5-q6_k
          runtime:
            enabled: false # set true only after the local files are verified
            executable: C:\path\to\llama-server.exe
            model_path: C:\path\to\nsfw-bge-m3-v5-q6_k.gguf
            arguments:
              - --alias
              - nsfw-bge-m3-v5-q6_k
              - --embedding
              - --ctx-size
              - "8192"
              - --n-gpu-layers
              - "99"
              - --device
              - Vulkan0
              - --flash-attn
              - auto
              - --threads
              - "8"
              - --batch-size
              - "512"
              - --ubatch-size
              - "512"
            startup_timeout_seconds: 180
```

`--model`, `--host`, and `--port` stay watchdog-owned so the configured loopback
endpoint cannot be silently redirected. The runtime mapping is disabled by
default; it does not alter the embedding adapter's own `enabled: false` default.
