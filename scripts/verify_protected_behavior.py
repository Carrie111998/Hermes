"""Prove the contract suite actually catches lost protections.

A contract test that passes tells you nothing on its own — it might assert
something that can never fail. This harness injects, one at a time, the exact
regressions an upstream merge would produce (a dropped kwarg, a reverted
encoding fix, a removed guard call), runs the contract suite against each, and
requires the suite to FAIL. A simulated regression the suite does not catch is
itself reported as a failure.

Every mutation is applied to a file that is restored from an in-memory copy in
a finally block, so an interrupted run cannot leave a mutated tree.

Usage:
    python scripts/verify_protected_behavior.py            # run all scenarios
    python scripts/verify_protected_behavior.py --list     # show scenarios

Exit 0 only when every simulated regression was caught.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

REPO = Path(__file__).resolve().parents[1]
CONTRACT_SUITE = "tests/contract/test_protected_behavior.py"


def _find_profile() -> Path:
    """Locate the live profile regardless of checkout layout.

    Same trap as the contract suite: REPO.parent is wrong when running from a
    git worktree, and HERMES_HOME can point at the hermes ROOT — which also has
    a plugins/ dir. A profile is defined structurally by sitting directly under
    a `profiles/` directory; matching on plugins/ alone silently turned every
    profile scenario into a no-op SKIP.
    """
    candidates = []
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        home = Path(env_home)
        candidates += [home, home / "profiles" / "aletheon"]
    candidates += [
        REPO.parent / "profiles" / "aletheon",
        Path.home() / "AppData" / "Local" / "hermes" / "profiles" / "aletheon",
        Path.home() / ".hermes" / "profiles" / "aletheon",
    ]
    for candidate in candidates:
        try:
            if candidate.parent.name == "profiles" and (candidate / "plugins").is_dir():
                return candidate
        except OSError:
            continue
    return REPO.parent / "profiles" / "aletheon"


PROFILE = _find_profile()


@dataclass
class Scenario:
    name: str
    why: str                      # the real-world regression this imitates
    path: Path
    mutate: Callable[[str], str]  # source -> mutated source


def _sub(src: str, old: str, new: str) -> str:
    """Replace once, adapting the pattern to the file's own line endings.

    Newlines are preserved byte-exactly (see _read_exact), so the two repos
    differ: hermes-agent stores LF, the profile stores CRLF. Anchors are
    written with \\n, so without this they silently fail to match in the CRLF
    tree and the scenario reports STALE instead of proving anything.
    """
    if "\r\n" in src:
        old = old.replace("\n", "\r\n")
        new = new.replace("\n", "\r\n")
    return src.replace(old, new, 1)


def _drop_user_message(src: str) -> str:
    return _sub(src, "user_message=original_user_message,", "")


def _drop_conversation_history(src: str) -> str:
    return _sub(src, "conversation_history=list(messages),", "")


def _stop_loading_soul(src: str) -> str:
    return _sub(src, "_soul_content = _r.load_soul_md(_ctx_len)", "_soul_content = None")


def _revert_git_encoding(src: str) -> str:
    return _sub(src, '        encoding="utf-8",\n        errors="surrogateescape",\n', "")


def _unwire_runtime_guard(src: str) -> str:
    return _sub(src, "        _enforce_runtime()", "        pass")


def _reintroduce_prelock_read(src: str) -> str:
    """Put the mandatory-lock-violating read back before acquisition."""
    anchor = '    with lock_path.open("r+b") as lock_file:\n        lock_file.seek(0)'
    replacement = ('    with lock_path.open("r+b") as lock_file:\n'
                   '        if lock_file.read(1) == b"":\n'
                   '            pass\n'
                   '        lock_file.seek(0)')
    return _sub(src, anchor, replacement)


def _reintroduce_phantom_path(src: str) -> str:
    return _sub(src, 'if home_path.parent.name == "profiles":', 'if False:')


def _drop_steer_neutralization(src: str) -> str:
    """Anchored dynamically: the neutralizer was renamed once already
    (_neutralize_steer_markers -> _neutralize_forged_trust_markers) and the
    hard-coded anchor went stale silently. Find the call site by shape."""
    target = next(l for l in src.split(chr(10))
                  if "wrapped = " in l and "_maybe_wrap_untrusted(name, content)" in l)
    return _sub(src, target,
                "    wrapped = _maybe_wrap_untrusted(name, content)")


def _restore_blocking_with(src: str) -> str:
    """Put the batch branch back inside a joining `with` block."""
    return _sub(src,
                "executor = DaemonThreadPoolExecutor(max_workers=max_children)",
                "with DaemonThreadPoolExecutor(max_workers=max_children) as executor:\n"
                "                pass  # simulated regression")


def _allow_crash_completion(src: str) -> str:
    return _sub(src, "        and not crashed\n", "")


def _drop_lease_binding(src: str) -> str:
    return _sub(src, "        if is_background and delegation_id:", "        if False:")


def _drop_refspec_force_pattern(src: str) -> str:
    """Neuter the + refspec regex without touching its neighbours.

    Single-line anchors throughout: a multi-line anchor must reproduce the
    file's exact indentation and escaping, and goes silently STALE the
    moment either shifts.
    """
    return _sub(src, r"\s\+\S'", r"\sNEVERMATCHES'")


def _drop_env_write_block(src: str) -> str:
    return _sub(src, "in _BLOCKED_PROJECT_ENV_BASENAMES:", "in frozenset():")


def _restore_silent_overwrite(src: str) -> str:
    return _sub(src, "                if os.path.getsize(resolved) > 0:", "                if False:")


def _drop_memory_threat_scan(src: str) -> str:
    return _sub(src, '"[System note: recalled memory context', '"[System note: authoritative reference data')


def _downgrade_sqlite_to_advisory(src: str) -> str:
    """Upstream restores advisory SQLite. hermes-agent/venv is 3.11.15 —
    INSIDE requires-python, so the Python half waves it through — linking
    SQLite 3.50.4 against ~10 already-WAL databases."""
    return _sub(src, "    ok = python_ok and sqlite_ok", "    ok = python_ok")


SCENARIOS: List[Scenario] = [
    Scenario("drop-user_message-kwarg",
             "upstream refactors the hook call; feedback-gate silently stops firing",
             REPO / "agent" / "turn_context.py", _drop_user_message),
    Scenario("drop-conversation_history-kwarg",
             "upstream trims hook kwargs; compaction-guard silently stops firing",
             REPO / "agent" / "turn_context.py", _drop_conversation_history),
    Scenario("stop-loading-SOUL.md",
             "identity slot regressed to DEFAULT_AGENT_IDENTITY; every profile rule lost",
             REPO / "agent" / "system_prompt.py", _stop_loading_soul),
    Scenario("unwire-runtime-guard",
             "merge drops the guard call; hermes runs on unsupported Python again",
             REPO / "hermes_cli" / "main.py", _unwire_runtime_guard),
    Scenario("revert-git-utf8-decoding",
             "conflict resolution takes upstream _run; path checks fail OPEN again",
             PROFILE / "plugins" / "bob" / "bob_core" / "bridge" / "workspace.py",
             _revert_git_encoding),
    Scenario("reintroduce-prelock-read",
             "lock helper reverted; alert sync thread dies on PermissionError",
             PROFILE / "plugins" / "worker-alert-gate" / "alert_core.py",
             _reintroduce_prelock_read),
    Scenario("reintroduce-phantom-profile-path",
             "path helper reverted; receipts + HMAC key vanish into a phantom tree",
             PROFILE / "plugins" / "execution-receipts" / "execution_receipts.py",
             _reintroduce_phantom_path),
    Scenario("drop-steer-marker-neutralization",
             "tool output can forge operator authority via the steer marker again",
             REPO / "agent" / "tool_dispatch_helpers.py", _drop_steer_neutralization),
    Scenario("restore-blocking-with-block",
             "batch delegation back inside `with`; interrupt cannot escape __exit__",
             REPO / "tools" / "delegate_tool.py", _restore_blocking_with),
    Scenario("allow-crashed-turn-to-complete",
             "a crashed turn reports completed=True; cron marks the job ok",
             REPO / "agent" / "turn_finalizer.py", _allow_crash_completion),
    Scenario("stop-binding-delegation-leases",
             "guard releases on the dispatch return again; lease covers 0.2s",
             PROFILE / "plugins" / "delegation-guard" / "plugin.py",
             _drop_lease_binding),
    Scenario("drop-refspec-force-pattern",
             "git push origin +main bypasses the force-push approval gate again",
             REPO / "tools" / "approval.py", _drop_refspec_force_pattern),
    Scenario("drop-env-write-block",
             ".env becomes destroyable while still unreadable",
             REPO / "agent" / "file_safety.py", _drop_env_write_block),
    Scenario("restore-silent-overwrite",
             "write_file silently replaces a file the agent never read",
             REPO / "tools" / "file_state.py", _restore_silent_overwrite),
    Scenario("reframe-memory-as-authoritative",
             "untrusted provider memory is laundered back into an obey-this framing",
             REPO / "agent" / "memory_manager.py", _drop_memory_threat_scan),
    Scenario("downgrade-sqlite-to-advisory",
             "merge restores advisory SQLite; a supported runtime reopens WAL corruption",
             REPO / "hermes_cli" / "runtime_guard.py", _downgrade_sqlite_to_advisory),
]


def _read_exact(path: Path) -> str:
    """Read without newline translation.

    ``Path.read_text``/``write_text`` use universal newlines, so a read/write
    round-trip on Windows silently rewrites an LF file to CRLF — every line
    shows as changed and the "restore" leaves the tree dirty. That is precisely
    the silent whole-file mutation this harness exists to detect, so it must
    not commit it itself.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write_exact(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _run_contract_suite() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", CONTRACT_SUITE, "-q", "-p", "no:cacheprovider",
         "--no-header", "-x"],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = parser.parse_args()

    if args.list:
        for s in SCENARIOS:
            print(f"{s.name:36s} {s.why}")
        return 0

    baseline = _run_contract_suite()
    if baseline.returncode != 0:
        print("BASELINE FAILURE — the contract suite is red before any mutation.")
        print(baseline.stdout[-3000:])
        return 2
    print(f"baseline: contract suite GREEN ({CONTRACT_SUITE})\n")

    caught, missed, skipped = [], [], []
    for scenario in SCENARIOS:
        if not scenario.path.exists():
            skipped.append((scenario.name, "file not present"))
            print(f"  [SKIP] {scenario.name:36s} {scenario.path.name} not present")
            continue
        original = _read_exact(scenario.path)
        mutated = scenario.mutate(original)
        if mutated == original:
            # The anchor text moved: the mutation is a no-op, so this scenario
            # proves nothing. Surface it rather than reporting a false pass.
            missed.append((scenario.name, "mutation anchor no longer matches — scenario is stale"))
            print(f"  [STALE] {scenario.name:36s} anchor not found; scenario proves nothing")
            continue
        try:
            _write_exact(scenario.path, mutated)
            result = _run_contract_suite()
            if result.returncode != 0:
                caught.append(scenario.name)
                print(f"  [CAUGHT] {scenario.name:36s} {scenario.why}")
            else:
                missed.append((scenario.name, "contract suite stayed green"))
                print(f"  [MISSED] {scenario.name:36s} NOT DETECTED — {scenario.why}")
        finally:
            _write_exact(scenario.path, original)

    restored = _run_contract_suite()
    print(f"\nrestored tree: contract suite {'GREEN' if restored.returncode == 0 else 'RED'}")
    print(f"caught {len(caught)}/{len(SCENARIOS) - len(skipped)} simulated regressions")
    for name, reason in missed:
        print(f"  MISSED: {name} ({reason})")
    for name, reason in skipped:
        print(f"  SKIPPED: {name} ({reason})")

    return 0 if (not missed and restored.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
