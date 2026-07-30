# Host Turn-Gate Provider Contract

Hermes exposes an opt-in, host-enforced outer-turn gate for deployments that
must pause, drain, or restrict agent work while a runtime policy changes. The
core is policy-neutral: a plugin decides admission and generation, while Hermes
owns lease lifetime and revalidates before tool execution, platform output, and
child-process environment injection.

When `agent.turn_gate` is absent, the extension point is disabled and Hermes
keeps its default behavior. When the section is present, malformed
configuration or a missing required provider fails closed before a turn body or
side effect begins.

## Configuration

```yaml
agent:
  turn_gate:
    required_provider: example-gate
    runtime_identity:
      machine_id: workstation-01
    allowed_child_environment:
      - EXAMPLE_TURN_LEASE_ID
      - EXAMPLE_COORDINATOR_SOCKET
```

- `required_provider` must match the registering plugin manifest key or name.
- `runtime_identity.machine_id` is host-owned. Hermes combines it with the
  active profile, gateway instance, turn ID, and an HMAC-derived session
  instance ID; raw platform chat IDs are not exposed to providers.
- `allowed_child_environment` is the complete host allowlist. Caller-controlled
  values with these names are stripped outside an admitted turn. Inside an
  admitted turn, only provider-returned values from this list are injected.

## Provider API

A plugin registers one provider under its own manifest identity:

```python
from agent.turn_gate import GateDecision, GateState


class ExampleGate:
    def acquire(self, request):
        return GateDecision(
            provider_id="example-gate",
            state=GateState.OPEN,
            lease_id="opaque-lease-id",
            generation=1,
            child_environment=(
                ("EXAMPLE_TURN_LEASE_ID", "opaque-lease-id"),
            ),
        )

    def validate(self, decision, checkpoint):
        return decision

    def release(self, decision):
        return None


def register(ctx):
    ctx.register_turn_gate_provider(ExampleGate(), api_version=1)
```

`acquire(request)` returns a `GateDecision`. `validate(decision, checkpoint)`
is called again at consequential boundaries and must return the current
decision. Hermes rejects provider identity mismatches, lease changes,
generation changes, state downgrades, widened tool permissions, and changed
child-environment contributions. `release(decision)` runs in `finally` after the
outer turn.

Provider exceptions and async provider methods fail closed. A plugin must
declare the exact host contract version when registering; this document defines
`api_version=1`, and mismatches are rejected before the provider can replace a
live registration. A provider may implement `record_tool_observation(...)` to
interpret the generic `(tool_name, tool_args, tool_call_id, result)` envelope.
Hermes does not recognize policy-specific tools or result fields. A provider
rejection or exception poisons the turn; a provider without an observation
callback leaves the gate state unchanged, so `RELOAD_ONLY` still cannot emit
business output.

## Gate states

- `OPEN`: tools and output are admitted, subject to revalidation.
- `CLOSED_DRAINING`: new work, tools, and output are blocked.
- `RELOAD_ONLY`: only the exact `allowed_tools` tuple is admitted; output remains
  blocked until a later turn is admitted as `OPEN`.

Detached background tasks are created without inheriting the parent turn's
context. Any later side effect must acquire its own fresh outer-turn lease.
Plugin force reload snapshots and restores provider ownership transactionally,
so a failed reload cannot silently remove or replace a required provider.
