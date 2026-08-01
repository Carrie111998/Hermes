from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
ASSET = ROOT / "session_bridge" / "assets" / "session-sidebar-sync"
BASELINE = Path(__file__).parent / "fixtures" / "sidebar_skill_baseline.txt"
LOCK_NAME = ".session-sidebar-sync.install.lock"
BACKUP_ROOT_NAME = ".session-bridge-skill-backups"


@pytest.mark.parametrize("relative", ("a:b", "D:/escape.txt", "D:escape.txt"))
def test_sidebar_installer_rejects_windows_drive_relative_manifest_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    from session_bridge import sidebar_skill
    from session_bridge.asset_installer import AssetInstallSpec

    monkeypatch.setattr(
        sidebar_skill,
        "_INSTALL_SPEC",
        AssetInstallSpec(
            asset_name="session-sidebar-sync",
            destination_name="session-sidebar-sync",
            files=(relative,),
            staging_marker_content=b"test\n",
            error_label="sidebar skill",
        ),
    )

    with pytest.raises(ValueError, match="asset file path"):
        sidebar_skill.install_sidebar_skill(tmp_path / "codex")

    assert not (tmp_path / "codex" / "skills").exists()


def _installed_files(path: Path) -> dict[str, bytes]:
    return {
        str(file.relative_to(path)).replace("\\", "/"): file.read_bytes()
        for file in path.rglob("*")
        if file.is_file()
    }


def _content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for relative, content in sorted(_installed_files(path).items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _subprocess_environment() -> dict[str, str]:
    environment = dict(os.environ)
    prior = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(ROOT) if not prior else str(ROOT) + os.pathsep + prior
    )
    return environment


