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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

REPO = Path(__file__).resolve().parents[1]
HERMES_HOME = REPO.parent
PROFILE = HERMES_HOME / "profiles" / "aletheon"
CONTRACT_SUITE = "tests/contract/test_protected_behavior.py"


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


def _downgrade_sqlite_to_advisory(src: str) -> str:
    """Upstream restores the original 'SQLite is advisory' behaviour."""
    return _sub(src, "    ok = python_ok and sqlite_ok", "    ok = python_ok")


def _unfix_crash_completion(src: str) -> str:
    """A merge drops the crash exclusion; crashes report completed again."""
    return _sub(src, "        and not _crashed" + chr(10), "")


def _unscrub_steer_markers(src: str) -> str:
    """Upstream drops the scrub; forged operator authority returns."""
    return _sub(src,
                "    wrapped = _scrub_steer_markers(_maybe_wrap_untrusted(name, content))",
                "    wrapped = _maybe_wrap_untrusted(name, content)")


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
    Scenario("downgrade-sqlite-to-advisory",
             "merge restores advisory SQLite; a supported runtime reopens WAL corruption",
             REPO / "hermes_cli" / "runtime_guard.py", _downgrade_sqlite_to_advisory),
    Scenario("unfix-crash-completion",
             "merge drops the crash exclusion; cron marks crashed jobs ok again",
             REPO / "agent" / "turn_finalizer.py", _unfix_crash_completion),
    Scenario("unscrub-steer-markers",
             "merge drops the scrub; tool output can forge operator authority again",
             REPO / "agent" / "tool_dispatch_helpers.py", _unscrub_steer_markers),
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
