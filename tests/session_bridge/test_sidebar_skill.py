from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    deadline = time.monotonic() + 5.0
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


def test_sidebar_skill_baseline_records_the_verbatim_no_skill_failure() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")

    assert 'prompt="Audit billing"' in baseline
    assert 'prompt="Review launch"' in baseline
    assert "No project-list call was made" in baseline


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


def test_sidebar_skill_encodes_the_bounded_parallel_native_delivery_protocol() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "session_sidebar_pending(limit=5)" in skill
    assert "at most five" in skill
    assert "concurrently across leases" in skill
    assert "preserve the procedure order within each lease" in skill
    assert "exactly once" in skill
    assert "no user-facing message" in skill
    assert "list" in skill.casefold() and "projects" in skill.casefold()
    assert "canonical local path" in skill
    assert "exact cwd" in skill
    assert "exact git root" in skill
    assert "Session Inbox" in skill
    assert "reconcile_required" in skill
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
    assert "every unfinished lease" in skill


def test_sidebar_skill_preflights_bridge_and_native_projects_before_leasing() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert skill.index("session_status") < skill.index("list_projects({})")
    assert skill.index("list_projects({})") < skill.index(
        "session_sidebar_pending(limit=5)"
    )
    assert "do not call `session_sidebar_pending`" in skill
    assert "no job attempt is consumed" in skill


def test_sidebar_skill_uses_one_authenticated_local_transport_when_mcp_is_missing() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "Authenticated Local Transport Fallback" in skill
    assert (
        'uv run --project "C:\\\\Users\\\\diego\\\\.hermes\\\\worktrees\\\\'
        'session-bridge-ship" --no-sync python -m session_bridge.broker_client'
    ) in skill
    assert "status|pending|bind|commit|fail" in skill
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
    assert "sidebar grouping" in folded and "command cwd" in folded
    assert "first substantive continuation" in folded
    assert "session_continue" in skill
    assert 'prompt="Audit billing"' not in skill
    assert 'prompt="Review launch"' not in skill


def test_sidebar_skill_unconditionally_renames_every_task_before_commit() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    rename_step = skill.split("\n7. ", 1)[1].split("\n8. ", 1)[0]

    assert rename_step.startswith(
        "Before any rename, durably bind every reconciled task and every newly "
        "created task to its exact native thread ID"
    )
    assert "Rename every bound task" in rename_step
    assert "whenever" not in rename_step
    assert "flag" not in rename_step.casefold()
    assert (
        "On rename failure, call `session_sidebar_fail` with `rename_failed`; "
        "do not commit and do not create a replacement task."
    ) in rename_step


def test_sidebar_skill_waits_for_new_task_indexing_before_rename() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    create_step = skill.split("\n6. ", 1)[1].split("\n7. ", 1)[0]

    assert "returned `threadId`" in create_step
    assert "session_sidebar_bind" in create_step
    assert "`read_thread`" in create_step
    assert "same thread ID" in create_step
    assert "status is `idle`" in create_step
    assert "60 seconds" in create_step
    assert "`native_task_not_indexed`" in create_step
    assert create_step.index("session_sidebar_bind") > create_step.index(
        "returned `threadId`"
    )
    assert create_step.index("`read_thread`") > create_step.index(
        "session_sidebar_bind"
    )


def test_sidebar_skill_gives_exact_native_tool_schemas_and_id_rules() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "`list_projects({})` exactly once" in skill
    assert "canonical path to its returned `projectId`" in skill
    assert '`list_threads({"query":"<exact signed marker>","limit":20})`' in skill
    assert (
        '`read_thread({"threadId":"<candidate threadId>",'
        '"hostId":"<candidate hostId>","turnLimit":10,'
        '"includeOutputs":false})`'
    ) in skill
    assert "Omit `hostId` only when it was absent or null" in skill
    assert "Pass no other fields" in skill
    assert '`read_thread({"threadId":"<candidate threadId>",...})`' not in skill
    assert "Before matching the signed marker" in skill
    assert "remote-host candidate" in skill
    assert "`codex_thread_conflict`" in skill
    assert (
        '`create_thread({"prompt":"<registration_prompt verbatim>",'
        '"target":{"type":"project","projectId":"<chosen projectId>",'
        '"environment":{"type":"local"}}})`'
    ) in skill
    assert "Only the returned `threadId` is a successful create result" in skill
    assert "`worktreeId`" in skill and "`clientThreadId`" in skill
    assert (
        "`session_sidebar_bind(lease_token=<exact token>, "
        "codex_thread_id=<threadId>)`" in skill
    )
    assert (
        '`set_thread_title({"threadId":"<threadId>","title":"<exact title>"})`' in skill
    )


