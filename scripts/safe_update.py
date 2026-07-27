"""Rehearse an upstream update in isolation before it can touch production.

Upstream is 102 commits ahead and this fork carries 25 local commits, seven of
which patch upstream files directly. A previous merge in this repository
already destroyed work by resolving a conflict file-wise (`6ab037f1e`,
"restore upstream changes lost by file-level conflict resolution"), so the
update path needs to be rehearsed somewhere that cannot break the running
gateway.

Everything happens in a throwaway git worktree. Production is never touched,
nothing is pushed, and the rehearsal is judged by whether the *protections*
survive — not by whether the merge merely applied cleanly, because a clean
merge that silently drops a security fix is the failure mode being defended
against.

Steps
  1. refuse to start from a dirty tree (unless --allow-dirty)
  2. record the current version, local patch set and ledger state
  3. fetch upstream WITHOUT modifying the working tree
  4. create an isolated detached worktree at the current HEAD
  5. merge upstream there
  6. detect whole-file conflict resolutions on protected files
  7. run the protected-behavior contract suite
  8. run the regression harness (each protection must still be catchable)
  9. run targeted regression suites
 10. emit a report and, unless --keep, remove the rehearsal worktree

Exit 0 only when the merge applied AND every protection survived.

Usage:
    python scripts/safe_update.py                 # full rehearsal, then clean up
    python scripts/safe_update.py --keep          # leave the worktree for inspection
    python scripts/safe_update.py --no-fetch      # use the already-fetched ref
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UPSTREAM_REMOTE = "origin"
UPSTREAM_BRANCH = "main"

# Files carrying local patches an upstream merge could silently revert. Kept in
# step with PATCH_LEDGER.md's "merge-sensitive regions".
PROTECTED_FILES = [
    "run_agent.py",
    "hermes_cli/main.py",
    "hermes_cli/doctor.py",
    "acp_adapter/entry.py",
    "agent/tool_dispatch_helpers.py",
    "agent/turn_finalizer.py",
    "agent/system_prompt.py",
    "agent/turn_context.py",
    "tools/delegate_tool.py",
    "tools/daemon_pool.py",
    "gateway/worker_bridge_watchers.py",
    # Carries local auto-dispatch and failure-successor work (2484ec0a0,
    # 6cd47d16e, d4f3806a0) and is the ONE file that actually conflicted in the
    # 2026-07-27 rehearsal against 114 upstream commits.
    "gateway/run.py",
]

REGRESSION_SUITES = [
    "tests/contract/",
    "tests/hermes_cli/test_runtime_guard.py",
    "tests/agent/test_reasoning_status.py",
    "tests/agent/test_steer_marker_forgery.py",
    "tests/agent/test_turn_completion_honesty.py",
    "tests/tools/test_delegate_batch_cancellation.py",
    "tests/tools/test_daemon_pool.py",
    "tests/gateway/test_worker_bridge_alert_handoff.py",
]


def run(cmd, cwd=REPO, check=False, timeout=1800):
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{proc.stderr[-1500:]}")
    return proc


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the rehearsal worktree")
    ap.add_argument("--allow-dirty", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()

    report = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # 1 -------------------------------------------------------------------
    step(1, "checking the working tree is clean")
    dirty = [l for l in run(["git", "status", "--porcelain"]).stdout.splitlines()
             if l and not l.startswith("??")]
    if dirty and not args.allow_dirty:
        print(f"  REFUSED: {len(dirty)} modified file(s). Commit, stash, or pass "
              "--allow-dirty.")
        for line in dirty[:10]:
            print("   ", line)
        return 2
    print(f"  ok ({len(dirty)} modified, ignoring untracked)")

    # 2 -------------------------------------------------------------------
    step(2, "recording the current state")
    head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    upstream_ref = f"{UPSTREAM_REMOTE}/{UPSTREAM_BRANCH}"
    local_only = run(["git", "rev-list", "--count", f"{upstream_ref}..HEAD"]).stdout.strip()
    report.update({"head": head, "local_commits": local_only})
    print(f"  HEAD={head[:12]}  local-only commits={local_only}")

    ledger = run([sys.executable, "scripts/check_patch_ledger.py"])
    report["ledger_ok"] = ledger.returncode == 0
    print(f"  patch ledger: {'OK' if ledger.returncode == 0 else 'DRIFTED'}")
    if ledger.returncode != 0:
        print("  refusing to rehearse against a drifted ledger — record the "
              "missing patches first, or they are what gets lost.")
        return 2

    # 3 -------------------------------------------------------------------
    step(3, f"fetching {upstream_ref} (working tree untouched)")
    if args.no_fetch:
        print("  skipped (--no-fetch)")
    else:
        fetched = run(["git", "fetch", UPSTREAM_REMOTE, UPSTREAM_BRANCH], timeout=1800)
        if fetched.returncode != 0:
            print(f"  fetch failed: {fetched.stderr.strip()[:300]}")
            return 2
    behind = run(["git", "rev-list", "--count", f"HEAD..{upstream_ref}"]).stdout.strip()
    report["upstream_ahead"] = behind
    print(f"  upstream is {behind} commit(s) ahead")
    if behind == "0":
        print("  nothing to rehearse.")
        return 0

    # 4 -------------------------------------------------------------------
    step(4, "creating an isolated rehearsal worktree")
    wt = REPO.parent / f"hermes-update-rehearsal-{int(time.time())}"
    # --detach: a branch ref would trip the profile's reference-transaction gate.
    created = run(["git", "worktree", "add", "--detach", str(wt), "HEAD"], timeout=1800)
    if created.returncode != 0:
        print(f"  could not create worktree: {created.stderr.strip()[:400]}")
        return 2
    print(f"  {wt}")

    ok = False
    try:
        # 5 ---------------------------------------------------------------
        step(5, f"merging {upstream_ref} in the rehearsal worktree")
        merged = run(["git", "merge", "--no-edit", upstream_ref], cwd=wt, timeout=1800)
        conflicts = [l[3:] for l in run(["git", "status", "--porcelain"], cwd=wt).stdout.splitlines()
                     if l.startswith(("UU", "AA", "DU", "UD", "AU", "UA"))]
        report["merge_clean"] = merged.returncode == 0
        report["conflicts"] = conflicts
        if conflicts:
            print(f"  {len(conflicts)} conflict(s):")
            for c in conflicts:
                flag = "  <-- PROTECTED" if c in PROTECTED_FILES else ""
                print(f"    {c}{flag}")
            print("\n  Resolve by HUNK. Never `git checkout --ours/--theirs` a "
                  "protected file — that is how 6ab037f1e happened.")
        else:
            print(f"  merged cleanly ({merged.returncode})")

        # 6 ---------------------------------------------------------------
        step(6, "checking protected files still carry their local patches")
        lost = []
        for rel in PROTECTED_FILES:
            before = run(["git", "show", f"{head}:{rel}"]).stdout
            after_path = wt / rel
            if not after_path.exists():
                lost.append(f"{rel} (file gone)")
                continue
            # Cheap, high-signal: a protected file that came back byte-identical
            # to upstream lost every local hunk.
            upstream_ver = run(["git", "show", f"{upstream_ref}:{rel}"]).stdout
            after = after_path.read_text(encoding="utf-8", errors="replace")
            if before != after and after == upstream_ver:
                lost.append(f"{rel} (reverted to upstream verbatim)")
        report["protected_lost"] = lost
        if lost:
            print("  LOCAL PATCHES LOST:")
            for entry in lost:
                print(f"    {entry}")
        else:
            print(f"  ok — {len(PROTECTED_FILES)} protected files retained local content")

        # 7 / 8 / 9 -------------------------------------------------------
        if conflicts:
            print("\n[7-9] skipped — resolve conflicts, then re-run in the worktree:")
            print(f"        cd {wt} && python scripts/verify_protected_behavior.py")
            report["verified"] = False
        else:
            step(7, "contract suite (protections must hold post-merge)")
            contract = run([sys.executable, "-m", "pytest", "tests/contract/",
                            "-q", "-p", "no:cacheprovider"], cwd=wt, timeout=1800)
            print("  " + (contract.stdout.strip().splitlines() or ["(no output)"])[-1])
            report["contract_ok"] = contract.returncode == 0

            step(8, "regression harness (each protection still catchable)")
            harness = run([sys.executable, "scripts/verify_protected_behavior.py"],
                          cwd=wt, timeout=3600)
            tail = [l for l in harness.stdout.splitlines() if "caught" in l]
            print("  " + (tail[-1] if tail else "(no output)"))
            report["harness_ok"] = harness.returncode == 0

            step(9, "targeted regression suites")
            suites = run([sys.executable, "-m", "pytest", *REGRESSION_SUITES,
                          "-q", "-p", "no:cacheprovider"], cwd=wt, timeout=3600)
            print("  " + (suites.stdout.strip().splitlines() or ["(no output)"])[-1])
            report["suites_ok"] = suites.returncode == 0

            report["verified"] = all([
                report.get("contract_ok"), report.get("harness_ok"),
                report.get("suites_ok"),
            ])
            ok = bool(report["verified"]) and not lost

        # 10 --------------------------------------------------------------
        step(10, "report")
        out = REPO / "update-rehearsal-report.json"
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"  {out.name}")
        print(f"\nRESULT: {'SAFE TO APPLY' if ok else 'DO NOT APPLY — review above'}")
        if ok:
            print("Apply for real with:")
            print(f"  git merge {upstream_ref}          # in {REPO}")
            print("Rollback:")
            print(f"  git reset --hard {head[:12]}      # pre-merge HEAD")
    finally:
        if args.keep:
            print(f"\nrehearsal worktree kept: {wt}")
        else:
            run(["git", "worktree", "remove", "--force", str(wt)], timeout=600)
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
            run(["git", "worktree", "prune"])
            print("\nrehearsal worktree removed")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
