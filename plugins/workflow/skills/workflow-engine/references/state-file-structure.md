# Workflow State File Structure

Location: `~/.hermes/workspace/docs/fleet-pipelines/.engine-state/<workflow_name>_<run_id>_state.json`

## Structure

```json
{
  "workflow_name": "ideation",
  "current_layer": 1,
  "layers": [
    ["enrich-artifact"],
    ["spec-author-spec"],
    ["qa-review"],
    ["spec-author-revise-spec", "security-security"],
    ["spec-author-revise-for-security"]
  ],
  "context": {
    "inputs": {"grill_artifact": "/path/to/file.md"},
    "grill_artifact": "/path/to/file.md"
  },
  "attachments": ["/path/to/file.md"],
  "states": {
    "enrich-artifact": {
      "status": "done",
      "kanban_card_id": "t_9e2254b6",
      "started_at": "2026-07-23T00:40:35Z",
      "completed_at": "2026-07-23T00:41:06Z",
      "result": "..."
    },
    "spec-author-spec": {
      "status": "running",
      "kanban_card_id": "t_6924803b"
    }
  },
  "results": {
    "enrich-artifact": "done",
    "spec-author-spec": "running"
  },
  "loop_counts": {
    "qa-review:spec-author-revise-spec": 1
  },
  "max_revision_loops": 3,
  "updated_at": "2026-07-23T00:41:06Z"
}
```

## Hook Lookup

When `kanban_task_blocked` or `kanban_task_completed` fires:

1. Scan all `*_state.json` files in `.engine-state/`
2. For each file, check if any node's `kanban_card_id` matches the task_id
3. If found, load the state and process the event

## Loop Count Tracking

`loop_counts` maps `"verify_node:revision_node"` to the current loop count.
When a verify node blocks, the hook increments the count.
After `max_revision_loops` (default 3), the node is marked "escalated".

## Key Fields

- `current_layer` — which layer the engine is currently processing
- `layers` — topological sort of nodes grouped by dependency depth
- `states[node_id].kanban_card_id` — the kanban task ID for this node
- `states[node_id].status` — current status (pending/running/done/blocked/looping/escalated)
- `loop_counts` — tracks revision loops per verify→revision pair
- `context` — workflow context including inputs
- `attachments` — file paths attached to first-layer cards
