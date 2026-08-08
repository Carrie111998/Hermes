---
title: "Safety & Enforcement Section Template"
---

# Safety & Enforcement — body section template

**Architecture rule:** Enforcement lives *inside* the skill's execution path via a
shared code-level utility (import/call). Never as an agent-selectable policy skill.

## Shared utility (code-level)

- Module: `scripts/policy.py` — repo-relative; never a machine-local path.
- Entry points this skill uses:
  - `policy.check_<guard>(...)` → returns `Decision` (never raises); caller reads `d.allowed`
  - `policy.enforce_<guard>(...)` → calls `check_*`, audit-logs, raises `PolicyViolation` on deny
  - `policy.audit_log(decision, context)` → structured decision record (no secrets)

## When section is required vs optional

| Skill does… | Section |
|---|---|
| Write/delete files, call external APIs, send messages, mutate shared state, handle PII/credentials, enforce auth/rate limits | **Required** (bundled); **Recommended** (optional-skills) |
| Pure read of public docs, formatting, local analysis of user-supplied non-sensitive text | Optional / omit |

## Template: paste into SKILL.md body

Replace bracketed placeholders. Keep prose short — process predictability over narrative.

```markdown
## Safety & Enforcement

Self-guarding: this skill validates preconditions **before** any side effect.
Enforcement is embedded in the skill's own path via a shared code utility —
not a separate agent-visible policy skill.

### Preconditions (fail closed)

| Guard | Trigger | On violation |
|---|---|---|
| [e.g. PII present] | [e.g. content matches `detect_pii`] | Block write; return clear error; audit-log |
| [e.g. missing auth] | [e.g. required env var empty] | Abort before API call |
| [e.g. rate limit] | [e.g. N calls / window exceeded] | Defer or refuse; do not retry-storm |

### Procedure integration

1. **Load inputs** (paths, payloads, env) with `read_file` / env checks as needed.
2. **Run guards** via the skill helper (preferred) or:
   ```
   terminal(command="python3 -c 'from scripts.policy import enforce_<guard>; enforce_<guard>(...)'", timeout=30)
   ```
3. **Only on allow:** proceed to side-effect steps in Procedure.
4. **On deny:** stop; surface the violation message; do not partial-apply.

Completion criterion for this section: every side-effect step in Procedure is
preceded by a named guard, and a unit test asserts the guard blocks a
known-bad input without network (side effect not called; audit log called on deny).
```

## Shared utility contract (`scripts/policy.py`)

Interface contract only — do not ship until a first consumer skill needs it.

```python
"""Shared policy helpers for skill scripts. Not an agent-selectable skill."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional


class PolicyViolation(Exception):
    """Raised when a skill must fail closed."""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    message: str
    context: dict[str, Any]


# Example guards — replace/extend per domain; keep regexes here, not copy-pasted in every skill.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def detect_pii(text: str) -> list[str]:
    """Return list of PII kind labels found (empty if clean)."""
    found: list[str] = []
    if _EMAIL.search(text or ""):
        found.append("email")
    if _SSN.search(text or ""):
        found.append("ssn")
    return found


def check_no_pii(text: str, *, action: str = "write") -> Decision:
    """Return a Decision. Never raises — caller inspects d.allowed."""
    kinds = detect_pii(text)
    if kinds:
        return Decision(
            allowed=False, code="pii_detected",
            message=f"Refusing {action}: detected {', '.join(kinds)}",
            context={"kinds": kinds},
        )
    return Decision(allowed=True, code="ok", message="clean", context={})


def enforce_no_pii(text: str, *, action: str = "write") -> None:
    """Call check_*, audit_log both outcomes, raise PolicyViolation on deny."""
    d = check_no_pii(text, action=action)
    if not d.allowed:
        audit_log(d)
        raise PolicyViolation(d.code, d.message)
    audit_log(d)


def audit_log(decision: Decision, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Return a structured record. Never include raw secrets or full PII payloads."""
    record = {
        "allowed": decision.allowed,
        "code": decision.code,
        "message": decision.message,
        "context": {**decision.context, **(extra or {})},
    }
    return record
```

