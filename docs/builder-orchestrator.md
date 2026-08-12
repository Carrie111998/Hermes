# Governed builder orchestrator

The builder orchestrator turns an owner-registered implementation contract into
one restricted Hermes Kanban worker. The worker receives an immutable execution
packet, can edit only allowed repository paths, and must pass the registered
validation profile before the adapter records completion evidence.

The adapter does not push, open or merge pull requests, approve its own work, or
accept arbitrary filesystem paths and shell commands from callers.

## Operator workflow

The adapter must already be running with an owner-only runtime configuration.
The CLI defaults to `~/.hermes/builder-adapter/runtime.json`; set
`HERMES_BUILDER_ADAPTER_CONFIG` or pass `--config` to use another file.

Check the local service and list the jobs the owner has registered:

```console
hermes orchestrate health
hermes orchestrate cycles
```

Start one registered job:

```console
hermes orchestrate start CYCLE_ID
```

The command prints the generated dispatch ID and the exact status command. A
specific dispatch UUID can be supplied with `--dispatch-id` when recovering an
idempotent request whose response was lost.

Monitor the job and retrieve its evidence:

```console
hermes orchestrate status DISPATCH_ID --cycle CYCLE_ID
hermes orchestrate evidence DISPATCH_ID --cycle CYCLE_ID
```

Cancel only when the job should genuinely stop:

```console
hermes orchestrate cancel DISPATCH_ID --cycle CYCLE_ID
```

Cancellation terminates the worker process tree and archives the native task;
it is not a pause operation.

## What is registered before `start`

`start` deliberately cannot invent a task contract. The owner-controlled
runtime and governance snapshot bind:

- objective and acceptance criteria;
- repository, branch, worktree, and exact starting commit;
- permitted paths;
- builder model and tool policy;
- validation commands and isolation policy;
- runtime, heartbeat, and retry limits.

This separation keeps the convenient operator command from becoming an
unrestricted remote-code-execution interface. Preparing new contracts is an
administrative operation; starting and monitoring an already registered job is
an operator operation.

## Authentication

Authenticated commands use the active key registered for the current local Unix
user. The secret is read only from its approved environment variable and is
never printed or written by the CLI. If several keys match, select one with
`--key-id`.
