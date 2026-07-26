# Programme control

Programme control is a single admission gate shared by every Kanban worker
profile. State lives in the shared `kanban.db`; each accepted task attempt is
therefore gated before Atlas, Mercury, Shield, a boss, or a leaf can fan out.

## States

| State | New tasks | In-flight tasks |
| --- | --- | --- |
| `RUNNING` | Admitted | Continue |
| `PAUSED` | Rejected with the stored reason | Continue |
| `DRAINING` | Rejected with the stored reason | Continue; the last completion changes the state to `PAUSED` |
| `HALTED` | Rejected with the stored reason | Stop cooperatively at the next safe leaf-attempt checkpoint |

## CLI

```text
hermes pause --reason "operator hold" --by "adrian"
hermes resume --by "adrian"
hermes drain --by "adrian"
hermes halt --reason "kill switch" --by "adrian"
hermes status
```

The existing `hermes status` report includes programme state, in-flight task
count, the last-change timestamp, and halt-signal presence.

## Halt signal

Entering `HALTED` writes an ISO-8601 UTC timestamp to
`~/.hermes/signals/halt`. Leaves check the file after finishing an attempt and
before starting another; no process is forcibly killed. Entering `RUNNING`
removes the signal. `PAUSED` and `DRAINING` deliberately leave it unchanged.

## Transitions

| From | Command/event | To |
| --- | --- | --- |
| Any | `pause` | `PAUSED` |
| Any | `resume` | `RUNNING` |
| Any | `drain` | `DRAINING` |
| `DRAINING` | In-flight count reaches zero | `PAUSED` |
| Any | `halt` | `HALTED` |

Every explicit or automatic transition updates the singleton state row and
appends one immutable history row in the same `BEGIN IMMEDIATE` transaction.