| Function family | Behavior |
|---|---|
| `check_*` | Returns `Decision` only — **never raises** |
| `enforce_*` | Calls `check_*` → `audit_log` → raises `PolicyViolation` on deny |
| `audit_log` | Structured record; codes/kinds only; no raw secrets/PII payloads |

## Unit-test skeleton

Conventions: stdlib + pytest + `unittest.mock` only, no live network.
Run: `scripts/run_tests.sh tests/skills/test_<skill>_skill.py -q`

### Import / PYTHONPATH note

> `scripts/` must be on `PYTHONPATH` when tests run. Existing Hermes test
> convention uses `python -m pytest` from the repo root (which adds the root to
> `sys.path`). If the skill is in `optional-skills/`, adjust the import to
> `optional_skills.<category>.<name>.scripts.policy` or add a `conftest.py`
> that extends `sys.path`. See `hermes-agent-skill-authoring` → Tests and Docs
> for the canonical pattern.

```python
"""Policy / enforcement tests for <skill>."""
from __future__ import annotations

import pytest
from unittest import mock

# Adjust import path: repo-root pytest → scripts.policy;
# optional-skills → optional_skills.<cat>.<name>.scripts.policy
from scripts.policy import (
    PolicyViolation, check_no_pii, enforce_no_pii, detect_pii, audit_log,
)


class TestPiiGuard:
    def test_detects_email(self):
        assert "email" in detect_pii("contact me at user@example.com please")

    def test_clean_text_has_no_pii(self):
        assert detect_pii("deploy the canary to staging") == []

    def test_check_no_pii_denies_on_ssn(self):
        d = check_no_pii("ssn 123-45-6789", action="bucket_write")
        assert d.allowed is False
        assert d.code == "pii_detected"

    def test_enforce_no_pii_raises(self):
        with pytest.raises(PolicyViolation) as ei:
            enforce_no_pii("email user@example.com", action="bucket_write")
        assert ei.value.code == "pii_detected"

    def test_enforce_no_pii_allows_clean(self):
        enforce_no_pii("no sensitive content here", action="bucket_write")


class TestSkillEmbedsEnforcement:
    """Prove the skill path calls the shared utility before side effects."""

    def test_blocks_write_and_audits(self):
        from scripts import policy as policy_mod
        side_effect = mock.Mock(name="write_bucket")
        with mock.patch.object(policy_mod, "audit_log") as mock_audit:
            def skill_write(payload: str) -> None:
                policy_mod.enforce_no_pii(payload, action="bucket_write")
                side_effect(payload)
            with pytest.raises(PolicyViolation):
                skill_write("leak user@example.com")
            mock_audit.assert_called_once()
        side_effect.assert_not_called()

    def test_allows_write_when_clean(self):
        from scripts import policy as policy_mod
        side_effect = mock.Mock(name="write_bucket")
        def skill_write(payload: str) -> None:
            policy_mod.enforce_no_pii(payload, action="bucket_write")
            side_effect(payload)
        skill_write("metrics batch 42")
        side_effect.assert_called_once_with("metrics batch 42")


class TestAuditLogShape:
    def test_audit_record_has_no_raw_payload(self):
        d = check_no_pii("user@example.com")
        record = audit_log(d, extra={"skill": "example-skill"})
        assert set(record) >= {"allowed", "code", "message", "context"}
        blob = str(record)
        assert "user@example.com" not in blob
```

### Done checklist for enforcement tests

- [ ] Deny case: violation input → guard raises/returns deny
- [ ] Allow case: clean input → side effect proceeds
- [ ] Side-effect mock **not** called on deny
- [ ] Deny path **calls `audit_log`** (mock and assert)
- [ ] No network fixtures, no live credentials
- [ ] Shared utility is the SUT for detection logic; skill tests only prove the call is wired before the side effect

## Anti-patterns

1. Agent-visible policy skill — banned; use `scripts/policy.py`
2. Copy-pasted regexes across 20 skills — put patterns in the shared module
3. Guards only in prose with no test — add `tests/skills/test_*_skill.py`
4. Logging full PII/secrets in audit trails — log codes/kinds only
5. Fail open ("couldn't check, proceed anyway") — fail closed
6. Shipping `scripts/policy.py` with no consumer — template-only until first skill needs it