def _start_python(code: str, *arguments: Path | str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", code, *(str(argument) for argument in arguments)],
        cwd=ROOT,
        env=_subprocess_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _run_python(
    code: str,
    *arguments: Path | str,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *(str(argument) for argument in arguments)],
        cwd=ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _wait_for_path(path: Path, process: subprocess.Popen[str]) -> None:
    # Windows process startup can exceed five seconds when the parallel suite is
    # launching many isolated interpreters. Keep enough headroom for scheduler
    # contention while remaining below pytest's per-test timeout.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                f"lock holder exited before readiness: {process.returncode}; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            )
        time.sleep(0.02)
    process.kill()
    stdout, stderr = process.communicate()
    pytest.fail(f"lock holder readiness timeout: stdout={stdout!r}; stderr={stderr!r}")


def _create_directory_redirect(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            raise
    completed = subprocess.run(  # noqa: S603
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"directory redirects are unavailable: {completed.stderr}")


def _remove_directory_redirect(link: Path) -> None:
    if os.name == "nt" and link.is_dir() and not link.is_symlink():
        os.rmdir(link)
    else:
        link.unlink()


def test_sidebar_skill_matches_the_single_reviewed_baseline() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    baseline = BASELINE.read_text(encoding="utf-8")

    assert baseline == skill


def test_sidebar_skill_contains_only_the_generated_skill_and_agent_metadata() -> None:
    files = {
        str(path.relative_to(ASSET)).replace("\\", "/")
        for path in ASSET.rglob("*")
        if path.is_file()
    }

    assert files == {"SKILL.md", "agents/openai.yaml"}


def test_sidebar_skill_metadata_matches_the_personal_codex_contract() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    metadata = (ASSET / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert skill.startswith("---\nname: session-sidebar-sync\ndescription: Use when ")
    assert "\n---\n" in skill
    assert "TODO" not in skill
    assert metadata == (
        "interface:\n"
        '  display_name: "Session Sidebar Sync"\n'
        '  short_description: "Deliver leased Claude and Hermes sessions to the Codex sidebar"\n'
        '  default_prompt: "Run $session-sidebar-sync once and end quietly when no work is pending."\n'
    )


def test_sidebar_skill_encodes_the_single_lease_sequential_delivery_protocol() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "session_sidebar_pending(limit=1)" in skill
    assert "session_sidebar_hydration_pending(limit=1)" in skill
    assert "at most one job per wake" in skill
    assert "Process that one lease sequentially to completion" in skill
    assert "Never run `create_thread` concurrently" in skill
    assert "never run native delivery operations concurrently" in skill
    assert "Do not claim or process another lease in the same wake" in skill
    assert "session_sidebar_pending(limit=3)" not in skill
    assert "pending --limit 3" not in skill
    assert "concurrently across leases" not in skill
    assert "continue with the next leased job" not in skill
    assert "Continue settling the other leases" not in skill
    assert "exactly once" in skill
    assert "no user-facing message" in skill
    assert "local-e59c279a6cdda9313cf111e46a80b027" in skill
    assert "reconciliation_state" in skill
    assert "reconciliation_proof_digest" in skill
    assert "reconciliation_generation" in skill
    assert "create_eligible" in skill
    assert "recovered_thread_id" in skill
    assert "registration_prompt" in skill
    assert "exactly one native local task" in skill
    assert "rename" in skill.casefold()
    assert "session_sidebar_bind" in skill
    assert "session_sidebar_commit" in skill
    assert "session_sidebar_fail" in skill
    assert "error_code=<fixed code>" in skill
    assert "never `code`" in skill
    assert "exception text" in skill
    assert "the unfinished lease" in skill


def test_sidebar_skill_prioritizes_exact_task_hydration_without_creation() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "session_sidebar_hydration_pending(limit=1)" in skill
    assert "hydration-pending --limit 1" in skill
    assert "always call hydration pending once" in skill
    assert "if it is empty, always call registration pending once regardless of status counts" in skill

    hydration = skill.split("\n## In-place Hydration Procedure\n", 1)[1].split(
        "\n## Registration Procedure\n", 1
    )[0]
    assert (
        '`read_thread({"threadId":"<exact codex_thread_id>",'
        '"turnLimit":10,"includeOutputs":false})`' in hydration
    )
    assert "exact linked task ID" in hydration
    assert "exact hydration marker" in hydration
    assert "session_sidebar_hydration_reserve" in hydration
    assert "send_message_to_thread" in hydration
    assert "hydration_message" in hydration
    assert "session_sidebar_hydration_commit" in hydration
    assert "session_sidebar_hydration_fail" in hydration
    assert "`hydration_send_ambiguous`" in hydration
    assert "`native_task_not_indexed`" in hydration
    assert "`marker_conflict`" in hydration
    assert "`codex_thread_conflict`" in hydration
    assert "A projectless legacy task is valid and remains valid" in hydration
    assert "Never create, rename, archive, move, fork, or replace a task in hydration mode" in hydration
    assert "create_thread" not in hydration
    assert "set_thread_title" not in hydration
    assert "set_thread_archived" not in hydration
    assert "move" not in hydration.replace("move, fork", "")
    assert "fork" not in hydration.replace("move, fork", "")


def test_sidebar_skill_uses_only_live_public_hydration_lease_fields() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    hydration = skill.split("\n## In-place Hydration Procedure\n", 1)[1].split(
        "\n## Registration Procedure\n", 1
    )[0]

    assert (
        "Required lease fields: `lease_token`, `codex_thread_id`, "
        "`hydration_message`, `hydration_marker`, `cwd`, `git_root`, "
        "`send_reserved`." in hydration
    )
    assert "source_session_id" not in hydration
    assert "bridge ID" not in hydration
    assert "preview digest" not in hydration
    assert (
        "The coordinator already authenticated source, bridge, and preview before "
        "issuing the lease."
    ) in hydration


def test_sidebar_skill_requires_definite_hydration_reserve_before_send() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    hydration = skill.split("\n## In-place Hydration Procedure\n", 1)[1].split(
        "\n## Registration Procedure\n", 1
    )[0]
    reserve = hydration.split("- **Reserve and send.**", 1)[1].split(
        "- **Classify send uncertainty.**", 1
    )[0]

    assert "immediately before" in reserve
    assert "`state=hydration_leased` and `send_reserved=true`" in reserve
    assert "matching exact `codex_thread_id` and `hydration_marker` when supplied" in reserve
    assert "missing, malformed, stale, or ambiguous" in reserve
    assert "`bridge_temporarily_unavailable`" in reserve
    assert "do not call `send_message_to_thread`" in reserve
    assert reserve.index("session_sidebar_hydration_reserve") < reserve.index(
        "send_message_to_thread"
    )
    assert "Every resumed `send_reserved=true` lease reconciles the exact marker" in hydration


def test_sidebar_skill_allows_only_prebind_candidate_authentication_reads() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    registration = skill.split("\n## Registration Procedure\n", 1)[1].split(
        "\n## Fixed Failure Mapping\n", 1
    )[0]
    hard_stops = skill.split("\n## Hard Stops\n", 1)[1].split(
        "\n## Continuation Contract\n", 1
    )[0]

    assert (
        "Bounded pre-bind reads are allowed solely to authenticate the one exact "
        "recovered ID"
    ) in registration
    assert "do not poll, rename, or commit during candidate authentication" in registration
    assert "Never bind an unauthenticated candidate" in registration
    assert "call `session_sidebar_bind" in registration
    assert "do not poll, rename, commit, or create a replacement" in registration
    assert "Bounded pre-bind candidate-authentication reads are permitted" in hard_stops
    assert "Never poll, rename, or commit a selected task before binding" in hard_stops


def test_sidebar_skill_binds_new_create_id_before_first_read() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    create_step = skill.split("\n6. ", 1)[1].split("\n7. ", 1)[0]

    assert "newly returned create ID" in create_step
    assert create_step.index("session_sidebar_bind") < create_step.index("`read_thread`")


def test_sidebar_skill_preflights_bridge_and_native_projects_before_leasing() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    queue = skill.split("\n## Queue Selection\n", 1)[1].split(
        "\n## In-place Hydration Procedure\n", 1
    )[0]

    assert queue.index("session_status") < queue.index("read_thread")
    assert queue.index("read_thread") < queue.index("list_projects({})")
    assert queue.index("list_projects({})") < queue.index("session_sidebar_hydration_pending(limit=1)")
    assert "Preflight failure ends before leasing" in queue
    assert "no job attempt is consumed" in skill
    assert (
        "After successful preflight, always call hydration pending once; if it is "
        "empty, always call registration pending once regardless of status counts."
    ) in queue
    assert "Status counts never authorize skipping either persisted-heartbeat call." in queue
    assert "If both registration counts are zero, end immediately" not in skill
    assert "broker_project_id" in queue
    assert (
        "canonical path equals `C:\\Users\\diego\\Developer\\session-sidebar-broker`, "
        "whose returned ID equals configured `broker_project_id`"
    ) in queue
    assert (
        "canonical path equals `C:\\Users\\diego\\.hermes`; retain its returned ID "
        "separately as `inbox_project_id`"
    ) in queue
    assert "`.hermes` project ID equals configured `broker_project_id`" not in queue
    assert "ID differs, preflight stops before lease" in queue


def test_sidebar_skill_uses_queue_selected_registration_lease_once() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    registration = skill.split("\n## Registration Procedure\n", 1)[1].split(
        "\n## Fixed Failure Mapping\n", 1
    )[0]

    assert skill.count("session_sidebar_pending(limit=1)") == 1
    assert "session_sidebar_pending(limit=1)" not in registration
    assert "Use the lease already selected in Queue Selection." in registration


def test_sidebar_skill_keeps_source_cwd_as_metadata_not_an_attached_root() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "Source cwd is authenticated metadata only; only `.hermes` is an attached root." in skill
    assert "source cwd only as authenticated metadata and a runtime root" not in skill
    assert "source cwd as a runtime root" not in skill


def test_sidebar_skill_qualifies_registration_placement_failures_from_legacy_hydration() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    hydration = skill.split("\n## In-place Hydration Procedure\n", 1)[1].split(
        "\n## Registration Procedure\n", 1
    )[0]
    failure_table = skill.split("\n## Fixed Failure Mapping\n", 1)[1].split(
        "\n## Deterministic Call-Failure Rules\n", 1
    )[0]

    assert "Native task outside Session Inbox placement (registration/new mirror only)" in failure_table
    assert "Exact authenticated projectless legacy hydration is exempt and valid." in hydration


def test_sidebar_skill_uses_one_authenticated_local_transport_when_mcp_is_missing() -> (
    None
):
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "Authenticated Local Transport Fallback" in skill
    assert (
        'uv run --project "C:\\\\Users\\\\diego\\\\.hermes\\\\agent-src" '
        "--no-sync python -m session_bridge.broker_client"
    ) in skill
    assert "worktrees\\\\session-bridge-ship" not in skill
    assert "status|pending|reserve|bind|commit|fail" in skill
    assert "session_sidebar_reserve" in skill
    assert "reserve --lease-token=<exact token>" in skill
    assert "--lease-token=<exact token>" in skill
    assert "--lease-token <exact token>" not in skill
    assert "never call both transports for the same bridge step" in skill
    assert "counts as the exact single bridge call" in skill
    assert "If neither transport is available, stop before leasing" in skill


def test_sidebar_skill_closes_the_baseline_and_ambiguity_loopholes() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    folded = skill.casefold()

    assert "app-server" in folded and "never" in folded
    assert "transcript" in folded and "summar" in folded
    assert "ambiguous" in folded and "duplicate" in folded
    assert "without a lease" in folded
    assert "project-scoped" in folded
    assert "first substantive continuation" in folded
    assert "session_continue" in skill
    assert 'prompt="Audit billing"' not in skill
    assert 'prompt="Review launch"' not in skill


def test_sidebar_skill_binds_each_registration_branch_exactly_once() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    reconcile_step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]
    create_step = skill.split("\n6. ", 1)[1].split("\n7. ", 1)[0]
    verification_step = skill.split("\n7. ", 1)[1].split("\n8. ", 1)[0]

    bind_call = "session_sidebar_bind(lease_token=<exact token>, codex_thread_id=<threadId>)"
    assert reconcile_step.count(bind_call) == 1
    assert create_step.count(bind_call) == 1
    assert bind_call not in verification_step
    assert (
        "Reconciled and newly created tasks are already bound exactly once in their "
        "respective branches."
    ) in verification_step
    assert "Do not call `session_sidebar_bind` again" in verification_step
    assert (
        "Rename a bound task only after every applicable exact-ID read, identity, "
        "marker, and authenticated-quiescence check has passed"
    ) in verification_step
    assert (
        "On rename failure, call `session_sidebar_fail` with `rename_failed` and "
        "`codex_thread_id=<threadId>`; do not commit and do not create a "
        "replacement task."
    ) in verification_step


def test_sidebar_skill_reconciled_bind_is_after_candidate_authentication() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    reconcile_step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]

    assert reconcile_step.index("Never bind an unauthenticated candidate") < (
        reconcile_step.index("session_sidebar_bind")
    )


def test_sidebar_skill_waits_for_new_task_indexing_before_rename() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    create_step = skill.split("\n6. ", 1)[1].split("\n7. ", 1)[0]

    assert "returned `threadId`" in create_step
    assert "session_sidebar_bind" in create_step
    assert "`read_thread`" in create_step
    assert "same thread ID" in create_step
    assert "authenticated quiescent registration" in create_step
    assert "60 seconds" in create_step
    assert "`native_task_not_indexed`" in create_step
    bind_call = (
        "session_sidebar_bind(lease_token=<exact token>, "
        "codex_thread_id=<threadId>)"
    )
    assert create_step.index(bind_call) > create_step.index("returned `threadId`")
    assert create_step.index("`read_thread`") > create_step.index(
        "session_sidebar_bind"
    )


def test_sidebar_skill_accepts_only_authenticated_completed_notloaded_tasks() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "authenticated quiescent registration" in skill
    assert "literal `idle`" in skill
    assert "top-level status is `notLoaded`" in skill
    assert "at least one returned turn" in skill
    assert "every returned turn has status `completed`" in skill
    assert (
        "no active turn, approval request, user-input request, or system error"
        in skill
    )
    assert "Never treat `notLoaded` as globally equivalent to `idle`" in skill
    assert "Missing turns" in skill
    assert "incomplete turn" in skill
    assert "exact signed marker" in skill


def test_sidebar_skill_reserves_the_create_boundary_before_native_creation() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    reconcile_step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]
    create_step = skill.split("\n6. ", 1)[1].split("\n7. ", 1)[0]

    assert "`create_reserved`" in skill
    assert (
        "`session_sidebar_reserve(lease_token=<exact token>, "
        "reconciliation_proof_digest=<exact digest>, "
        "reconciliation_generation=<exact generation>)` immediately before native "
        "create"
    ) in create_step
    assert create_step.index("session_sidebar_reserve") < create_step.index(
        "create_thread"
    )
    assert "Do not create unless reserve succeeds" in create_step


