"""Independent self-audit for Workspace Runtime release-blocker closure."""

from __future__ import annotations

import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
for path in (PLUGIN_PARENT, REPO_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import workspace_runtime as plugin
from workspace_runtime.discovery import VerdictState, discover


def reset() -> None:
    with plugin._lock:
        plugin._verdict_by_session.clear()
        plugin._context_by_session.clear()
        plugin._augmented_sessions.clear()


def check(label: str, condition: bool, detail: str = "") -> int:
    status = "PASS" if condition else "FAIL"
    print(f"{status}: {label}{' - ' + detail if detail else ''}")
    return 0 if condition else 1


def main() -> int:
    failures = 0
    canonical = Path("/home/taras/projects")

    reset()
    verdict = discover(canonical)
    original_discover = plugin._discovery.discover
    plugin._discovery.discover = lambda: verdict
    try:
        plugin.on_session_start(session_id="audit-canonical")
        delivered = plugin.pre_llm_call(
            session_id="audit-canonical", user_message="inspect", turn_id=0
        )
        context = delivered["context"] if delivered else ""
        failures += check(
            "F1/F4 verdict and canonical context delivered",
            '<workspace-runtime-verdict' in context
            and '<workspace-runtime-context' in context
            and "Production Engineering." in context
            and len(context) <= 10_000,
            f"chars={len(context)}",
        )
    finally:
        plugin._discovery.discover = original_discover

    reset()
    with tempfile.TemporaryDirectory() as td:
        bare = discover(Path(td))
        sequence = iter((verdict, bare))
        plugin._discovery.discover = lambda: next(sequence)
        original_builder = plugin._build_session_context
        plugin._build_session_context = plugin.render_verdict_block
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda sid: plugin.on_session_start(session_id=sid), ("A", "B")))
            contexts = {
                plugin.pre_llm_call(session_id=sid, user_message=sid, turn_id=0)["context"]
                for sid in ("A", "B")
            }
            failures += check(
                "F2 concurrent sessions remain isolated",
                any('inside_workspace' in c for c in contexts)
                and any('not_a_workspace' in c for c in contexts),
            )
        finally:
            plugin._discovery.discover = original_discover
            plugin._build_session_context = original_builder

    reset()
    calls = 0

    def counted():
        nonlocal calls
        calls += 1
        return verdict

    plugin._discovery.discover = counted
    try:
        plugin.on_session_start(session_id="stable")
        plugin.on_session_start(session_id="stable")
        failures += check("F3 repeated start is idempotent", calls == 1, f"calls={calls}")
    finally:
        plugin._discovery.discover = original_discover

    failures += check(
        "F5 mission continuation recovered",
        plugin._discovery._current_mission(
            canonical,
            canonical / ".project-state" / "workspace-runtime-release-blocker-remediation-2026-07-25",
            json.loads((canonical / "CONTEXT" / "workspace-index.json").read_text()),
        )
        == canonical / ".project-state" / "workspace-runtime-release-blocker-remediation-2026-07-25",
    )
    forbidden_override = "WORKSPACE" + "_OS_ROOT"
    failures += check(
        "F6 false root-override instruction removed",
        forbidden_override not in (Path(plugin._discovery.__file__).read_text()),
    )
    failures += check(
        "F7 failure fallback is stable",
        plugin._safe_cwd().is_absolute(),
    )
    tracked = __import__("subprocess").run(
        ["git", "ls-files", "plugins/workspace_runtime"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    failures += check("F9 release surface tracked", len(tracked) == 9, f"tracked={len(tracked)}")

    print(f"FAILURES: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
