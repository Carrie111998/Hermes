"""One-shot runtime patcher — applies the four RCA fixes directly to the
runtime Hermes install. Idempotent: re-runs are no-ops once patches land.

Run with the runtime venv Python:
    C:\\Users\\<user>\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe apply-runtime-patches.py
"""
from __future__ import annotations
import sys
from pathlib import Path

RUNTIME = Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent"


def patch(path: Path, old: str, new: str, sentinel: str, label: str) -> bool:
    """Apply one patch. Returns True if a change was made."""
    text = path.read_text(encoding="utf-8")
    if sentinel in text:
        print(f"  [SKIP] {label} - already applied")
        return False
    if old not in text:
        print(f"  [WARN] {label} - anchor not found, skipping")
        return False
    new_text = text.replace(old, new, 1)
    path.write_text(new_text, encoding="utf-8")
    # Clear stale bytecode so the next import picks up the patched source.
    pyc = path.parent / "__pycache__"
    if pyc.is_dir():
        for stale in pyc.glob(f"{path.stem}.*.pyc"):
            try:
                stale.unlink()
            except OSError:
                pass
    print(f"  [ OK ] {label}")
    return True


ec = RUNTIME / "agent" / "error_classifier.py"
rt = RUNTIME / "agent" / "reasoning_timeouts.py"
print(f"Runtime: {RUNTIME}")

# ── Patch 1: 429 billing-pattern check (Z.AI "Insufficient balance") ─────
patch(
    ec,
    old=(
        '        if any(p in error_msg for p in _OVERLOADED_PATTERNS):\n'
        '            return result_fn(\n'
        '                FailoverReason.overloaded,\n'
        '                retryable=True,\n'
        '            )\n'
        '        # Distinguish an OpenRouter-aggregator upstream 429'
    ),
    new=(
        '        if any(p in error_msg for p in _OVERLOADED_PATTERNS):\n'
        '            return result_fn(\n'
        '                FailoverReason.overloaded,\n'
        '                retryable=True,\n'
        '            )\n'
        '        # Some providers mislabel account-balance exhaustion as HTTP 429\n'
        '        # instead of 402. Z.AI / Zhipu returns code 1113 "Insufficient\n'
        '        # balance or no resource package. Please recharge." as a 429 when\n'
        '        # GLM_API_KEY is out of credits. Retrying is pointless — classify\n'
        '        # as billing so we rotate / fall back immediately. (#16)\n'
        '        if any(p in error_msg for p in _BILLING_PATTERNS):\n'
        '            return result_fn(\n'
        '                FailoverReason.billing,\n'
        '                retryable=False,\n'
        '                should_rotate_credential=True,\n'
        '                should_fallback=True,\n'
        '            )\n'
        '        # Distinguish an OpenRouter-aggregator upstream 429'
    ),
    sentinel="# Some providers mislabel account-balance exhaustion as HTTP 429",
    label="429 billing-pattern check (Z.AI 1113)",
)

# ── Patch 2: 403 Kimi billing patterns ───────────────────────────────────
# Anchor on the tail common to both old + new build shapes.
patch(
    ec,
    old=(
        '            or any(p in error_msg for p in _BILLING_PATTERNS)\n'
        '        ):\n'
        '            return result_fn(\n'
        '                FailoverReason.billing,\n'
        '                retryable=False,\n'
        '                should_rotate_credential=True,\n'
        '                should_fallback=True,\n'
        '            )\n'
        '        return result_fn(\n'
        '            FailoverReason.auth,\n'
        '            retryable=False,\n'
        '            should_fallback=True,\n'
        '        )'
    ),
    new=(
        '            or ("usage limit" in error_msg and "billing cycle" in error_msg)\n'
        '            or "quota will be refreshed" in error_msg\n'
        '            or "purchase extra usage" in error_msg\n'
        '            or any(p in error_msg for p in _BILLING_PATTERNS)\n'
        '        ):\n'
        '            return result_fn(\n'
        '                FailoverReason.billing,\n'
        '                retryable=False,\n'
        '                should_rotate_credential=True,\n'
        '                should_fallback=True,\n'
        '            )\n'
        '        return result_fn(\n'
        '            FailoverReason.auth,\n'
        '            retryable=False,\n'
        '            should_fallback=True,\n'
        '        )'
    ),
    sentinel='"quota will be refreshed"',
    label="403 Kimi billing patterns (usage limit / billing cycle)",
)

# ── Patch 3: code 1113 in structured billing code set ───────────────────
# Target the frozenset of error CODES (not the _BILLING_PATTERNS message
# list). Anchor includes _XAI_SPENDING_LIMIT_ERROR_CODE so we land in the
# code set, not the message-pattern list.  Runtime uses 4-space indent here.
patch(
    ec,
    old=(
        '    "balance_depleted",\n'
        '    "model_not_supported_on_free_tier",\n'
        '    _XAI_SPENDING_LIMIT_ERROR_CODE,\n'
    ),
    new=(
        '    "balance_depleted",\n'
        '    "model_not_supported_on_free_tier",\n'
        '    "1113",  # Z.AI / Zhipu GLM "Insufficient balance" code. (#16)\n'
        '    _XAI_SPENDING_LIMIT_ERROR_CODE,\n'
    ),
    sentinel='"1113",  # Z.AI / Zhipu GLM',
    label="code 1113 to billing",
)

# ── Patch 4: GLM reasoning timeout floors ────────────────────────────────
patch(
    rt,
    old='    ("deepseek-v4-pro", 600),\n',
    new=(
        '    ("deepseek-v4-pro", 600),\n'
        '    # Z.AI / Zhipu GLM-4.5-and-later reasoning models. GLM-5.2 ships\n'
        '    # with thinking ON by default and routinely pauses minutes before\n'
        '    # the first content token — the default 180s stale detector kills\n'
        '    # the stream mid-think, surfacing as "GLM hangs without doing\n'
        '    # anything". Floor the stale detector so thinking completes. (#17)\n'
        '    ("glm-5.2", 300),\n'
        '    ("glm-5p2", 300),  # Fireworks alias\n'
        '    ("glm-5", 240),\n'
        '    ("glm-4.6", 180),\n'
        '    ("glm-4.5", 180),\n'
    ),
    sentinel='("glm-5.2", 300),',
    label="GLM-5/5.2 reasoning timeout floors",
)

print("\nDone. Restart the Hermes gateway to pick up the changes.")
