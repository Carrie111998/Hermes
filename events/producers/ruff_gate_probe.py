"""RuffGateProbe — emits DEVFLOW_BUILD_FAILED when agent-src's ruff gate is red.

EXTERNAL PRODUCER. Runs as a Windows Scheduled Task every 15 minutes, NOT in
the gateway poll loop. Do NOT register this in
``events.gateway_integration.startup()``: the whole point is that it survives
a working tree broken badly enough to take the gateway down with it.

Why this exists
---------------
The ruff F group is enforced tree-wide on agent-src with zero suppressions,
but on 2026-08-17 a break still reached ``main``. Measured with a sentinel
hook, the pre-commit gate fires for a plain ``git commit`` ONLY — ``git
rebase`` replay, ``git cherry-pick`` and merge commits fire it ZERO times. Of
the last 300 first-parent commits on main, 200 were plain (gated), 76 replayed
and 24 were merges: one in three commits reaching main never runs the gate.
A fourth hole: the hook uses ``pass_filenames=true``, so even when it fires it
lints only STAGED files and cannot see a whole-tree break.

``.github/workflows/lint.yml``'s blocking job does not close this. Local main
is on zero remote refs and is ~18k commits ahead of the upstream fork, and the
workflow triggers only on pull_request / push:[main], so it provably never runs
for this checkout. Pushing is not an acceptable remedy here.

So the backstop is a clock, not a hook: lint the WHOLE tree on a schedule and
route a red gate through the existing notification layer.

Event type
----------
Reuses ``DEVFLOW_BUILD_FAILED`` rather than minting a new member. A lint gate
IS a build check, the type already routes WARN -> ``watchdog_alerts``, and the
WhatsApp escalator's existing branch renders exactly the fields this payload
carries (``build_name`` / ``repo`` / ``branch`` / ``error_summary``). Minting a
member would mean a fresh three-way pairing across schema.py,
EVENT_TYPE_EMOJI and routing_policy._POLICY — a pairing that has broken four
times on record — to buy nothing this payload cannot already say.

There is deliberately NO falling-edge "recovered" event. The only fitting type
(``DEVFLOW_BUILD_SUCCEEDED``) is TRACE, which the notifier batches hourly into
``devflow_firehose`` — an all-clear arriving up to an hour late in a different
topic than the alarm is worse than none. The falling edge still clears episode
state, so the NEXT break re-alerts.

Read-only contract
------------------
agent-src is hot: many concurrent sessions, main moves constantly, foreign
staged and dirty files are normal. This probe therefore never writes to the
repo. ``ruff check`` runs without ``--fix``; ``--no-cache`` keeps it from
writing ``.ruff_cache``; the only git calls are ``rev-parse``. All state lands
under ``~/.hermes/notifications/``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from events.bus import EventBus
from events.paths import ruff_gate_state_path
from events.schema import EventType, Priority
from events.state import load_state, save_state

logger = logging.getLogger(__name__)

_AGENT_SRC_DEFAULT = Path.home() / ".hermes" / "agent-src"

BUILD_NAME = "ruff F-group gate"
SOURCE = "ruff-gate-probe"

# Re-ping a sustained red gate this often even when the violation set has not
# changed, so a break that nobody fixes does not fall silent forever. Matches
# CodeDriftMonitor's cooldown.
DEFAULT_RE_ALERT_COOLDOWN_SECONDS = 6 * 3600.0

# Ruff on a whole tree is ~2s warm; the ceiling only exists so a wedged
# subprocess cannot pin a Scheduled Task slot until the next tick.
DEFAULT_RUFF_TIMEOUT_SECONDS = 180.0
GIT_TIMEOUT_SECONDS = 15.0

SAMPLE_CAP = 5

# Files/dirs whose presence means a rebase, merge or cherry-pick is mid-flight
# in this checkout. Linting then measures a half-applied tree and would blame
# whichever session happens to be replaying commits. Skip without touching
# state: the next tick lands on a settled tree.
_IN_PROGRESS_MARKERS = (
    "MERGE_HEAD",
    "REBASE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
)


def _agent_src_root() -> Path:
    """Mirrors code_drift_monitor._agent_src_root so both probes agree on the
    watched checkout and both stay redirectable under test."""
    return Path(os.getenv("HERMES_AGENT_SRC") or _AGENT_SRC_DEFAULT)


@dataclass
class RuffSample:
    """One measurement of the gate. ``ok`` False means the probe could not
    measure — distinct from a measured-clean tree, and never an alert."""

    ok: bool
    red: bool = False
    violations: int = 0
    codes: Dict[str, int] = field(default_factory=dict)
    sample: List[str] = field(default_factory=list)
    detail: str = ""


def _git_dir(repo: Path) -> Optional[Path]:
    """Resolve the real .git directory.

    A worktree's ``.git`` is a FILE pointing at the real gitdir, so a plain
    ``(repo / ".git" / "MERGE_HEAD").exists()`` would silently never fire
    there. Ask git instead of guessing.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=str(repo), capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    path = out.stdout.strip()
    return Path(path) if path else None