def test_sidebar_skill_retains_exact_returned_id_when_bind_response_is_lost() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    create_step = skill.split("\n6. ", 1)[1].split("\n7. ", 1)[0]

    assert (
        "`session_sidebar_fail(lease_token=<exact token>, "
        "error_code=bridge_temporarily_unavailable, "
        "codex_thread_id=<threadId>)`"
    ) in create_step
    assert "fail --lease-token=<exact token> --error-code=<fixed code> " in skill
    assert "--thread-id=<threadId>" in skill
    assert "Only that exact same thread ID may be rebound idempotently" in skill
    assert "never create a replacement" in create_step


def test_sidebar_skill_passes_exact_id_to_every_fail_after_identity_is_known() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    bind_and_rename_step = skill.split("\n7. ", 1)[1].split("\n8. ", 1)[0]
    exit_step = skill.split("\n9. ", 1)[1].split("\n\n## Fixed Failure", 1)[0]

    assert "codex_thread_id=<threadId>" in bind_and_rename_step
    assert "include `codex_thread_id=<threadId>`" in exit_step


def test_sidebar_skill_gives_exact_native_tool_schemas_and_id_rules() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "`list_projects({})` exactly once" in skill
    assert "exactly one local saved project" in skill
    assert "canonical path equals `C:\\Users\\diego\\.hermes`" in skill
    assert (
        '`read_thread({"threadId":"<recovered_thread_id>","turnLimit":10,'
        '"includeOutputs":false})`'
    ) in skill
    assert "Pass no other fields" in skill
    assert "`codex_thread_conflict`" in skill
    create = (
        '`create_thread({"prompt":"<registration_prompt verbatim>",'
        '"target":{"type":"project","projectId":"local-e59c279a6cdda9313cf111e46a80b027",'
        '"environment":{"type":"local"}}})`'
    )
    assert create in skill
    assert "cwd" not in create
    assert "runtimeWorkspaceRoots" not in create
    assert "idempotencyKey" not in create
    create_examples = [
        line for line in skill.splitlines() if "create_thread({" in line
    ]
    assert create_examples
    assert all('"environment":{"type":"local"}' in line for line in create_examples)
    assert all('"environment":"local"' not in line for line in create_examples)
    assert "illustrates the validated returned production ID" in skill
    assert "substitute only the exact preflight-validated returned `inbox_project_id`" in skill
    assert "substitute only the exact preflight-validated returned `broker_project_id`" not in skill
    assert "Only the returned `threadId` is a successful create result" in skill
    assert (
        "`session_sidebar_bind(lease_token=<exact token>, "
        "codex_thread_id=<threadId>)`" in skill
    )
    assert (
        '`set_thread_title({"threadId":"<threadId>","title":"<exact title>"})`' in skill
    )


