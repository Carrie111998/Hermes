# JAY OS v1

JAY OS v1 is the operating contract for Jay/Hermes as an autonomous digital-organization operator for One Memory.

## Non-negotiables

- Parallel by default: independent work is concurrent.
- Foundations first: networking, identity, worker lifecycle, observability, and task leases are not temporary hacks.
- One Memory is the control plane for identity, Knowledge Contexts, skills, tools, capabilities, policies, and provenance.
- Agents do not perform human login/OTP. They use service identities and short-lived scoped tokens.
- Mastra is the standard framework for agents, workflows, traces, scorers, datasets, experiments, and auto-improvement.
- GitHub remains the engineering ledger; this repo stores versioned Jay configuration.
- Safe/reversible operations proceed without founder approval under policy gates.
- Every bug fix requires regression evidence.
- A release is done only after production verification passes; failures roll back or re-enter development.
- Agent errors feed datasets/experiments; promotion requires scorer evidence.
- Capacity target is >=90% of batch-allocatable capacity productively assigned, while preserving control-plane, voice, and personal-device headroom.
- RTX voice workloads preempt batch work. M5 is opportunistic and never critical-path.

## Bootstrap artifacts

- `bootstrap.yaml` — initial measured state, control-plane contract, and active foundation workstreams.
- `worker-registry.schema.json` — versioned registry contract for resource nodes and safety policies.
- `task-lease.schema.json` — lease/provenance contract for autonomous work allocation.
- `scripts/jay_os_probe.py` — local read-only probe for node/tool/git status snapshots.

## First implementation slice

1. Commit immutable v1 contracts into git.
2. Add read-only probes for nodes/providers/repos.
3. Add leases + heartbeat schema before any durable work scheduler.
4. Bind worker capabilities to service identities and policy classes.
5. Wire Mastra traces/scorers/datasets as the improvement loop once API/auth inventory is verified.
