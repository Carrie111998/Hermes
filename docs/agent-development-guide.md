# Agent Development Guide

Read [AGENTS.md](../AGENTS.md) first. It remains the detailed codebase guide.

Charterforge-specific agent work must preserve these invariants:

- the Founder/CEO is the normal planner and dispatcher;
- human silence does not block in-charter progress;
- model output is a proposal, never authority or verification;
- objectives, plans, tasks, permits, results, evidence, accounting, and
  organization state remain durable and structured;
- prompt caching and message-role alternation remain intact;
- every external action is idempotent, bounded, attributable, and verified;
- employees receive exact task grants and enterprise reporting lines;
- new public branding uses Charterforge while migration aliases remain
  explicit and tested;
- project-specific work is pushed only to the independent `origin`.

Before implementing a new autonomous behavior, identify:

1. authoritative input state;
2. candidate-action contract;
3. deterministic policy and budget decision;
4. narrow executor;
5. independent verification evidence;
6. committed state transition;
7. retry, expiry, stop, and escalation behavior;
8. audit-export representation.

Conversation-only state or a prompt instruction is insufficient.

## Validation

Use focused tests for the changed contracts, then the broader governed runtime
suite. Exercise real imports and temporary state roots. Record failed or
unavailable checks accurately. Never commit state databases, `.env`, logs,
provider payloads, or credentials.