def test_sidebar_skill_uses_project_placement_only_for_registration() -> (
    None
):
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    hydration = skill.split("\n## In-place Hydration Procedure\n", 1)[1].split(
        "\n## Registration Procedure\n",
        1,
    )[0]
    registration = skill.split("\n## Registration Procedure\n", 1)[1].split(
        "\n## Fixed Failure Mapping\n",
        1,
    )[0]

    assert "projectless legacy task is valid" in hydration
    assert '"target":{"type":"project"' in registration


def test_sidebar_skill_uses_only_authoritative_reconciliation_paths() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    reconcile_step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]

    assert "Trust only the authoritative reconciliation object" in reconcile_step
    assert "When `reconciliation_state` is `recovered`" in reconcile_step
    assert (
        '`read_thread({"threadId":"<recovered_thread_id>",'
        '"turnLimit":10,"includeOutputs":false})`' in reconcile_step
    )
    assert '"turnLimit":20' not in reconcile_step
    assert "session_sidebar_bind" in reconcile_step
    assert "When `reconciliation_state` is `absence_proven`" in reconcile_step
    assert "do not inspect any other native task" in reconcile_step
    assert "A missing or unsupported reconciliation state" in reconcile_step
    recovered_branch = reconcile_step.split(
        "When `reconciliation_state` is `recovered`", 1
    )[1].split("When `reconciliation_state` is `absence_proven`", 1)[0]
    assert (
        "unavailable, missing, or not-yet-indexed recovered-ID read maps to "
        "`native_task_not_indexed`"
    ) in recovered_branch
    assert (
        "returns successfully but `thread.id` or the signed marker mismatches, map "
        "to `marker_conflict`"
    ) in recovered_branch
    assert (
        "host, environment, or task-kind field that explicitly contradicts local "
        "native execution maps to `codex_thread_conflict`"
    ) in recovered_branch
    assert "project identity outside the selected inbox project" in recovered_branch
    assert "maps to `placement_mismatch`" in recovered_branch
    assert (
        "missing or mismatched task maps to `marker_conflict`" not in recovered_branch
    )
    assert "never permits creation" in recovered_branch


def test_sidebar_skill_matches_the_native_read_thread_response_schema() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    reconcile_step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]

    assert "response's nested `thread` object" in reconcile_step
    assert "absence of a top-level thread ID is expected" in reconcile_step
    assert "`thread.id`, `thread.hostId`, and `thread.cwd`" in reconcile_step
    assert (
        "missing or null `thread.hostId` and explicit `local` normalize only to `local`"
    ) in reconcile_step
    assert "every other explicit `thread.hostId` maps to `codex_thread_conflict`" in (
        reconcile_step
    )
    assert "must equal the Session Inbox project's normalized host" in reconcile_step
    assert "does not return an explicit environment field" in reconcile_step
    assert "must not be treated as unavailable or ambiguous" in reconcile_step
    assert "explicitly contradicts local native execution" in reconcile_step


def test_sidebar_skill_fails_closed_when_recovered_task_drifted_outside_inbox() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    reconcile_step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]
    recovered_branch = reconcile_step.split(
        "When `reconciliation_state` is `recovered`", 1
    )[1].split("When `reconciliation_state` is `absence_proven`", 1)[0]

    assert "Require `thread.cwd` to match the resolved Session Inbox cwd" in (
        recovered_branch
    )
    assert "source cwd remains authenticated only from the registration metadata" in (
        recovered_branch
    )
    assert "it never satisfies native placement" in recovered_branch
    assert "`placement_mismatch`" in recovered_branch
    assert "never permits creation or replacement" in recovered_branch


def test_sidebar_skill_never_discovers_tasks_through_native_list_threads() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "list_threads" not in skill
    assert "reconciliation_state" in skill
    assert "reconciliation_proof_digest" in skill
    assert "reconciliation_generation" in skill
    assert "create_eligible" in skill


def test_sidebar_skill_deterministically_settles_native_and_broker_failures() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    required_rules = (
        "Unavailable native tool before native-create dispatch, or during a "
        "non-create native operation -> `codex_tool_unavailable`",
        "Desktop explicitly offline before native-create dispatch -> `desktop_offline`",
        "A native-create rejection proven before invoking `create_thread` -> "
        "`native_task_not_indexed`",
        "Unavailable, not-yet-indexed, or not-quiescent reconciliation read -> "
        "`native_task_not_indexed`",
        "Successfully returned thread-ID or marker mismatch, or multiple exact "
        "marker matches -> `marker_conflict`",
        "A registration candidate, recovered task, or newly created task whose cwd "
        "or supplied project identity is outside the resolved Session Inbox -> "
        "`placement_mismatch`",
        "Explicit host, environment, or task-kind contradiction -> "
        "`codex_thread_conflict`",
        "Failed rename -> `rename_failed`",
        "Definite or ambiguous commit failure -> `bridge_temporarily_unavailable`",
        "If the fail/release call itself fails",
        "exhausts settlement for that lease in this batch",
        "never call `session_sidebar_fail` for that lease again",
        "end this wake after that single settlement attempt",
        "never expose raw exception text",
    )
    for rule in required_rules:
        assert rule in skill
    assert "Never retry create after any ambiguous create outcome" in skill