def test_sidebar_skill_reads_recovered_thread_directly_before_marker_search() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    reconcile_step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]

    assert (
        '`read_thread({"threadId":"<recovered_thread_id>",'
        '"turnLimit":10,"includeOutputs":false})`' in reconcile_step
    )
    assert '"turnLimit":20' not in reconcile_step
    assert "Ten is the bounded reconciliation and read limit" in reconcile_step
    assert "Do not call `list_threads` before this recovered-ID read" in reconcile_step
    assert (
        "Only when `recovered_thread_id` is absent, call "
        '`list_threads({"query":"<exact signed marker>","limit":20})`' in reconcile_step
    )
    recovered_branch = reconcile_step.split("When `recovered_thread_id` is present", 1)[
        1
    ].split("Only when `recovered_thread_id` is absent", 1)[0]
    assert "missing or mismatched task maps to `marker_conflict`" in recovered_branch
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
    assert "must equal the chosen project's normalized host" in reconcile_step
    assert "does not return an explicit environment field" in reconcile_step
    assert "must not be treated as unavailable or ambiguous" in reconcile_step
    assert "explicitly contradicts local native execution" in reconcile_step


def test_sidebar_skill_never_creates_after_an_unverifiable_search_candidate() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    reconcile_step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]

    assert (
        "Creation is permitted only when the exact-marker search returns zero "
        "candidate summaries"
    ) in reconcile_step
    assert (
        "any returned candidate that cannot be authenticated within the ten-turn "
        "read maps to `native_task_not_indexed`"
    ) in reconcile_step
    assert "never continue to creation after a candidate summary was returned" in (
        reconcile_step
    )
    assert "no match: continue to creation" not in reconcile_step


def test_sidebar_skill_deterministically_settles_native_and_broker_failures() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    required_rules = (
        "Unavailable native tool -> `codex_tool_unavailable`",
        "Desktop explicitly offline -> `desktop_offline`",
        "Definite or ambiguous create failure -> `native_task_not_indexed`",
        "Failed or ambiguous reconciliation -> `native_task_not_indexed`",
        "Failed rename -> `rename_failed`",
        "Definite or ambiguous commit failure -> `bridge_temporarily_unavailable`",
        "If the fail/release call itself fails",
        "exhausts settlement for that lease in this batch",
        "never call `session_sidebar_fail` for that lease again",
        "continue with the next leased job",
        "never expose raw exception text",
    )
    for rule in required_rules:
        assert rule in skill
    assert "Never retry create after any ambiguous create outcome" in skill
    assert "Never create a replacement after commit ambiguity" in skill


def test_sidebar_skill_verification_requires_exactly_one_settlement_attempt() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    verification = skill.split("\n## Verification\n", 1)[1]

    assert (
        "every noncommitted lease had exactly one fail/release attempt with a "
        "fixed code, whether successful, failed, or ambiguous"
    ) in verification
    assert "every other lease was failed/released" not in verification
    assert "at most one" not in verification


def test_sidebar_skill_normalizes_only_current_local_host_identity() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    project_step = skill.split("\n2. ", 1)[1].split("\n3. ", 1)[0]
    reconcile_step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]

    assert "(`projectId`, original returned `hostId`, normalized host)" in project_step
    assert "missing or null `hostId` and the explicit string `local`" in project_step
    assert "current-local sentinel `local`" in project_step
    assert "Reject every other explicit host value" in project_step
    assert "never infer or coerce an arbitrary host string" in project_step
    assert (
        "Apply the same host normalization to every thread candidate" in reconcile_step
    )
    assert "normalized host is `local`" in reconcile_step
    assert "equals the chosen project's normalized host" in reconcile_step
    assert "original candidate `hostId`" in reconcile_step
    assert "Omit `hostId` only when it was absent or null" in reconcile_step
    assert "remote marker collision" in reconcile_step


def test_sidebar_skill_filters_explicit_remote_candidate_before_read() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    reconcile_step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]

    filter_rule = (
        "Normalize and filter each `list_threads` candidate summary before any "
        "`read_thread` call"
    )
    read_schema = (
        '`read_thread({"threadId":"<candidate threadId>","hostId":"<candidate hostId>"'
    )
    assert filter_rule in reconcile_step
    assert reconcile_step.index(filter_rule) < reconcile_step.index(read_schema)
    assert (
        "explicit non-`local` host maps to `codex_thread_conflict` without a read"
        in (reconcile_step)
    )
    assert "only when that summary supplies project identity" in reconcile_step
    assert "do not invent a missing project field" in reconcile_step


def test_sidebar_skill_names_only_the_allowed_session_tools() -> None:
    import re

    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    named = set(re.findall(r"\bsession_[a-z_]+\b", skill))
    named.discard("session_bridge")  # module/server name, not a callable tool

    assert named == {
        "session_status",
        "session_sidebar_pending",
        "session_sidebar_bind",
        "session_sidebar_commit",
        "session_sidebar_fail",
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
    second = install_sidebar_skill(codex_home)

    assert first == codex_home / "skills" / "session-sidebar-sync"
    assert second == first
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

    backups = sorted((codex_home / "skills").glob("session-sidebar-sync.backup*"))
    assert len(backups) == 2
    assert {(backup / "old.txt").read_text(encoding="utf-8") for backup in backups} == {
        "first",
        "second",
    }
    assert _installed_files(destination) == _installed_files(ASSET)


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
    backup = next(skills.glob("session-sidebar-sync.backup*"))
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
    backups = list(skills.glob("session-sidebar-sync.backup*"))
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
    backups = list(skills.glob("session-sidebar-sync.backup*"))
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
