# Gateway resource safety runbook

The messaging gateway is the control plane. Resource pressure must stop new
agent admission before it stops Telegram, sibling sessions, SSH, or systemd.

## One-command evidence

Run this read-only snapshot before changing a service or resource limit:

```bash
python3 scripts/hermes_gateway_diagnostics.py --since-minutes 30
```

It reports the gateway PID/RSS, host `MemAvailable`, the gateway cgroup's
current/high/max values and memory events, and independently scoped worker
PIDs/RSS. Shutdown log records include the signal, explicit reason, parent,
gateway RSS, host headroom, active and queued task IDs, worker PIDs, and cgroup
OOM counters.

The same output includes at most 100 filtered lifecycle/resource journal
events from the gateway, health guard, and kernel. It deliberately excludes
general chat and command logs.

Admission uses the tighter of host `MemAvailable` and finite gateway-cgroup
headroom (`MemoryMax - MemoryCurrent`). Structured admission logs expose both
witnesses and the effective value used for the decision.

Correlate the timestamp with both service and kernel evidence:

```bash
journalctl -u hermes-gateway.service --since '30 minutes ago' --no-pager
journalctl -k --since '30 minutes ago' --no-pager | grep -Ei 'oom|killed process|memory cgroup'
systemctl show hermes-gateway.service -p MainPID -p Result -p ExecMainCode -p ExecMainStatus -p OOMPolicy -p KillMode
```

Do not label an incident OOM unless kernel or cgroup `memory.events` evidence
supports it. A service-manager SIGTERM, watchdog abort, updater, deployment,
or external health guard is a different failure path.

## Supervisor policy

- SQLite `database is locked`, task count, aggregate memory, or low host
  headroom are admission/alert signals. They must not trigger a whole-gateway
  restart.
- Restart only for a proven gateway control-plane failure. Rate-limit and
  persist the reason; do not create an endless restart loop.
- Keep `KillMode=mixed` and `OOMPolicy=continue` on the gateway. Independently
  scoped workers carry their own finite `MemoryHigh`, `MemoryMax`, and
  `MemorySwapMax=0` so they cannot evade the RAM boundary through swap.
- For production Linux gateways, verify either `systemd-run --user --scope` or
  the same-UID `sudo -n systemd-run --system --scope --uid=<runtime-user>` path
  works before selecting `terminal.worker_cgroup_mode: required`.

## Controlled stress verification

The repository stress command is deliberately capped (`<=12` workers, `<=6`
parallel, `<=64 MiB` per worker, `<=15s`) and does not contact Telegram:

```bash
python3 scripts/stress_gateway_admission.py --workers 4 --parallel 2 --memory-mb 16 --seconds 1
```

Pass requires `peak_active <= parallel_limit`, at least one queue notice when
workers exceed the limit, no subprocess failures, and the parent/gateway test
process surviving.

## Rollout and rollback

1. Capture the diagnostic and recent service/kernel journals.
2. Confirm no unrelated active workloads would be interrupted.
3. Deploy code and config, then perform one controlled gateway restart.
4. Start representative parallel tasks; verify queue notices, FIFO resume,
   Telegram responsiveness, independent worker cgroups, and unchanged gateway
   PID throughout a worker failure.
5. Re-run diagnostics and confirm no gateway cgroup OOM or restart loop.

Rollback is one code/config revert plus one controlled restart. Before that
restart, drain or explicitly reconcile queued and active tasks. Never remove
the worker memory boundary or increase concurrency as an emergency workaround;
set `gateway.admission.max_parallel_agents` lower if pressure remains.
