# DB Corruption from Concurrent Supervisors

## Problem

Multiple supervisor subprocesses writing to the same SQLite DB (`kanban.db`) can corrupt the database file. This happens when:
1. The initial `workflow_start` spawns a supervisor
2. Kanban hooks spawn additional supervisors for layer advancement
3. Multiple supervisors run concurrently, each trying to write to the same DB

## Symptoms

- `sqlite3.DatabaseError: file is not a database: invalid SQLite header`
- `sqlite3.DatabaseError: database disk image is malformed`
- Kanban tools fail with DB errors
- Tasks can't be created, completed, or blocked

## Recovery

```python
from hermes_cli import kanban_db as kb
import os

db_path = '/home/ubuntu/.hermes/kanban/boards/<board>/kanban.db'
backup = db_path + '.corrupted'
os.rename(db_path, backup)
conn = kb.init_db(board='<board>')
```

This creates a fresh DB. Previous tasks are lost but the board is functional again.

## Prevention

- The hook-based architecture (spawning supervisors from hooks) should reduce concurrency since hooks fire sequentially in worker processes
- The supervisor subprocess should use `stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL` to avoid blocking
- Consider using WAL mode for better concurrent read/write support: `PRAGMA journal_mode=WAL`
