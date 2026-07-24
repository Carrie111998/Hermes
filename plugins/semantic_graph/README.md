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
