# Tiered Pipeline Context Engine

A three-tier context engine for Hermes Agent that keeps the current task hot while moving older topics into durable, searchable storage.

## Behavior

- **Current task first:** ordinary 50K compaction only demotes messages before the latest explicit task/topic switch (`NEW TASK`, `switch task/topic`, `新任务`, `换个话题`, or `切换任务/话题`). A single ongoing task is left untouched.
- **Emergency checkpoint:** at 85% of the physical model context window, or for an explicit manual compression, older active-task turns are summarized so the session can continue safely.
- **L1:** raw current-task transcript and protected recent tail.
- **L2:** structured summaries for recent/high-value/unresolved topics.
- **L3:** SQLite archive containing summaries plus exact raw source messages.
- **Retrieval:** relevant L2/L3 capsules are added to a request as clearly labelled historical reference, without modifying the persisted transcript.
- **Tool cleanup:** Hermes' deterministic, prompt-cache-aware tool-result pruning starts at 25K tokens and is independent of the 50K topic-compaction threshold.

## Configuration

```yaml
context:
  engine: tiered_pipeline

tiered_pipeline:
  l1:
    trigger_tokens: 50000
    protect_last_n: 20
  l2:
    max_topics: 512
    archive_target_ratio: 0.70
  l3:
    # Optional; defaults to <HERMES_HOME>/context/tiered_pipeline.db
    path: "/path/to/tiered_pipeline.db"
  recall:
    top_k: 3
    max_chars: 6000
  prune:
    trigger_tokens: 25000
    min_result_chars: 8000
    min_reclaim_tokens: 4096
```

When `l3.path` is empty, the profile-aware fallback is `<HERMES_HOME>/context/tiered_pipeline.db`.
For a custom path, keep the `.db`, `-wal`, and `-shm` files together and do not edit them while Hermes is running.

## Agent tools

When the active context engine toolset is exposed, the engine provides:

- `context_search` — search L2/L3 topic capsules.
- `context_recall` — recover exact raw source messages by topic ID.
- `context_list_topics` — list recent capsules and their tiers.
- `context_pin_topic` — pin/unpin a capsule against L2 eviction.
- `context_status` — inspect token and L2/L3 counts.

## Safety properties

- Summary failure returns the original messages unchanged.
- A capsule and its raw source messages are committed in one SQLite transaction before any L2 eviction.
- SQLite WAL mode and foreign keys are enabled.
- Exact raw messages are stored as plaintext JSON. Keep the database on a trusted local path and protect backups accordingly.
- Pinned capsules are not evicted; unresolved work outranks resolved work during L2 overflow.
- Retrieval text is explicitly marked as historical data, not instructions.
