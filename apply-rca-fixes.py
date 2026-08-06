#!/usr/bin/env python3
"""Apply RCA fixes for Z.AI 429, Kimi 403/429, and GLM hangs to the runtime Hermes install.

The Hermes runtime installs to:
    C:\\Users\\<user>\\AppData\\Local\\hermes\\hermes-agent\\

which is a SEPARATE copy from any dev / git checkout. Editing the dev tree
alone has no effect on the running agent. This script applies the four RCA
fixes from issue #16/#17 directly to the runtime copy and clears the
bytecode cache so the changes take effect on the next gateway restart.

Idempotent: every patch is guarded by a sentinel check, so re-running the
script after a successful patch is a no-op (exits 0 with "already applied").

Fixes applied:
  1. error_classifier.py _classify_by_status 429 — check _BILLING_PATTERNS
     before the rate_limit fallback. Fixes Z.AI code-1113 "Insufficient
     balance" 429 being retried 3x before failing.
  2. error_classifier.py _classify_by_status 403 — match Kimi's "usage
     limit for this billing cycle" / "quota will be refreshed" / "purchase
     extra usage" phrases as billing (was misclassified as auth).
  3. error_classifier.py _classify_by_error_code — add Z.AI code "1113"
     to the billing code set.
  4. reasoning_timeouts.py — add GLM-5.2 / GLM-5 / GLM-4.6 / GLM-4.5 to
     _REASONING_STALE_TIMEOUT_FLOORS so thinking-mode GLM streams don't
     get killed by the 180s default stale detector.

Usage:
    python apply-rca-fixes.py            # patches default runtime path
    python apply-rca-fixes.py --check    # check only, no changes
    python apply-rca-fixes.py --path X   # patches a custom install path
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# Resolve the default runtime path from HERMES_HOME or the conventional
# AppData location. ``Path.home()`` keeps this portable across usernames.
_DEFAULT_RUNTIME = Path(os.environ.get(
    "HERMES_HOME",
    str(Path.home() / "AppData" / "Local" / "hermes"),
)) / "hermes-agent"


# ── Patch 1: 429 billing check ───────────────────────────────────────────
# Inserted right after the _OVERLOADED_PATTERNS check in the 429 branch.
PATCH_429_SENTINEL = "# Some providers mislabel account-balance exhaustion as HTTP 429"
PATCH_429_BLOCK = '''        # Some providers mislabel account-balance exhaustion as HTTP 429
        # instead of the standards-correct 402. Z.AI / Zhipu is the worst
        # offender: it returns ``HTTP 429 {"error":{"code":"1113",
        # "message":"Insufficient balance or no resource package. Please
        # recharge."}}`` when the GLM_API_KEY account is out of credits.
        # That body is identical in meaning to a 402 billing exhaustion — the
        # credential is valid but the account can't pay — so retrying is
        # pointless (3 retries waste ~10s before the loop gives up) and the
        # right action is to rotate / fall back immediately. Check the body
        # against the billing patterns AND the Z.AI-specific code 1113 before
        # the rate_limit fallback so this case is handled here. (#16)
        if any(p in error_msg for p in _BILLING_PATTERNS):
            return result_fn(
                FailoverReason.billing,
                retryable=False,
                should_rotate_credential=True,
                should_fallback=True,
            )
'''
PATCH_429_ANCHOR = '''        if any(p in error_msg for p in _OVERLOADED_PATTERNS):
            return result_fn(
                FailoverReason.overloaded,
                retryable=True,
            )
        # Distinguish an OpenRouter-aggregator upstream 429'''

# ── Patch 2: 403 Kimi billing patterns ───────────────────────────────────
PATCH_403_OLD = '''    if status_code == 403:
        # OpenRouter 403 "key limit exceeded" is actually billing. Other
        # providers also use 403 for account-plan or credit exhaustion.
        if (
            "key limit exceeded" in error_msg
            or "spending limit" in error_msg
            or any(p in error_msg for p in _BILLING_PATTERNS)
        ):'''
PATCH_403_NEW = '''    if status_code == 403:
        # OpenRouter 403 "key limit exceeded" is actually billing. Other
        # providers also use 403 for account-plan or credit exhaustion.
        # Kimi / Moonshot's Coding Plan (api.kimi.com/coding) returns
        # HTTP 403 with type=permission_error and the body "You've reached
        # your usage limit for this billing cycle. Your quota will be
        # refreshed in the next cycle. To continue now, purchase extra usage
        # or upgrade your plan" when the subscription quota is exhausted.
        # The wording contains "usage limit" + "billing cycle" + "quota will
        # be refreshed" + "purchase extra usage" + "upgrade your plan" — none
        # of those are in the generic _BILLING_PATTERNS list (which matches
        # "exceeded your current quota" / "insufficient balance" etc.), so
        # without this disambiguation the error falls through to the auth
        # branch, surfaces as "Non-retryable client error" to the user, and
        # never triggers the billing-aware recovery path (rotate credential,
        # activate fallback, show "credits exhausted" status). Match the
        # Kimi-specific phrases here so the recovery is identical to a 402.
        if (
            "key limit exceeded" in error_msg
            or "spending limit" in error_msg
            or ("usage limit" in error_msg and "billing cycle" in error_msg)
            or "quota will be refreshed" in error_msg
            or "purchase extra usage" in error_msg
            or any(p in error_msg for p in _BILLING_PATTERNS)
        ):'''

# ── Patch 3: code 1113 in billing code set ──────────────────────────────
PATCH_CODE_1113_OLD = '''        "balance_depleted",
        "model_not_supported_on_free_tier",
    }:'''
PATCH_CODE_1113_NEW = '''        "balance_depleted",
        "model_not_supported_on_free_tier",
        # Z.AI / Zhipu GLM returns code "1113" with "Insufficient balance
        # or no resource package. Please recharge." — surfaced as HTTP 429
        # (see _classify_by_status 429 branch) but also reachable here when
        # the body shape preserves the structured code. Treat as billing so
        # the recovery is rotate/fallback rather than retry. (#16)
        "1113",
    }:'''

# ── Patch 4: GLM reasoning timeout floors ────────────────────────────────
PATCH_GLM_OLD = '''    ("deepseek-v4-pro", 600),'''
PATCH_GLM_NEW = '''    ("deepseek-v4-pro", 600),
    # Z.AI / Zhipu GLM-4.5-and-later reasoning models.  GLM-5.2 ships with
    # thinking enabled by default on the OpenAI-compatible endpoint
    # (api.z.ai/api/paas/v4) — see plugins/model-providers/zai/__init__.py
    # — and routinely pause for minutes during extended thinking before
    # emitting the first content token.  Without a floor, the default
    # HERMES_STREAM_STALE_TIMEOUT of 180s (and the httpx read timeout of
    # 120s) fire *before* GLM-5.2 finishes thinking, tearing down a
    # healthy reasoning stream mid-think.  The user-visible symptom is
    # "GLM models just hang without doing anything" followed by a stale-
    # stream kill — because no content tokens ever arrive inside the
    # default window.  GLM-5.2 max-effort thinking is the slowest variant
    # (300s floor); GLM-5 / GLM-4.6 / GLM-4.5 are progressively lighter.
    # Slug-anchored like the rest of the table so "glm-4-9b" (a non-
    # thinking variant) is NOT matched. (#17)
    ("glm-5.2", 300),
    ("glm-5p2", 300),  # Fireworks alias spelling
    ("glm-5", 240),
    ("glm-4.6", 180),
    ("glm-4.5", 180),'''


def _apply(text: str, old: str, new: str, sentinel: str, label: str) -> tuple[str, bool, bool]:
    """Return (new_text, already_applied, changed)."""
    if sentinel in text:
        return text, True, False
    if old not in text:
        return text, False, False
    return text.replace(old, new, 1), False, True


def patch_file(path: Path, ops: list[tuple[str, str, str, str, str]]) -> int:
    """Apply a sequence of (old, new, sentinel, label) ops to *path*.

    Returns the number of ops that actually changed the file. Exits the
    script with a non-zero status if any op fails to find its anchor.
    """
    original = path.read_text(encoding="utf-8")
    text = original
    changes = 0
    for old, new, sentinel, label in ops:
        text, already, changed = _apply(text, old, new, sentinel, label)
        if already:
            print(f"  [SKIP] {label} — already applied")
        elif changed:
            print(f"  [ OK ] {label}")
            changes += 1
        else:
            print(f"  [FAIL] {label} — anchor not found in {path.name}")
            print("         The runtime file may be a different version than "
                  "this patch was built against.")
            sys.exit(2)
    if changes:
        path.write_text(text, encoding="utf-8")
        # Clear bytecode cache so the next import picks up the new source.
        pycache = path.parent / "__pycache__"
        if pycache.is_dir():
            for pyc in pycache.glob(f"{path.stem}.*.pyc"):
                try:
                    pyc.unlink()
                    print(f"  [CLEA] removed stale bytecode {pyc.name}")
                except OSError:
                    pass
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply RCA fixes for Z.AI/Kimi/GLM to the runtime Hermes install.",
    )
    parser.add_argument(
        "--path",
        default=str(_DEFAULT_RUNTIME),
        help=f"Runtime hermes-agent install path (default: {_DEFAULT_RUNTIME})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check whether patches are applied; do not modify anything.",
    )
    args = parser.parse_args()

    runtime = Path(args.path)
    ec_path = runtime / "agent" / "error_classifier.py"
    rt_path = runtime / "agent" / "reasoning_timeouts.py"

    print(f"Runtime install: {runtime}")
    if not ec_path.is_file() or not rt_path.is_file():
        print(f"ERROR: expected files not found under {runtime / 'agent'}")
        return 3

    total_changes = 0

    print("\n=== Patching agent/error_classifier.py ===")
    if args.check:
        ec_text = ec_path.read_text(encoding="utf-8")
        for label, sentinel in [
            ("429 billing check", PATCH_429_SENTINEL),
            ("403 Kimi billing patterns", '"quota will be refreshed"'),
            ("code 1113 billing", '"1113",'),
        ]:
            status = "applied" if sentinel in ec_text else "MISSING"
            print(f"  [{status:>8}] {label}")
    else:
        total_changes += patch_file(ec_path, [
            (PATCH_429_ANCHOR, PATCH_429_BLOCK + PATCH_429_ANCHOR, PATCH_429_SENTINEL, "429 billing check"),
            (PATCH_403_OLD, PATCH_403_NEW, '"quota will be refreshed"', "403 Kimi billing patterns"),
            (PATCH_CODE_1113_OLD, PATCH_CODE_1113_NEW, '"1113",', "code 1113 billing"),
        ])

    print("\n=== Patching agent/reasoning_timeouts.py ===")
    if args.check:
        rt_text = rt_path.read_text(encoding="utf-8")
        for label, sentinel in [
            ("GLM-5.2 floor", '("glm-5.2", 300)'),
            ("GLM-5 floor", '("glm-5", 240)'),
        ]:
            status = "applied" if sentinel in rt_text else "MISSING"
            print(f"  [{status:>8}] {label}")
    else:
        total_changes += patch_file(rt_path, [
            (PATCH_GLM_OLD, PATCH_GLM_NEW, '("glm-5.2", 300)', "GLM reasoning timeout floors"),
        ])

    if args.check:
        print("\n--check: no modifications made.")
        return 0

    if total_changes == 0:
        print("\nAll patches already applied — no changes needed.")
    else:
        print(f"\nApplied {total_changes} patch(es). Restart the Hermes gateway "
              "(`hermes restart` or restart the desktop app) to pick up the changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