def operation_in_progress(repo: Path) -> bool:
    """True when a merge/rebase/cherry-pick/revert is mid-flight."""
    git_dir = _git_dir(repo)
    if git_dir is None:
        return False
    return any((git_dir / marker).exists() for marker in _IN_PROGRESS_MARKERS)


def _git_line(repo: Path, args: Sequence[str]) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def describe_checkout(repo: Path) -> Dict[str, str]:
    """Branch + short HEAD, for the payload.

    The agent-src checkout is deliberately parked on a detached HEAD much of
    the time so worktree agents can land onto the ``main`` ref, so an empty
    branch name is normal rather than an error.
    """
    branch = _git_line(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    commit = _git_line(repo, ["rev-parse", "--short", "HEAD"])
    if branch == "HEAD":
        branch = f"detached@{commit}" if commit else "detached"
    return {"branch": branch or "?", "commit": commit or "?"}


def run_ruff(repo: Path, timeout: float = DEFAULT_RUFF_TIMEOUT_SECONDS) -> RuffSample:
    """Lint the whole tree. Never writes to the repo.

    Exit 0 = clean, 1 = violations (syntax errors land here too, as
    ``invalid-syntax`` diagnostics), >=2 = ruff itself failed. That last case
    is a BROKEN PROBE, not a clean gate: report it as unmeasurable so a
    mangled ruff.toml can never read as "all checks passed".
    """
    cmd = [
        sys.executable, "-m", "ruff", "check", "--no-cache",
        "--output-format", "json", ".",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(repo), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RuffSample(ok=False, detail=f"ruff timed out after {timeout:.0f}s")
    except OSError as exc:
        return RuffSample(ok=False, detail=f"could not run ruff: {exc}")

    if proc.returncode == 0:
        return RuffSample(ok=True, red=False)
    if proc.returncode != 1:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return RuffSample(
            ok=False,
            detail=f"ruff exited {proc.returncode}: {detail[0] if detail else 'no output'}",
        )

    try:
        findings = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        return RuffSample(ok=False, detail=f"ruff JSON unparseable: {exc}")
    if not isinstance(findings, list):
        return RuffSample(ok=False, detail="ruff JSON was not a list")

    codes = Counter()
    sample: List[str] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "invalid-syntax")
        codes[code] += 1
        if len(sample) < SAMPLE_CAP:
            filename = str(item.get("filename") or "?")
            try:
                filename = str(Path(filename).relative_to(repo))
            except ValueError:
                pass
            row = (item.get("location") or {}).get("row", "?")
            message = str(item.get("message") or "").strip()
            sample.append(f"{filename}:{row}: {code} {message}"[:200])

    return RuffSample(
        ok=True, red=True, violations=len(findings),
        codes=dict(sorted(codes.items())), sample=sample,
    )


def _shape(sample: RuffSample) -> str:
    """Identity of a red episode.

    Keyed on the rule codes and their counts, NOT on file/line: a session
    reformatting unrelated code shifts every line number, and re-alerting on
    that would defeat the debounce this whole module exists to provide.
    """
    return ",".join(f"{code}={count}" for code, count in sorted(sample.codes.items()))


def _summary(sample: RuffSample) -> str:
    breakdown = ", ".join(
        f"{code}x{count}" for code, count in sorted(sample.codes.items())
    )
    plural = "" if sample.violations == 1 else "s"
    return f"{sample.violations} ruff violation{plural} ({breakdown})" if breakdown \
        else f"{sample.violations} ruff violation{plural}"


class RuffGateProbe:
    """Edge-triggered wrapper around :func:`run_ruff`."""

    def __init__(
        self,
        bus: Optional[EventBus] = None,
        repo_path: Optional[Path] = None,
        state_path: Optional[Path] = None,
        re_alert_cooldown_seconds: float = DEFAULT_RE_ALERT_COOLDOWN_SECONDS,
        ruff_timeout_seconds: float = DEFAULT_RUFF_TIMEOUT_SECONDS,
    ) -> None:
        self.bus = bus if bus is not None else EventBus()
        self._repo = Path(repo_path) if repo_path else _agent_src_root()
        self._state_path = Path(state_path) if state_path else ruff_gate_state_path()
        self.re_alert_cooldown_seconds = re_alert_cooldown_seconds
        self.ruff_timeout_seconds = ruff_timeout_seconds

        state = load_state(self._state_path, {})
        self._alerting = bool(state.get("alerting", False))
        self._last_emit = float(state.get("last_emit_wall") or 0.0)
        self._last_shape = str(state.get("last_shape") or "")

    def _save(self) -> None:
        try:
            save_state(self._state_path, {
                "alerting": self._alerting,
                "last_emit_wall": self._last_emit,
                "last_shape": self._last_shape,
            })
        except Exception:  # pragma: no cover - defensive
            logger.exception("RuffGateProbe: state persist failed")

    def check(self, now: Optional[float] = None) -> Optional[str]:
        """One tick. Returns the emitted event_id, or None."""
        now = time.time() if now is None else now

        if not self._repo.exists():
            logger.info("RuffGateProbe: %s does not exist — skipping", self._repo)
            return None

        if operation_in_progress(self._repo):
            # Deliberately no state change: a half-applied tree is not
            # evidence the gate went green OR red.
            logger.info(
                "RuffGateProbe: merge/rebase/cherry-pick in progress in %s — skipping",
                self._repo,
            )
            return None

        sample = run_ruff(self._repo, timeout=self.ruff_timeout_seconds)

        if not sample.ok:
            # Unmeasurable. Leave episode state alone so this neither
            # fabricates a recovery nor re-alerts on the way back.
            logger.warning("RuffGateProbe: %s", sample.detail)
            return None

        if not sample.red:
            if self._alerting:
                logger.info("RuffGateProbe: gate is green again — episode cleared")
                self._alerting = False
                self._last_shape = ""
                self._save()
            return None

        shape = _shape(sample)
        rising_edge = not self._alerting
        shape_changed = shape != self._last_shape
        cooldown_elapsed = (
            self.re_alert_cooldown_seconds > 0
            and (now - self._last_emit) >= self.re_alert_cooldown_seconds
        )
        self._alerting = True

        if not (rising_edge or shape_changed or cooldown_elapsed):
            # Same break, already reported, cooldown not elapsed. This branch
            # is the reason the probe can run every 15 minutes.
            self._save()
            return None

        self._last_emit = now
        self._last_shape = shape
        self._save()
        return self._emit(sample)

    def _emit(self, sample: RuffSample) -> str:
        identity = describe_checkout(self._repo)
        summary = _summary(sample)
        logger.warning(
            "Ruff gate RED in %s on %s: %s", self._repo, identity["branch"], summary,
        )
        return self.bus.emit(
            event_type=EventType.DEVFLOW_BUILD_FAILED,
            source=SOURCE,
            payload={
                # Keys the WhatsApp escalator's build_failed branch renders.
                "build_name": BUILD_NAME,
                "repo": self._repo.name,
                "branch": identity["branch"],
                "error_summary": summary,
                # Detail for the Telegram body and for postmortems.
                "repo_path": str(self._repo),
                "commit": identity["commit"],
                "violations": sample.violations,
                "codes": sample.codes,
                "sample": sample.sample,
                "gate": "ruff",
            },
            priority=Priority.HIGH,
            tags=["ruff", "lint", "gate"],
        )


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    import argparse

    parser = argparse.ArgumentParser(prog="events.producers.ruff_gate_probe")
    parser.add_argument("--repo", default=None, help="Checkout to lint")
    parser.add_argument("--state", default=None, help="Episode state file")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Measure and report, but never emit or persist state",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo) if args.repo else _agent_src_root()

    if args.dry_run:
        if operation_in_progress(repo):
            print("skipped: merge/rebase/cherry-pick in progress")
            return 0
        sample = run_ruff(repo)
        if not sample.ok:
            print(f"unmeasurable: {sample.detail}")
            return 2
        print("red: " + _summary(sample) if sample.red else "green")
        for line in sample.sample:
            print(f"  {line}")
        return 1 if sample.red else 0

    probe = RuffGateProbe(repo_path=repo, state_path=args.state)
    event_id = probe.check()
    if event_id:
        print(event_id)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
