#!/usr/bin/env python3
"""hermes-oauth-expiry-check — daily no_agent watchdog for Google OAuth
re-auth windows, one pass per identity registered in
``_google_identities.py``.

Background
----------
Google OAuth refresh tokens for this system's identities expire ~7 days
after a full re-authentication, while the OAuth consent screen remains in
"Testing" status. Before this job there was no proactive reminder — JID and
family members only found out reactively when something broke. This job:

  1. Estimates each identity's expiry from the re-auth sidecar written by
     ``setup.py --auth-code`` (see that script's ``REAUTH_SIDECAR_PATH``) —
     the token file's own mtime is NOT reliable, since it is rewritten on
     every routine access-token refresh too, not just a full re-auth.
  2. When an identity enters its 2-day warning window, sends that person a
     direct Telegram reminder carrying a ``[ref:oauth_reauth:<id>]`` tag,
     which the gateway's no-agent short-circuit (see gateway/run.py) later
     matches deterministically when they reply with their OAuth code.

Identity generality (do not reintroduce hardcoded names here)
---------------------------------------------------------------
This entire job is driven by iterating ``_google_identities.py``'s
``IDENTITIES`` registry dynamically (``load_identities_registry``) — no
identity name is hardcoded anywhere below. The one behavioral fork this job
makes (daily-repeat reminders vs. one-time heads-up/expired reminders) is
decided by ``is_primary_identity()``, a STRUCTURAL rule (is this identity's
credential directory HERMES_HOME itself, or nested under it, e.g.
``family_credentials/<name>/``?) — not a name comparison. A third or fourth
family member added to the registry later is automatically treated as
non-primary and gets the same one-time-reminder behavior as Zee, with zero
changes to this file.

Delivery-target resolution — flagged gap
-----------------------------------------
There is currently no code-level registry anywhere in this codebase mapping
a Hermes identity name to a per-platform delivery target (Telegram chat id,
etc.). ``telegram.allow_from`` in config.yaml is an unordered INBOUND
allowlist, not an identity-keyed outbound map; the actual jid/zarkash
Telegram IDs only exist as prose in vault ``Profile/*.md`` files, which this
script deliberately does not parse (fragile, and couples a git-tracked cron
script to vault layout). Rather than hardcode a jid/zarkash lookup table
here — which would need a code change for every future family member, the
exact thing this generality pass is meant to avoid — target resolution uses
an env-var convention consistent with how every other per-platform value in
this system is already supplied (``TELEGRAM_BOT_TOKEN``,
``TELEGRAM_HOME_CHANNEL``, ...):

    HERMES_IDENTITY_TELEGRAM_CHAT_ID__<IDENTITY, upper-cased>

e.g. ``HERMES_IDENTITY_TELEGRAM_CHAT_ID__JID=8758899353`` and
``HERMES_IDENTITY_TELEGRAM_CHAT_ID__ZARKASH=5542989100``. These do NOT exist
in ``~/.hermes/.env`` yet as of this PR — JID needs to add them as part of
live deployment (see the PR description). Onboarding a new family member
after that needs exactly one new env-var line and a Google identity
registry entry — zero code changes to this script. If a given identity's
env var is unset, this job SKIPS that identity's reminder and says so in
its own operational summary rather than guessing a chat id — the same
failure mode that caused the real 2026-08-12 cross-person data-disclosure
incident this system already has on record.

Part 2 / Part 3 coupling
-------------------------
The one-time-sent state file this job maintains
(``<HERMES_HOME>/cron/oauth_reauth_notify_state.json``) is also read AND
reset by gateway/run.py's oauth_reauth no-agent short-circuit handler, after
a successful ``--auth-code`` exchange for an identity — that reset starts a
fresh 7-day cycle's one-time flags clean. Keep this file's JSON shape
(``{"<identity>": {"last_known_recorded_at", "heads_up_sent_at",
"expired_sent_at"}}``) and path convention in sync with the constants
gateway/run.py uses (see the matching comment there).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

WARNING_WINDOW = timedelta(days=2)
REAUTH_VALIDITY = timedelta(days=7)

# Same name/location gateway/run.py's oauth_reauth short-circuit handler
# reads/writes — see the coupling note above and in that file.
STATE_FILE_NAME = "oauth_reauth_notify_state.json"


# ---------------------------------------------------------------------------
# Path resolution — everything HERMES_HOME-relative, matching how the
# deployed skill scripts are actually laid out on this host
# (~/.hermes/skills/productivity/google-workspace/scripts/), not the git
# checkout layout, so this works identically in production and in a
# --hermes-home-pointed test/dev sandbox.
# ---------------------------------------------------------------------------

def _google_scripts_dir(hermes_home: Path) -> Path:
    return hermes_home / "skills" / "productivity" / "google-workspace" / "scripts"


def _setup_py_path(hermes_home: Path) -> Path:
    return _google_scripts_dir(hermes_home) / "setup.py"


def load_identities_registry(hermes_home: Path) -> Dict[str, dict]:
    """Import the deployed ``_google_identities.py`` scoped to hermes_home
    and return its IDENTITIES registry.

    Dynamic and re-executed fresh every run (never cached across identities
    or across runs) so this job automatically covers every identity ever
    added to the registry, including ones added after this script was
    written, with zero changes here.
    """
    scripts_dir = _google_scripts_dir(hermes_home)
    path = scripts_dir / "_google_identities.py"
    os.environ["HERMES_HOME"] = str(hermes_home)
    scripts_dir_str = str(scripts_dir)
    if scripts_dir_str not in sys.path:
        sys.path.insert(0, scripts_dir_str)
    # _google_identities / _hermes_home compute HERMES_HOME once, at import
    # time, and cache in sys.modules under their bare names. Evict any stale
    # copy so a caller that points this job at a different --hermes-home
    # within the same process (tests; also defensive for any future
    # multi-profile use) always gets a module bound to the CURRENT
    # hermes_home, never a leftover from a previous call.
    for stale in ("_google_identities", "_hermes_home"):
        sys.modules.pop(stale, None)
    spec = importlib.util.spec_from_file_location(
        "_google_identities_oauth_expiry_check", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.IDENTITIES)


def is_primary_identity(entry: dict, hermes_home: Path) -> bool:
    """Structural rule: the primary/admin identity is the one whose
    credential directory IS HERMES_HOME itself. Every other (family-member)
    identity's directory is nested inside it (currently under
    ``family_credentials/<name>/``, per ``_google_identities.py``). No name
    comparison — a newly onboarded family member is automatically
    non-primary the moment they're added to IDENTITIES.
    """
    try:
        return Path(entry["credentials_dir"]).resolve() == hermes_home.resolve()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Re-auth sidecar / expiry estimation
# ---------------------------------------------------------------------------

def sidecar_path_for(entry: dict) -> Path:
    return Path(entry["credentials_dir"]) / "google_token_reauth_at.json"


def read_reauth_recorded_at(entry: dict) -> Optional[float]:
    """Return the sidecar's ``recorded_at_epoch``, or None when it doesn't
    exist / can't be read — either this identity predates this mechanism,
    or it has never completed a full re-auth since it was added.
    """
    path = sidecar_path_for(entry)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data["recorded_at_epoch"])
    except Exception:
        return None


def estimate_expiry(recorded_at_epoch: Optional[float]) -> Optional[datetime]:
    if recorded_at_epoch is None:
        return None
    return (
        datetime.fromtimestamp(recorded_at_epoch, tz=timezone.utc) + REAUTH_VALIDITY
    )


def in_warning_window(expiry: Optional[datetime], now: datetime) -> bool:
    """True once ``expiry`` is within (inclusive) or past the 2-day warning
    window. Stays True indefinitely past expiry — an unrenewed token does
    not leave the window by getting older."""
    if expiry is None:
        return False
    return (expiry - now) <= WARNING_WINDOW


# ---------------------------------------------------------------------------
# One-time-sent state (non-primary identities) + cycle-reset detection
# (all identities)
# ---------------------------------------------------------------------------

def _state_path(hermes_home: Path) -> Path:
    return hermes_home / "cron" / STATE_FILE_NAME


def load_state(hermes_home: Path) -> Dict[str, Any]:
    path = _state_path(hermes_home)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(hermes_home: Path, state: Dict[str, Any]) -> None:
    path = _state_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _blank_identity_state(recorded_at_epoch: Optional[float]) -> Dict[str, Any]:
    return {
        "last_known_recorded_at": recorded_at_epoch,
        "heads_up_sent_at": None,
        "expired_sent_at": None,
    }


def reset_identity_cycle(hermes_home: Path, identity: str) -> None:
    """Clear one identity's one-time sent-flags so the NEXT estimated-expiry
    cycle starts clean. Called by gateway/run.py's oauth_reauth short-circuit
    handler after a successful ``--auth-code`` exchange for ``identity`` —
    exposed as a plain function operating on the same state-file shape
    ``process_identity`` below reads/writes, so both sides stay in sync
    without gateway/run.py needing to import this script as a module.
    """
    state = load_state(hermes_home)
    # last_known_recorded_at is left None here deliberately: the NEXT run of
    # this job will read the identity's freshly-written sidecar timestamp
    # and detect the transition itself (None -> real epoch), logging the
    # cycle-reset the same way it would for any other advance. Setting it to
    # the new value here too would be equally correct but would duplicate
    # the sidecar as the source of truth; leaving it None keeps the sidecar
    # as the only place a "cycle" is actually defined.
    state[identity] = _blank_identity_state(None)
    save_state(hermes_home, state)


# ---------------------------------------------------------------------------
# setup.py subprocess helpers
# ---------------------------------------------------------------------------

def _run_setup(
    hermes_home: Path, identity: str, *args: str, timeout: int = 30
) -> "tuple[int, str]":
    """Run ``setup.py --identity <identity> <*args>`` under the SAME
    interpreter this script is running under (``sys.executable``) — the
    portable equivalent of a hardcoded venv path, matching how other
    subprocess calls into this venv resolve their interpreter elsewhere in
    this codebase (see gateway/run.py's ``_resolve_hermes_bin``).
    """
    setup_py = _setup_py_path(hermes_home)
    cmd = [sys.executable, str(setup_py), "--identity", identity, *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return 1, f"subprocess failed: {exc}"


def check_auth_live(hermes_home: Path, identity: str) -> bool:
    """Real ``--check`` call — used to verify for real once an identity is
    inside its estimated warning window, rather than trusting the estimate
    alone (the estimate can be wrong if Google's actual window differs)."""
    code, _ = _run_setup(hermes_home, identity, "--check")
    return code == 0


def fetch_fresh_auth_url(hermes_home: Path, identity: str) -> Optional[str]:
    """Generate a brand-new auth URL — never cache/reuse a stale one, since
    each reminder send must carry a URL still valid when the recipient opens
    it, potentially hours or days after this job ran."""
    code, output = _run_setup(hermes_home, identity, "--auth-url")
    if code != 0:
        return None
    for line in output.strip().splitlines():
        line = line.strip()
        if line.startswith("http"):
            return line
    return None


# ---------------------------------------------------------------------------
# Delivery-target resolution (see module docstring's flagged gap) + send
# ---------------------------------------------------------------------------

def resolve_telegram_chat_id(identity: str) -> Optional[str]:
    env_key = f"HERMES_IDENTITY_TELEGRAM_CHAT_ID__{identity.upper()}"
    value = (os.environ.get(env_key) or "").strip()
    return value or None


def send_telegram_message(chat_id: str, message: str) -> "tuple[bool, str]":
    """Direct send, reusing the EXISTING shared send tool — the same one
    ``hermes send`` (hermes_cli/send_cmd.py) and the agent's own
    ``send_message`` tool call both use. No new Telegram-sending path is
    invented here.
    """
    try:
        from tools.send_message_tool import send_message_tool
    except Exception as exc:
        return False, f"could not import send_message_tool: {exc}"
    try:
        result_json = send_message_tool(
            {"action": "send", "target": f"telegram:{chat_id}", "message": message}
        )
        result = json.loads(result_json)
    except Exception as exc:
        return False, f"send_message_tool raised: {exc}"
    if result.get("error"):
        return False, str(result["error"])
    return bool(result.get("success") or result.get("skipped")), json.dumps(result)


# ---------------------------------------------------------------------------
# Pending-approval staging (cron-approval-reply / ref-tag mechanism)
# ---------------------------------------------------------------------------

def stage_reminder(identity: str, *, kind: str, expiry: Optional[datetime]) -> str:
    from tools import write_approval as wa

    payload = {
        "action": "oauth_reauth",
        "identity": identity,
        "kind": kind,  # "daily_warning" | "expired" | "heads_up" | "reactive_daily" | "reactive_expired_once"
        "expected_code_format": (
            "Google OAuth authorization code — either the bare code, or the "
            "full redirect URL containing 'code=...'"
        ),
        "estimated_expiry": expiry.isoformat() if expiry else None,
    }
    record = wa.stage_write(
        "oauth_reauth",
        payload,
        summary=f"Google re-auth reminder sent to '{identity}' ({kind})",
        origin="cron:hermes-oauth-expiry-check",
    )
    return record["id"]


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------

_EXPIRED_KINDS = frozenset({"expired", "reactive_expired_once", "reactive_daily"})


def build_reminder_message(
    pending_id: str, auth_url: str, *, kind: str, expiry: Optional[datetime]
) -> str:
    ref_tag = f"[ref:oauth_reauth:{pending_id}]"
    if kind in _EXPIRED_KINDS:
        headline = (
            "Your Google account connection to Hermes has stopped working "
            "and needs to be renewed."
        )
    else:
        when = f" It's expected to stop working around {expiry.strftime('%Y-%m-%d')}." if expiry else ""
        headline = (
            "Your Google account connection to Hermes (calendar, email, "
            f"and drive access) is about to expire.{when}"
        )
    return (
        f"{ref_tag}\n\n"
        f"{headline}\n\n"
        "To fix it:\n"
        f"1. Open this link and sign in with Google: {auth_url}\n"
        "2. After signing in, you'll land on a page that doesn't load — "
        "that's expected. Copy the code from that page's web address (the "
        "part after \"code=\"), or just copy the whole address.\n"
        "3. Reply to THIS message (swipe/hold to reply) with the code from "
        "the redirect URL once you've completed the Google sign-in."
    )


# ---------------------------------------------------------------------------
# Per-identity evaluation
# ---------------------------------------------------------------------------

def _send_and_stage(
    identity: str,
    chat_id: str,
    hermes_home: Path,
    *,
    kind: str,
    expiry: Optional[datetime],
    logs: List[str],
    dry_run: bool,
) -> None:
    if dry_run:
        logs.append(f"{identity}: [dry-run] would send kind={kind}")
        return
    auth_url = fetch_fresh_auth_url(hermes_home, identity)
    if auth_url is None:
        logs.append(
            f"{identity}: could not generate a fresh auth URL (client secret "
            "missing or setup.py error) — skipping send this run"
        )
        return
    pending_id = stage_reminder(identity, kind=kind, expiry=expiry)
    message = build_reminder_message(pending_id, auth_url, kind=kind, expiry=expiry)
    ok, detail = send_telegram_message(chat_id, message)
    if ok:
        logs.append(f"{identity}: sent {kind} reminder (pending={pending_id})")
    else:
        logs.append(
            f"{identity}: FAILED to send {kind} reminder (pending={pending_id}): {detail}"
        )


def process_identity(
    identity: str,
    entry: dict,
    *,
    hermes_home: Path,
    now: datetime,
    state: Dict[str, Any],
    dry_run: bool = False,
) -> List[str]:
    """Evaluate and (unless dry_run) act on one identity's re-auth status.

    Mutates ``state[identity]`` in place. Returns human-readable log lines
    for this job's own operational summary (never a user-facing reminder —
    those are sent directly per-identity via ``send_telegram_message``, see
    module docstring for why cron's own single-target stdout delivery is
    wrong for this job).
    """
    logs: List[str] = []
    primary = is_primary_identity(entry, hermes_home)
    recorded_at_epoch = read_reauth_recorded_at(entry)
    expiry = estimate_expiry(recorded_at_epoch)

    identity_state = dict(
        state.get(identity) or _blank_identity_state(None)
    )

    # New re-auth cycle detection: whenever the sidecar's recorded_at has
    # advanced past what was last seen (including the very first time a
    # sidecar appears at all), reset the one-time flags. Applies uniformly
    # to every identity — primary has no one-time flags to reset, but this
    # keeps the state shape consistent and auditable across the board.
    if recorded_at_epoch != identity_state.get("last_known_recorded_at"):
        identity_state = _blank_identity_state(recorded_at_epoch)
        logs.append(f"{identity}: new re-auth cycle detected, one-time flags reset")

    chat_id = resolve_telegram_chat_id(identity)
    if chat_id is None:
        logs.append(
            f"{identity}: SKIPPED — no delivery target resolvable "
            f"(HERMES_IDENTITY_TELEGRAM_CHAT_ID__{identity.upper()} not set). "
            "Not guessing a chat id for this identity — flagged as a gap."
        )
        state[identity] = identity_state
        return logs

    if expiry is None:
        # Fallback: no sidecar recorded yet (identity predates this system,
        # or has never completed a full re-auth since). No proactive window
        # can be computed — purely reactive on --check's pass/fail.
        revoked = not check_auth_live(hermes_home, identity)
        if not revoked:
            logs.append(f"{identity}: fallback reactive mode, --check OK, nothing to do")
        elif primary:
            _send_and_stage(
                identity, chat_id, hermes_home,
                kind="reactive_daily", expiry=None, logs=logs, dry_run=dry_run,
            )
        elif identity_state.get("expired_sent_at") is None:
            _send_and_stage(
                identity, chat_id, hermes_home,
                kind="reactive_expired_once", expiry=None, logs=logs, dry_run=dry_run,
            )
            if not dry_run:
                identity_state["expired_sent_at"] = now.timestamp()
        else:
            logs.append(
                f"{identity}: fallback reactive mode, already sent one-time "
                "expired notice this cycle"
            )
        state[identity] = identity_state
        return logs

    if not in_warning_window(expiry, now):
        logs.append(
            f"{identity}: outside warning window (est. expiry "
            f"{expiry.isoformat()}), nothing to do"
        )
        state[identity] = identity_state
        return logs

    # Inside the window by estimate — verify for real rather than trusting
    # the estimate alone.
    revoked = not check_auth_live(hermes_home, identity)

    if primary:
        _send_and_stage(
            identity, chat_id, hermes_home,
            kind="expired" if revoked else "daily_warning",
            expiry=expiry, logs=logs, dry_run=dry_run,
        )
        # No one-time suppression for the primary identity — daily until the
        # sidecar advances (detected by the cycle-reset check above, which
        # fires once Part 3 records a fresh re-auth).
    elif revoked:
        if identity_state.get("expired_sent_at") is None:
            _send_and_stage(
                identity, chat_id, hermes_home,
                kind="expired", expiry=expiry, logs=logs, dry_run=dry_run,
            )
            if not dry_run:
                identity_state["expired_sent_at"] = now.timestamp()
        else:
            logs.append(f"{identity}: already sent one-time EXPIRED notice this cycle")
    elif identity_state.get("heads_up_sent_at") is None:
        _send_and_stage(
            identity, chat_id, hermes_home,
            kind="heads_up", expiry=expiry, logs=logs, dry_run=dry_run,
        )
        if not dry_run:
            identity_state["heads_up_sent_at"] = now.timestamp()
    else:
        logs.append(f"{identity}: already sent one-time heads-up this cycle")

    state[identity] = identity_state
    return logs


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--now", help="ISO timestamp override for deterministic tests")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Evaluate and log without sending messages, staging pending records, or writing state.",
    )
    return parser.parse_args()


def _parse_now(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def main() -> int:
    args = parse_args()
    hermes_home = args.hermes_home
    now = _parse_now(args.now)

    try:
        identities = load_identities_registry(hermes_home)
    except Exception as exc:
        print(
            "🩺 Operations Alert — hermes-oauth-expiry-check crashed loading "
            f"the identity registry: {type(exc).__name__}: {exc}"
        )
        return 0

    state = load_state(hermes_home)
    all_logs: List[str] = []
    for identity, entry in identities.items():
        try:
            all_logs.extend(
                process_identity(
                    identity, entry,
                    hermes_home=hermes_home, now=now, state=state,
                    dry_run=args.dry_run,
                )
            )
        except Exception as exc:
            all_logs.append(f"{identity}: CRASHED: {type(exc).__name__}: {exc}")

    if not args.dry_run:
        save_state(hermes_home, state)

    # This job's actual user-facing reminders are sent DIRECTLY per-identity
    # above via send_telegram_message — never through cron's own
    # single-target stdout delivery, which would either leak one person's
    # reminder into a different person's chat, or only ever reach whichever
    # one chat this job's own `deliver` field happens to be configured for.
    # stdout here is operational/debug only (identity names + outcome, never
    # message content, auth URLs, or codes) and follows the standard
    # no_agent convention: silent when there's nothing notable to report.
    notable = any(
        ("SKIPPED" in line or "FAILED" in line or "sent " in line or "CRASHED" in line)
        for line in all_logs
    )
    if notable:
        print("🩺 hermes-oauth-expiry-check run summary")
        for line in all_logs:
            print(f"- {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