def test_sidebar_skill_quarantines_ambiguous_create_without_retrying_creation() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert (
        "every raised error or missing or uncertain response, including an explicit "
        "desktop-offline tool error, is `native_create_ambiguous`"
    ) in skill
    assert "`native_create_ambiguous`" in skill
    assert "requires an operator audit before the failed job may be requeued" in skill
    assert "Never create a replacement after commit ambiguity" in skill


def test_sidebar_skill_treats_every_post_dispatch_create_error_as_ambiguous() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert (
        "After `session_sidebar_reserve` succeeds and `create_thread` is invoked, "
        "every raised error or missing or uncertain response, including an explicit "
        "desktop-offline tool error, is `native_create_ambiguous`."
    ) in skill
    assert ("`desktop_offline` applies only before native-create dispatch.") in skill
    assert (
        "Native Codex task/project operation unavailable before native-create "
        "dispatch, or during a non-create native operation"
    ) in skill
    assert "Desktop offline before native-create dispatch" in skill


def test_sidebar_skill_verification_requires_exactly_one_settlement_attempt() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    verification = skill.split("\n## Verification\n", 1)[1]

    assert (
        "the noncommitted lease had exactly one fail/release attempt with a "
        "fixed code, whether successful, failed, or ambiguous"
    ) in verification
    assert "every other lease was failed/released" not in verification
    assert "at most one" not in verification


def test_sidebar_skill_verification_is_conditional_on_the_taken_delivery_path() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    verification = skill.split("\n## Verification\n", 1)[1]

    assert "If a task was created" in verification
    assert "If commit succeeded" in verification
    assert "If commit did not succeed" in verification
    assert "hydration pending ran once" in verification
    assert "registration pending ran once only when hydration was empty" in verification


def test_sidebar_skill_normalizes_only_current_local_host_identity() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    project_step = skill.split("\n2. ", 1)[1].split("\n3. ", 1)[0]
    reconcile_step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]

    assert "(`projectId`, original returned `hostId`, normalized host)" in project_step
    assert "missing or null `hostId` and the explicit string `local`" in project_step
    assert "current-local sentinel `local`" in project_step
    assert "Reject every other explicit host value" in project_step
    assert "never infer or coerce an arbitrary host string" in project_step
    assert "normalized task host must equal the Session Inbox project's normalized host" in (
        reconcile_step
    )


def test_sidebar_skill_names_only_the_allowed_session_tools() -> None:
    import re

    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    named = set(re.findall(r"\bsession_[a-z_]+\b", skill))
    named.discard("session_bridge")  # module/server name, not a callable tool

    assert named == {
        "session_status",
        "session_sidebar_pending",
        "session_sidebar_reserve",
        "session_sidebar_bind",
        "session_sidebar_commit",
        "session_sidebar_fail",
        "session_sidebar_hydration_pending",
        "session_sidebar_hydration_reserve",
        "session_sidebar_hydration_commit",
        "session_sidebar_hydration_fail",
        "session_continue",
    }


def test_resolve_codex_home_prefers_explicit_environment(tmp_path: Path) -> None:
    from session_bridge.sidebar_skill import resolve_codex_home

    selected = tmp_path / "portable-codex"

    assert resolve_codex_home({"CODEX_HOME": str(selected)}) == selected


def test_resolve_codex_home_defaults_below_the_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge.sidebar_skill import resolve_codex_home

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert resolve_codex_home({}) == tmp_path / ".codex"


