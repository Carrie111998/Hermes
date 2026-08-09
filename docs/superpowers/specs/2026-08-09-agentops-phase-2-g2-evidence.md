# AgentOps Phase 2 G2 Evidence (Review Request)

**Branch:** `codex/agentops-phase-2-observer`
**Base:** `1500b6efca95b63c2e28082a1bd8050169e71dce`
**Scope authorization:** this isolated branch is authorized for Phase 2
observer implementation only. It does not authorize production rollout,
Gateway/LaunchAgent/Cron changes, service lifecycle operations, LLMs,
Dashboard, R1-R4, merge, or push.

This is an evidence handoff for independent G2 review, not a G2 approval.

## Scope and protected assets

- Phase 0/1 source and tests are unchanged from the reviewed base.
- New code is an unregistered observer library and a bounded opt-in Bridge. It
  registers no Gateway hook and has no daemon/API route change.
- The only new writable location in normal operation is the fixed
  AgentOps-owned `observer.db` beneath the prevalidated Phase 1 state root.
  Target databases are opened through a read-only SQLite URI.
- No production state, user Gateway, existing service configuration, Cron job,
  repository, or business data was changed while producing this evidence.

## G2 evidence matrix

| G2 expectation | Evidence | Test / command |
|---|---|---|
| Five Profile assets are registered and remain observe-only | Fixed Phase 0 IDs; no registry authority mutator; synthetic first inventory snapshot reaches 100% | `test_registry.py`; inventory command below |
| Same log error does not create duplicate Signal across files | Signal identity excludes source path; fan-out suppresses duplicate IDs | `test_log_rotation.py::test_same_log_error_from_two_files_is_a_single_signal_in_fan_out` |
| Log rotation/truncation/restart cursor recovery | Inode plus offset decision resets to zero on changed inode or oversized offset | `test_log_rotation.py::test_log_cursor_recovers_after_rotation_and_truncation` |
| Cron exit 0 plus failed business assertion is unhealthy | Execution and assertion are separate contracts; failed assertion emits `cron.business_assertion_failed` | `test_read_only_collectors.py::test_zero_exit_with_failed_business_assertion_is_unhealthy` |
| Collector failure is isolated | Fan-out turns exceptions/timeouts into one unhealthy batch and continues | `test_collector_protocol.py` |
| Bridge failure is bounded and cannot change caller flow | Consumer exception becomes a bounded FIFO enqueue/status; Bridge imports no Gateway code or hook | `test_bridge.py` |
| Redaction reaches collector and persistence boundaries | Structured keys and values are redacted, then observer-store re-redacts before its transaction | `test_redaction.py`, `test_observer_store.py::test_observer_store_reapplies_redaction_to_signals_and_target_snapshots` |
| Read-only boundary holds | No command runner/service-control primitive in AgentOps source; target SQLite bytes remain unchanged | static scan below; `test_read_only_collectors.py::test_sqlite_collector_keeps_target_database_bytes_unchanged` |
| Runtime-core pack has no executable action | Declarative manifest has `authority_mode: observe_only`, `execution: disabled`, and empty actions | `test_review_pack_manifest.py` |

## Inventory command and raw output

```bash
/Users/molly/Desktop/Hermes/venv/bin/python - <<'PY'
import json
from datetime import datetime, timezone
from plugins.agentops.control.registry import bootstrap_gateway_registry
from plugins.agentops.control.observer_models import TargetSnapshot
registry = bootstrap_gateway_registry()
for target in registry.list_targets():
    registry.record_target_snapshot(TargetSnapshot(target_id=target.target_id, observed_at=datetime(2026, 8, 9, tzinfo=timezone.utc), facts={"inventory": "registered"}))
print(json.dumps({"targets": [target.target_id for target in registry.list_targets()], "coverage": registry.coverage_report().coverage_percent, "authority_modes": sorted({target.authority_mode.value for target in registry.list_targets()})}, sort_keys=True))
PY
```

```text
{"authority_modes": ["observe_only"], "coverage": 100, "targets": ["hermes:profile:default:gateway", "hermes:profile:feishu3:gateway", "hermes:profile:feishu4:gateway", "hermes:profile:feishu5:gateway", "hermes:profile:newbot:gateway"]}
```

## Verification output

```bash
/Users/molly/Desktop/Hermes/venv/bin/python -m pytest -q tests/plugins/agentops
```

```text
bringing up nodes...
bringing up nodes...

........................................................................ [ 79%]
...................                                                      [100%]
91 passed in 3.72s
```

```bash
/Users/molly/Desktop/Hermes/venv/bin/python -m pytest -q tests/hermes_cli/test_plugins.py tests/hermes_cli/test_plugin_cli_registration.py tests/hermes_cli/test_startup_plugin_gating.py tests/hermes_cli/test_plugin_scanner_recursion.py
```

```text
bringing up nodes...
bringing up nodes...

........................................................................ [ 59%]
..................................................                       [100%]
122 passed in 2.34s
```

```text
phase2 read-only boundary scan: PASS; matches=[]
python 3.11 compileall: PASS
python 3.14 compileall: PASS
```

`git diff --check` exited with status 0.

Python 3.14 source compilation succeeded:

```bash
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14 -m compileall -q plugins/agentops tests/plugins/agentops/phase2
```

It does not have the project-declared `psutil` dependency installed, so its
full pytest invocation stopped at collection with
`ModuleNotFoundError: No module named 'psutil'`. This is an environment gap,
not a passing Python 3.14 test result; no interpreter dependency was installed
or modified in this task.

## Known limitations and next review inputs

- This is a library-only observer foundation. No daemon scheduling, production
  target configuration, or live fleet collection was enabled; the 100% record
  above proves registry coverage, not production telemetry freshness.
- Git state is deliberately conservative: it reads direct HEAD/config metadata
  and reports dirty state as `unknown` unless a separate read-only metadata
  callback supplies a fact. It never infers a clean worktree.
- Plist collection records configuration facts only; runtime service state is
  represented separately by process evidence. No lifecycle API exists here.
- Log input is bounded by configured bytes and lines. A line split at the read
  boundary is intentionally treated as an observed fragment rather than
  buffering unbounded input.
- The Bridge is intentionally unregistered and in-memory. Durable bridge
  delivery and production wiring require a later, separately authorized phase.
- G2 independent security/architecture review is still required before merge
  or any next phase.
