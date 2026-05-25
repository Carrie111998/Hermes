# TGG iLinked reconciliation lookup

`tools.tgg_ilinked_lookup` is Christopher's read-only matching layer between
WhatsApp/operator-cases facts and the latest captured iLinked corpus.

## Source

The lookup reads a local corpus directory only. It never submits iLinked forms
and does not require a live browser session.

Resolution order:

1. `CHRISTOPHER_ILINKED_CORPUS_DIR`
2. newest `~/pcl/ilinked-corpus/tgg/full-import-*`

Expected corpus shape is the browser-owner crawler output:

- `tree/*.json`
- each JSON has `grid.headers` and `grid.rows`
- canonical fields are read from `Task Number`, `Description`, `Task Type`,
  `Location`, `Created Date`, `Created By`, `Sub Status`, `Status`

## API

```python
from tools.tgg_ilinked_lookup import query_ilinked

result = query_ilinked(
    {
        "message": "worker says blk 223A #12-4947 door done",
        "jobNo": "PG/JOB/2605/0334",  # optional
        "block": "223A",              # optional
        "unit": "12-4947",            # optional
        "limit": 5,
    },
    corpus_dir="/path/to/full-import",
)
```

The PA bridge command operation can call the same layer:

```yaml
ilinked_lookup:
  type: command
  tenant: tgg
  command:
    - python3
    - -m
    - tools.tgg_ilinked_lookup
  timeout: 90
```

## Return shape

```json
{
  "ok": true,
  "query": {
    "raw": "worker says blk 223A #12-4947 door done",
    "jobNo": "PG/JOB/2605/0334",
    "block": "223A",
    "unit": "12-4947"
  },
  "confidence": "exact",
  "matches": [
    {
      "confidence": "exact",
      "score": 1.0,
      "reasons": ["task_no_exact"],
      "entry": {
        "taskNo": "PG/JOB/2605/0334",
        "jobNo": "PG/JOB/2605/0334",
        "taskType": "Job",
        "description": "...",
        "location": "...",
        "status": "...",
        "subStatus": "...",
        "leaf": "Job (1381)",
        "sourceFile": "tree/leaf-0001-page-first.json"
      }
    }
  ],
  "meta": {
    "adapter": "tools.tgg_ilinked_lookup",
    "corpus_dir": "/path/to/full-import",
    "indexed_entries": 1234,
    "read_only": true
  }
}
```

## Confidence levels

- `exact` — task/job number from the query exactly matches an iLinked `Task Number`.
- `high_similarity` — no exact task number, but block+unit or strong address
  similarity identifies a likely entry.
- `low` — candidate exists but needs human/agent confirmation before being
  treated as the canonical iLinked case.
- `no_match` — no candidate crossed the floor. This is the gap-detection signal:
  WhatsApp/operator-cases has something iLinked does not, or the query lacked
  enough address information.

## Read-only boundary

This layer only reads local JSON corpus files. It does not call the iLinked
browser relay, does not open Chromium, does not POST WebForms state, and does
not write to operator-cases.