def test_install_sidebar_skill_copies_packaged_asset_and_is_idempotent(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    first = install_sidebar_skill(codex_home)
    first_digest = _content_digest(first)
    second = install_sidebar_skill(codex_home)
    second_digest = _content_digest(second)

    assert first == codex_home / "skills" / "session-sidebar-sync"
    assert second == first
    assert second_digest == first_digest
    assert _installed_files(first) == _installed_files(ASSET)
    assert list((codex_home / "skills").glob("session-sidebar-sync.backup*")) == []


def test_install_sidebar_skill_backs_up_different_content_without_collision(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    destination = codex_home / "skills" / "session-sidebar-sync"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("first", encoding="utf-8")
    install_sidebar_skill(codex_home)
    shutil.rmtree(destination)
    destination.mkdir()
    (destination / "old.txt").write_text("second", encoding="utf-8")

    install_sidebar_skill(codex_home)

    backups = sorted(
        (codex_home / BACKUP_ROOT_NAME).glob("session-sidebar-sync.backup*")
    )
    assert len(backups) == 2
    assert {(backup / "old.txt").read_text(encoding="utf-8") for backup in backups} == {
        "first",
        "second",
    }
    assert _installed_files(destination) == _installed_files(ASSET)


def test_install_sidebar_skill_keeps_replacement_backup_outside_skills_root(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    destination = skills / "session-sidebar-sync"
    destination.mkdir(parents=True)
    original = b"\x00\xfflegacy-sidebar-skill\r\n"
    (destination / "old.bin").write_bytes(original)

    install_sidebar_skill(codex_home)

    assert list(skills.glob("session-sidebar-sync.backup*")) == []
    backups = list((codex_home / BACKUP_ROOT_NAME).glob("session-sidebar-sync.backup*"))
    assert len(backups) == 1
    assert (backups[0] / "old.bin").read_bytes() == original


def test_install_sidebar_skill_migrates_legacy_backups_before_idempotent_return(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    destination = install_sidebar_skill(codex_home)
    payloads = {b"\x00\xfffirst\r\n", b"second\x00payload"}
    for name, payload in zip(
        ("session-sidebar-sync.backup", "session-sidebar-sync.backup-7"),
        payloads,
        strict=True,
    ):
        legacy = skills / name
        legacy.mkdir()
        (legacy / "old.bin").write_bytes(payload)

    assert _installed_files(destination) == _installed_files(ASSET)
    install_sidebar_skill(codex_home)

    assert list(skills.glob("session-sidebar-sync.backup*")) == []
    migrated = list(
        (codex_home / BACKUP_ROOT_NAME).glob("session-sidebar-sync.backup*")
    )
    assert len(migrated) == 2
    assert {(backup / "old.bin").read_bytes() for backup in migrated} == payloads
    assert _installed_files(destination) == _installed_files(ASSET)


def test_install_sidebar_skill_migrates_legacy_regular_file_backup_verbatim(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    destination = install_sidebar_skill(codex_home)
    payload = b"\x00\xfflegacy-file-backup\r\n"
    legacy = skills / "session-sidebar-sync.backup"
    legacy.write_bytes(payload)

    install_sidebar_skill(codex_home)

    assert not legacy.exists()
    migrated = list(
        (codex_home / BACKUP_ROOT_NAME).glob("session-sidebar-sync.backup*")
    )
    assert len(migrated) == 1
    assert migrated[0].is_file()
    assert migrated[0].read_bytes() == payload
    assert _installed_files(destination) == _installed_files(ASSET)


def test_install_sidebar_skill_rejects_redirected_legacy_backup(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    destination = install_sidebar_skill(codex_home)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.bin").write_bytes(b"do-not-move")
    legacy = skills / "session-sidebar-sync.backup"
    _create_directory_redirect(legacy, outside)

    with pytest.raises(PermissionError, match="redirect"):
        install_sidebar_skill(codex_home)

    assert legacy.exists()
    assert (outside / "keep.bin").read_bytes() == b"do-not-move"
    assert _installed_files(destination) == _installed_files(ASSET)
    _remove_directory_redirect(legacy)


def test_install_sidebar_skill_copy_failure_preserves_existing_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge import sidebar_skill

    codex_home = tmp_path / "codex"
    destination = codex_home / "skills" / "session-sidebar-sync"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(
        sidebar_skill,
        "_copy_packaged_skill",
        lambda _destination, *_guard: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with pytest.raises(PermissionError, match="denied"):
        sidebar_skill.install_sidebar_skill(codex_home)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "preserve"
    assert not list((codex_home / "skills").glob(".session-sidebar-sync.install-*"))


def test_installer_documents_portable_filesystem_threat_boundary() -> None:
    from session_bridge import sidebar_skill

    documentation = " ".join((sidebar_skill.__doc__ or "").split())

    assert "not a security boundary" in documentation
    assert "malicious process running as the same OS user" in documentation
    assert "between filesystem syscalls" in documentation
    assert "modify the installed skill afterward" in documentation
    assert "user-owned and not writable by other principals" in documentation


def test_install_sidebar_skill_rejects_observed_staging_redirect_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge import sidebar_skill

    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    destination = skills / "session-sidebar-sync"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("preserve", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    real_copy = sidebar_skill._copy_packaged_skill
    swapped: dict[str, Path] = {}

    def swap_staging_to_redirect(staging: Path, *guard: object) -> None:
        preserved = staging.with_name(f"{staging.name}.preserved")
        staging.rename(preserved)
        try:
            _create_directory_redirect(staging, outside)
        except OSError:
            preserved.rename(staging)
            pytest.skip("directory symlinks are unavailable")
        swapped.update(staging=staging, preserved=preserved)
        real_copy(staging, *guard)

    monkeypatch.setattr(sidebar_skill, "_copy_packaged_skill", swap_staging_to_redirect)

    with pytest.raises(PermissionError, match="redirect|identity"):
        sidebar_skill.install_sidebar_skill(codex_home)

    assert list(outside.iterdir()) == []
    assert (destination / "old.txt").read_text(encoding="utf-8") == "preserve"
    assert swapped["preserved"].is_dir()

    _remove_directory_redirect(swapped["staging"])
    swapped["preserved"].rename(swapped["staging"])
    user_lookalike = skills / ".session-sidebar-sync.install-user-content"
    user_lookalike.mkdir()
    (user_lookalike / "notes.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(sidebar_skill, "_copy_packaged_skill", real_copy)

    sidebar_skill.install_sidebar_skill(codex_home)

    assert not swapped["staging"].exists()
    assert (user_lookalike / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_install_sidebar_skill_revalidates_parent_identity_before_copy_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge import sidebar_skill

    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    destination = skills / "session-sidebar-sync"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("preserve", encoding="utf-8")
    real_copy = sidebar_skill._copy_packaged_skill
    real_lstat = os.lstat
    identity_drifted = False

    def arm_identity_drift(staging: Path, *guard: object) -> None:
        nonlocal identity_drifted
        identity_drifted = True
        real_copy(staging, *guard)

    def drifting_lstat(path: os.PathLike[str] | str):
        info = real_lstat(path)
        if identity_drifted and Path(path).absolute() == skills.absolute():
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=getattr(info, "st_file_attributes", 0),
                st_dev=info.st_dev,
                st_ino=info.st_ino + 1,
            )
        return info

    monkeypatch.setattr(sidebar_skill, "_copy_packaged_skill", arm_identity_drift)
    monkeypatch.setattr(os, "lstat", drifting_lstat)

    with pytest.raises(PermissionError, match="identity"):
        sidebar_skill.install_sidebar_skill(codex_home)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "preserve"


def test_install_sidebar_skill_allows_benign_windows_directory_attribute_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge import sidebar_skill

    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    real_copy = sidebar_skill._copy_packaged_skill
    real_lstat = os.lstat
    attributes_drifted = False

    def arm_attribute_drift(staging: Path, *guard: object) -> None:
        nonlocal attributes_drifted
        attributes_drifted = True
        real_copy(staging, *guard)

    def drifting_lstat(path: os.PathLike[str] | str):
        info = real_lstat(path)
        if attributes_drifted and Path(path).absolute() == skills.absolute():
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=getattr(info, "st_file_attributes", 0) ^ 0x20,
                st_dev=info.st_dev,
                st_ino=info.st_ino,
            )
        return info

    monkeypatch.setattr(sidebar_skill, "_copy_packaged_skill", arm_attribute_drift)
    monkeypatch.setattr(os, "lstat", drifting_lstat)

    installed = sidebar_skill.install_sidebar_skill(codex_home)

    assert installed == skills / "session-sidebar-sync"
    assert (installed / "SKILL.md").is_file()


def test_install_sidebar_skill_reports_operation_and_cleanup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge import sidebar_skill

    def copy_failure(_destination: Path, *guard: object) -> None:
        raise PermissionError("copy denied")

    def cleanup_failure(_destination: Path, **_kwargs: object) -> None:
        raise OSError("cleanup denied")

    monkeypatch.setattr(sidebar_skill, "_copy_packaged_skill", copy_failure)
    monkeypatch.setattr(shutil, "rmtree", cleanup_failure)

    with pytest.raises(ExceptionGroup) as captured:
        sidebar_skill.install_sidebar_skill(tmp_path / "codex")

    messages = {str(error) for error in captured.value.exceptions}
    assert messages == {"copy denied", "cleanup denied"}


def test_install_sidebar_skill_preserves_promotion_and_restore_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge import sidebar_skill

    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    destination = skills / "session-sidebar-sync"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("preserve", encoding="utf-8")
    real_guarded_replace = sidebar_skill._guarded_replace

    def fail_promotion_and_restore(
        source: Path, target: Path, identity: object
    ) -> None:
        if source == destination:
            real_guarded_replace(source, target, identity)
            return
        if source.name.startswith(".session-sidebar-sync.install-"):
            raise OSError("promotion failed")
        if source.name.startswith("session-sidebar-sync.backup"):
            raise PermissionError("restore failed")
        real_guarded_replace(source, target, identity)

    monkeypatch.setattr(sidebar_skill, "_guarded_replace", fail_promotion_and_restore)

    with pytest.raises(BaseExceptionGroup) as captured:
        sidebar_skill.install_sidebar_skill(codex_home)

    assert {str(error) for error in captured.value.exceptions} == {
        "promotion failed",
        "restore failed",
    }
    assert not destination.exists()
    backup = next((codex_home / BACKUP_ROOT_NAME).glob("session-sidebar-sync.backup*"))
    assert (backup / "old.txt").read_text(encoding="utf-8") == "preserve"


def test_filesystem_lock_closes_descriptor_when_unlock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge import sidebar_skill

    class TrackedStream:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    tracked = TrackedStream()
    monkeypatch.setattr(sidebar_skill, "_open_lock_descriptor", lambda _path: tracked)
    monkeypatch.setattr(sidebar_skill, "_try_lock_descriptor", lambda _descriptor: True)
    monkeypatch.setattr(
        sidebar_skill,
        "_unlock_descriptor",
        lambda _descriptor: (_ for _ in ()).throw(OSError("unlock failed")),
    )

    with pytest.raises(OSError, match="unlock failed"):
        with sidebar_skill._filesystem_install_lock(tmp_path):
            pass

    assert tracked.closed is True


def test_install_sidebar_skill_refuses_redirected_destination(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    outside = tmp_path / "outside"
    outside.mkdir()
    skills.mkdir(parents=True)
    destination = skills / "session-sidebar-sync"
    try:
        destination.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(PermissionError, match="redirect"):
        install_sidebar_skill(codex_home)

    assert list(outside.iterdir()) == []


def test_install_sidebar_skill_refuses_redirected_parent_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    redirected_parent = tmp_path / "redirected-parent"
    redirected_parent.mkdir()
    real_lstat = os.lstat

    def redirect_aware_lstat(path: os.PathLike[str] | str):
        if Path(path).absolute() == redirected_parent.absolute():
            return SimpleNamespace(st_mode=0o120777, st_file_attributes=0)
        return real_lstat(path)

    monkeypatch.setattr(os, "lstat", redirect_aware_lstat)

    with pytest.raises(PermissionError, match="redirect"):
        install_sidebar_skill(redirected_parent / "codex")

    assert list(redirected_parent.iterdir()) == []


def test_install_sidebar_skill_serializes_concurrent_repeated_installs(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _index: install_sidebar_skill(codex_home), range(8))
        )

    assert len(set(results)) == 1
    assert _installed_files(results[0]) == _installed_files(ASSET)
    assert not list((codex_home / "skills").glob(".session-sidebar-sync.install-*"))


@pytest.mark.live_system_guard_bypass
def test_process_lock_serializes_install_and_preserves_one_backup(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    destination = skills / "session-sidebar-sync"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("preserve", encoding="utf-8")
    ready = tmp_path / "holder-ready"
    holder = _start_python(
        "from pathlib import Path\n"
        "import sys, time\n"
        "from session_bridge.sidebar_skill import _filesystem_install_lock\n"
        "skills, ready = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "with _filesystem_install_lock(skills):\n"
        "    ready.write_text('ready', encoding='utf-8')\n"
        "    time.sleep(0.6)\n",
        skills,
        ready,
    )
    _wait_for_path(ready, holder)
    installer = _start_python(
        "from pathlib import Path\n"
        "import sys\n"
        "from session_bridge.sidebar_skill import install_sidebar_skill\n"
        "print(install_sidebar_skill(Path(sys.argv[1])))\n",
        codex_home,
    )
    time.sleep(0.15)

    assert installer.poll() is None, "installer must wait for the held OS lock"
    holder_stdout, holder_stderr = holder.communicate(timeout=5)
    installer_stdout, installer_stderr = installer.communicate(timeout=10)
    assert holder.returncode == 0, (holder_stdout, holder_stderr)
    assert installer.returncode == 0, (installer_stdout, installer_stderr)
    backups = list((codex_home / BACKUP_ROOT_NAME).glob("session-sidebar-sync.backup*"))
    assert len(backups) == 1
    assert (backups[0] / "old.txt").read_text(encoding="utf-8") == "preserve"
    assert _installed_files(destination) == _installed_files(ASSET)
    assert (skills / LOCK_NAME).is_file(), "descriptor lock file is persistent"


@pytest.mark.live_system_guard_bypass
def test_process_lock_crash_releases_descriptor_without_unlinking(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "codex" / "skills"
    skills.mkdir(parents=True)
    ready = tmp_path / "crash-ready"
    holder = _start_python(
        "from pathlib import Path\n"
        "import os, sys\n"
        "from session_bridge.sidebar_skill import _filesystem_install_lock\n"
        "skills, ready = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "with _filesystem_install_lock(skills):\n"
        "    ready.write_text('ready', encoding='utf-8')\n"
        "    os._exit(23)\n",
        skills,
        ready,
    )
    _wait_for_path(ready, holder)
    lock_path = skills / LOCK_NAME
    identity_before = (lock_path.stat().st_dev, lock_path.stat().st_ino)
    holder.wait(timeout=5)
    result = _run_python(
        "from pathlib import Path\n"
        "import sys\n"
        "from session_bridge.sidebar_skill import install_sidebar_skill\n"
        "install_sidebar_skill(Path(sys.argv[1]))\n",
        tmp_path / "codex",
    )

    assert holder.returncode == 23
    assert result.returncode == 0, result.stderr
    assert lock_path.is_file()
    assert (lock_path.stat().st_dev, lock_path.stat().st_ino) == identity_before


@pytest.mark.live_system_guard_bypass
def test_process_lock_ignores_malformed_persistent_contents(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    skills.mkdir(parents=True)
    lock_path = skills / LOCK_NAME
    lock_path.write_bytes(b"not-json-and-not-a-pid")
    identity_before = (lock_path.stat().st_dev, lock_path.stat().st_ino)

    result = _run_python(
        "from pathlib import Path\n"
        "import sys\n"
        "import session_bridge.sidebar_skill as skill\n"
        "skill._LOCK_WAIT_SECONDS = 0.1\n"
        "skill.install_sidebar_skill(Path(sys.argv[1]))\n",
        codex_home,
    )

    assert result.returncode == 0, result.stderr
    assert lock_path.read_bytes() == b"not-json-and-not-a-pid"
    assert (lock_path.stat().st_dev, lock_path.stat().st_ino) == identity_before


@pytest.mark.live_system_guard_bypass
def test_process_lock_times_out_without_stealing_held_descriptor(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    skills.mkdir(parents=True)
    ready = tmp_path / "timeout-ready"
    holder = _start_python(
        "from pathlib import Path\n"
        "import sys, time\n"
        "from session_bridge.sidebar_skill import _filesystem_install_lock\n"
        "skills, ready = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "with _filesystem_install_lock(skills):\n"
        "    ready.write_text('ready', encoding='utf-8')\n"
        "    time.sleep(5.0)\n",
        skills,
        ready,
    )
    _wait_for_path(ready, holder)
    result = _run_python(
        "from pathlib import Path\n"
        "import sys\n"
        "import session_bridge.sidebar_skill as skill\n"
        "skill._LOCK_WAIT_SECONDS = 0.1\n"
        "try:\n"
        "    skill.install_sidebar_skill(Path(sys.argv[1]))\n"
        "except TimeoutError:\n"
        "    print('timeout')\n"
        "else:\n"
        "    raise SystemExit('lock was stolen')\n",
        codex_home,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "timeout"
    holder_stdout, holder_stderr = holder.communicate(timeout=5)
    assert holder.returncode == 0, (holder_stdout, holder_stderr)
    assert (skills / LOCK_NAME).is_file()
    assert not (skills / "session-sidebar-sync").exists()


@pytest.mark.live_system_guard_bypass
def test_process_lock_concurrent_installers_do_not_lose_backup_or_install(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    destination = skills / "session-sidebar-sync"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("original", encoding="utf-8")
    code = (
        "from pathlib import Path\n"
        "import sys\n"
        "from session_bridge.sidebar_skill import install_sidebar_skill\n"
        "install_sidebar_skill(Path(sys.argv[1]))\n"
    )
    installers = [_start_python(code, codex_home) for _index in range(4)]

    outputs = [process.communicate(timeout=15) for process in installers]

    assert [process.returncode for process in installers] == [0, 0, 0, 0], outputs
    backups = list((codex_home / BACKUP_ROOT_NAME).glob("session-sidebar-sync.backup*"))
    assert len(backups) == 1
    assert (backups[0] / "old.txt").read_text(encoding="utf-8") == "original"
    assert _installed_files(destination) == _installed_files(ASSET)
    assert (skills / LOCK_NAME).is_file()


@pytest.mark.skipif(
    "built_wheel" not in " ".join(sys.argv),
    reason="run explicitly because a full repository wheel exceeds focused timeout",
)
@pytest.mark.timeout(180)
def test_built_wheel_contains_the_sidebar_skill_assets(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["UV_NO_PROGRESS"] = "1"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    wheel = next(tmp_path.glob("*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = archive.read(entry_points_name).decode("utf-8")
        extracted = tmp_path / "extracted-wheel"
        archive.extractall(extracted)

    assert "session_bridge/assets/session-sidebar-sync/SKILL.md" in names
    assert "session_bridge/assets/session-sidebar-sync/agents/openai.yaml" in names
    assert "session_bridge/entrypoint.py" in names
    assert "hermes-session-bridge = session_bridge.entrypoint:main" in entry_points

    codex_home = tmp_path / "wheel-codex-home"
    environment = {
        "CODEX_HOME": str(codex_home),
        "PYTHONPATH": str(extracted),
        "SYSTEMROOT": os.environ["SYSTEMROOT"],
    }
    installed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from session_bridge.entrypoint import main; raise SystemExit(main())",
            "install-sidebar-skill",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout) == {
        "status": "installed",
        "path": str(codex_home / "skills" / "session-sidebar-sync"),
    }
    assert _installed_files(
        codex_home / "skills" / "session-sidebar-sync"
    ) == _installed_files(ASSET)
