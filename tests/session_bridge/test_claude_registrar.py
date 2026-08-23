from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timezone
import io
import hashlib
import json
from itertools import product
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time
from typing import Any, cast

import pytest

from hermes_state import SessionDB

from session_bridge.claude_adapter import (
    ClaudeSourceAdapter,
    claude_project_directory_name,
)
from session_bridge.characterize import build_characterization_auth_recovery_prompt
from session_bridge.claude_registrar import (
    ClaudeNativeRegistrar,
    PtyCleanupResult,
    WindowsConPtyFactory,
    _PtyReadinessTimeout,
    _RegistrarCancelled,
    _PtyResponseTimeout,
    _WinPtyProcess,
    _claude_main_repl_ready,
    _registrar_pywinpty_process_type,
)
from session_bridge.claude_visibility import (
    ClaudeVisibilityCandidate,
    ClaudeVisibilityClaim,
    build_claude_registration_prompt,
    derive_claude_visibility_identity,
)
from session_bridge.models import (
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
)
from session_bridge.store import SessionBridgeStore


SECRET = b"registrar-test-marker-secret"


def candidate() -> ClaudeVisibilityCandidate:
    return ClaudeVisibilityCandidate(
        source_session_id="codex:source-1",
        source_provider=Provider.CODEX,
        native_name="[Codex] Explain the registrar",
        source_cwd="C:/exact/project/subdir",
        git_root="C:/exact/project",
        git_branch="main",
        git_head="abc123",
        worktree_id="worktree-1",
        eligible_at=10.0,
    )


def claim(**changes: Any) -> ClaudeVisibilityClaim:
    value = candidate()
    identity = derive_claude_visibility_identity(value, SECRET)
    base = ClaudeVisibilityClaim(
        status="claimed",
        lease_kind="launch",
        job_id=identity.job_id,
        source_session_id=value.source_session_id,
        source_provider=value.source_provider,
        reserved_claude_uuid=identity.claude_uuid,
        native_name=value.native_name,
        source_cwd=value.source_cwd,
        git_root=value.git_root,
        git_branch=value.git_branch,
        git_head=value.git_head,
        worktree_id=value.worktree_id,
        signed_marker=identity.signed_marker,
        lease_digest="a" * 64,
        attempt_ordinal=1,
        registration_reserved=True,
        launch_permitted=True,
    )
    return replace(base, **changes)


def test_cancelled_claim_retries_without_spawning_or_creation_ambiguity() -> None:
    store = FakeStore()
    factory = FakeFactory()
    stop = threading.Event()
    stop.set()

    result = registrar(FakeSource(), factory, store).process(claim(), stop=stop)

    assert result.status == "retry"
    assert result.error_code == "session_bridge_unavailable"
    assert factory.spawns == []
    assert [call[0] for call in store.calls] == ["retry"]


def test_cancelled_reconciliation_does_not_write_exact_absence() -> None:
    store = FakeStore()
    stop = threading.Event()
    stop.set()
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )

    result = registrar(FakeSource(), FakeFactory(), store).process(item, stop=stop)

    assert result.status == "retry"
    assert result.error_code == "session_bridge_unavailable"
    assert [call[0] for call in store.calls] == ["retry"]


def test_cancellation_during_reconciliation_lookup_does_not_record_absence() -> None:
    stop = threading.Event()
    store = FakeStore()
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )

    class CancellingSource(FakeSource):
        def find_native_session(self, native_id: str) -> Path | None:
            stop.set()
            return super().find_native_session(native_id)

    result = registrar(CancellingSource(), FakeFactory(), store).process(
        item, stop=stop
    )

    assert result.status == "retry"
    assert result.error_code == "session_bridge_unavailable"
    assert [call[0] for call in store.calls] == ["retry"]


def test_cancellation_during_existing_exact_lookup_does_not_commit() -> None:
    stop = threading.Event()
    store = FakeStore()
    item = claim()

    class CancellingSource(FakeSource):
        def find_native_session(self, native_id: str) -> Path | None:
            found = super().find_native_session(native_id)
            stop.set()
            return found

    result = registrar(
        CancellingSource([projection_for(item)]), FakeFactory(), store
    ).process(item, stop=stop)

    assert result.status == "retry"
    assert result.error_code == "session_bridge_unavailable"
    assert [call[0] for call in store.calls] == ["retry"]


def test_cancellation_during_exact_lookup_still_prevents_spawn() -> None:
    stop = threading.Event()
    store = FakeStore()

    class CancellingSource(FakeSource):
        def find_native_session(self, native_id: str) -> Path | None:
            stop.set()
            return super().find_native_session(native_id)

    factory = FakeFactory()

    result = registrar(CancellingSource(), factory, store).process(claim(), stop=stop)

    assert factory.spawns == []
    assert result.status == "retry"
    assert result.error_code == "session_bridge_unavailable"
    assert [call[0] for call in store.calls] == ["retry"]


def test_post_spawn_cancellation_binds_token_and_cleans_up_as_ambiguous() -> None:
    stop = threading.Event()
    store = FakeStore()

    class CancellingPty(FakePty):
        def __init__(self) -> None:
            super().__init__()
            self.bound_stop: object | None = None

        def set_cancel_event(self, value: object) -> None:
            self.bound_stop = value

        def read_until_ready(
            self, timeout: float, *, accept_workspace_trust: bool = False
        ) -> str:
            stop.set()
            raise _RegistrarCancelled()

    process = CancellingPty()
    factory = FakeFactory(process)

    result = registrar(FakeSource(), factory, store).process(claim(), stop=stop)

    assert process.bound_stop is stop
    assert process.terminated is True
    assert process.closed is True
    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert [call[0] for call in store.calls] == ["retry"]


def test_cancellation_during_prompt_settle_cannot_commit_and_cleans_up() -> None:
    stop = threading.Event()
    store = FakeStore()

    class CancelAfterPromptWrite(FakePty):
        def write(self, data: str) -> None:
            super().write(data)
            stop.set()

    process = CancelAfterPromptWrite()

    result = registrar(FakeSource(), FakeFactory(process), store).process(
        claim(), stop=stop
    )

    assert process.prompt_input_waits == []
    assert process.terminated is True
    assert process.closed is True
    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert [call[0] for call in store.calls] == ["retry"]


def test_cancellation_interrupts_transcript_discovery_poll() -> None:
    stop = threading.Event()
    store = FakeStore()
    item = claim()
    process = FakePty()

    class CancellingSource(FakeSource):
        def find_native_session(self, native_id: str) -> Path | None:
            found = super().find_native_session(native_id)
            if len(self.lookups) == 2:
                stop.set()
            return found

    source = CancellingSource([None, None])

    result = registrar(
        source,
        FakeFactory(process),
        store,
        discovery_timeout=30.0,
    ).process(item, stop=stop)

    assert source.lookups == [item.reserved_claude_uuid, item.reserved_claude_uuid]
    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert [call[0] for call in store.calls] == ["retry"]


def test_post_spawn_cancellation_cannot_commit_discovered_transcript() -> None:
    stop = threading.Event()
    store = FakeStore()
    item = claim()

    class CancellingSource(FakeSource):
        def find_native_session(self, native_id: str) -> Path | None:
            found = super().find_native_session(native_id)
            if len(self.lookups) == 2:
                stop.set()
            return found

    source = CancellingSource([None, projection_for(item)])

    result = registrar(source, FakeFactory(FakePty()), store).process(item, stop=stop)

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert [call[0] for call in store.calls] == ["retry"]


def test_claude_visibility_claim_rejects_positional_construction() -> None:
    with pytest.raises(TypeError):
        ClaudeVisibilityClaim("claimed", "launch")  # type: ignore[misc]


@dataclass
class FakeParse:
    projection: SessionProjection
    malformed_lines: int = 0
    unknown_records: int = 0
    entrypoint: str | None = "cli"


class FakeSource:
    def __init__(
        self,
        projections: list[SessionProjection | None] | None = None,
        *,
        parse_error: Exception | None = None,
        project_name: str | None = None,
        duplicate_paths: list[Path] | None = None,
        malformed_lines: int = 0,
        unknown_records: int = 0,
        entrypoint: str | None = "cli",
    ):
        self.projections = list(projections or [None])
        self.lookups: list[str] = []
        self.parse_error = parse_error
        self.project_name = project_name
        self.duplicate_paths = duplicate_paths
        self.malformed_lines = malformed_lines
        self.unknown_records = unknown_records
        self.entrypoint = entrypoint

    def find_native_session(self, native_id: str) -> Path | None:
        self.lookups.append(native_id)
        item = (
            self.projections.pop(0)
            if len(self.projections) > 1
            else self.projections[0]
        )
        self.current = item
        if item is None:
            return None
        project_name = self.project_name or claude_project_directory_name(
            item.cwd or ""
        )
        return (
            Path("C:/Users/test/.claude/projects") / project_name / f"{native_id}.jsonl"
        )

    def find_native_sessions(self, native_id: str) -> list[Path]:
        if self.duplicate_paths is not None:
            self.lookups.append(native_id)
            self.current = self.projections[0]
            return self.duplicate_paths
        found = self.find_native_session(native_id)
        return [] if found is None else [found]


    def parse(self, path: Path) -> FakeParse:
        if self.parse_error is not None:
            raise self.parse_error
        assert self.current is not None
        return FakeParse(
            self.current,
            malformed_lines=self.malformed_lines,
            unknown_records=self.unknown_records,
            entrypoint=self.entrypoint,
        )

    def projection_has_exact_marker(
        self, projection: SessionProjection, marker: str
    ) -> bool:
        return any(marker in (message.content or "") for message in projection.messages)


class ExactStemSource(FakeSource):
    def __init__(self) -> None:
        super().__init__()
        self.exact_stem_calls: list[str] = []

    def find_native_sessions_by_stem(self, native_id: str) -> list[Path]:
        self.exact_stem_calls.append(native_id)
        return []

    def find_native_sessions(self, native_id: str) -> list[Path]:
        raise AssertionError("registrar used the unbounded compatibility lookup")


class FakeStore:
    def __init__(self):
        self.calls: list[tuple[Any, ...]] = []

    def commit_claude_visibility_job(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("commit", *args))
        return {"state": "claude_visible"}

    def retry_claude_visibility_job(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("retry", *args))
        return {"state": "claude_retry"}

    def fail_claude_visibility_job(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("fail", *args))
        return {"state": "claude_failed"}

    def record_claude_visibility_exact_id_absent(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("absent", *args))
        return {"state": "claude_retry"}

    def retry_claude_auth_recovery(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("retry_auth_recovery", *args))
        return {"state": "retry"}

    def begin_claude_auth_recovery(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("begin_auth_recovery", *args))
        return {"state": "leased", "call_started_at": 100.0}


class FakePty:
    def __init__(
        self,
        output: str = "REGISTERED\r\n",
        exit_code: int = 0,
        read_error: Exception | None = None,
        ready_output: str = "\x1b[?2004h\x1b[2m⏵⏵ don't ask on\x1b[0m",
        ready_error: Exception | None = None,
        prompt_input_output: str = "[Pasted text #1 +12 lines]\r\n",
        prompt_input_error: Exception | None = None,
        write_error_at: int | None = None,
        wait_error: Exception | None = None,
    ):
        self.output = output
        self.exit_code = exit_code
        self.read_error = read_error
        self.ready_output = ready_output
        self.ready_error = ready_error
        self.prompt_input_output = prompt_input_output
        self.prompt_input_error = prompt_input_error
        self.write_error_at = write_error_at
        self.wait_error = wait_error
        self.writes: list[str] = []
        self.waits: list[float] = []
        self.terminated = False
        self.closed = False
        self.cleanup_result = PtyCleanupResult(True, True, True, exit_code)
        self.ready_waits: list[float] = []
        self.ready_trust_acceptances: list[bool] = []
        self.prompt_input_waits: list[tuple[float, str]] = []

    def read_until_ready(
        self, timeout: float, *, accept_workspace_trust: bool = False
    ) -> str:
        self.ready_waits.append(timeout)
        self.ready_trust_acceptances.append(accept_workspace_trust)
        if self.ready_error is not None:
            raise self.ready_error
        return self.ready_output

    def read_until(self, timeout: float, *, prompt: str | None = None) -> str:
        if self.read_error:
            raise self.read_error
        return self.output

    def read_until_prompt_input(self, timeout: float, *, prompt: str) -> str:
        self.prompt_input_waits.append((timeout, prompt))
        if self.prompt_input_error is not None:
            raise self.prompt_input_error
        return self.prompt_input_output

    def write(self, data: str) -> None:
        if self.write_error_at == len(self.writes):
            raise RuntimeError("PTY write failed")
        self.writes.append(data)

    def wait(self, timeout: float) -> int:
        self.waits.append(timeout)
        if self.wait_error is not None:
            raise self.wait_error
        return self.exit_code

    def terminate(self, timeout: float = 1.0) -> bool:
        self.terminated = True
        return True

    def close(self, timeout: float = 1.0) -> PtyCleanupResult:
        self.closed = True
        return self.cleanup_result


class FakeFactory:
    def __init__(self, process: FakePty | None = None, error: Exception | None = None):
        self.process = process or FakePty()
        self.error = error
        self.spawns: list[tuple[list[str], str]] = []

    def spawn(self, argv: list[str], *, cwd: str):
        self.spawns.append((argv, cwd))
        if self.error:
            raise self.error
        return self.process


def projection_for(
    item: ClaudeVisibilityClaim, *, response: str = "REGISTERED", **changes: Any
) -> SessionProjection:
    value = candidate()
    identity = derive_claude_visibility_identity(value, SECRET)
    prompt = build_claude_registration_prompt(value, identity, SECRET)
    base = SessionProjection(
        provider=Provider.CLAUDE,
        native_id=item.reserved_claude_uuid or "",
        title=item.native_name,
        cwd=item.source_cwd,
        started_at=10.0,
        last_active=11.0,
        messages=[
            ProjectedMessage("u1", 0, "user", prompt, 10.0),
            ProjectedMessage("a1", 0, "assistant", response, 11.0),
        ],
        native_path=str(
            Path("C:/Users/test/.claude/projects")
            / claude_project_directory_name(item.source_cwd or "")
            / f"{item.reserved_claude_uuid}.jsonl"
        ),
        native_hash="b" * 64,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=identity.bridge_id,
    )
    return replace(base, **changes)


def registrar(
    source: FakeSource,
    factory: FakeFactory,
    store: FakeStore | None = None,
    **kwargs: Any,
):
    startup_theme = kwargs.pop("startup_theme", "light")
    sleep = kwargs.pop("sleep", lambda _value: None)
    discovery_timeout = kwargs.pop("discovery_timeout", 0.0)
    return ClaudeNativeRegistrar(
        store or FakeStore(),
        source,
        marker_secret=SECRET,
        startup_theme=startup_theme,
        pty_factory=factory,
        clock=lambda: 100.0,
        monotonic=lambda: 1.0,
        sleep=sleep,
        process_timeout=2.0,
        exit_timeout=1.0,
        discovery_timeout=discovery_timeout,
        retry_delay=5.0,
        **kwargs,
    )


@pytest.mark.parametrize(
    "startup_theme",
    [None, "", "auto", "Light", "future-theme", {"theme": "light"}],
)
def test_registrar_rejects_noncanonical_startup_theme_before_spawn(
    startup_theme: Any,
) -> None:
    factory = FakeFactory()

    with pytest.raises(ValueError, match="invalid Claude startup theme"):
        registrar(FakeSource(), factory, startup_theme=startup_theme)

    assert factory.spawns == []


def test_launch_uses_interactive_mode_and_writes_prompt_then_exit() -> None:
    item = claim()
    process = FakePty(output="\x1b[?2004hClaude>\x1b[0m REGISTERED\r\n")
    factory = FakeFactory(process)
    source = FakeSource([None, projection_for(item)])
    result = registrar(source, factory).process(item)
    expected = build_claude_registration_prompt(
        candidate(), derive_claude_visibility_identity(candidate(), SECRET), SECRET
    )

    assert result.status == "visible"
    assert factory.spawns == [
        (
            [
                "claude",
                "--session-id",
                item.reserved_claude_uuid,
                "--name",
                item.native_name,
                "--settings",
                '{"theme":"light"}',
                "--setting-sources=",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--strict-mcp-config",
                "--no-chrome",
                "--model",
                "haiku",
                "--tools",
                "",
                "--permission-mode",
                "dontAsk",
            ],
            item.source_cwd,
        )
    ]
    argv = factory.spawns[0][0]
    assert "--print" not in argv and "-p" not in argv
    assert expected not in argv
    assert "--no-session-persistence" not in argv
    assert process.writes == [
        f"\x1b[200~{expected}\x1b[201~",
        "\r",
        "/exit\r",
    ]
    assert "tool_calls" not in expected
    assert process.ready_waits == [2.0]
    assert process.ready_trust_acceptances == [True]
    assert process.closed and process.waits == [1.0]


def test_launch_waits_for_multiline_paste_before_submitting_return() -> None:
    events: list[tuple[str, object]] = []

    class OrderedPty(FakePty):
        def write(self, data: str) -> None:
            events.append(("write", data))
            super().write(data)

        def read_until_prompt_input(self, timeout: float, *, prompt: str) -> str:
            events.append(("prompt_input", timeout))
            return super().read_until_prompt_input(timeout, prompt=prompt)

    item = claim()
    process = OrderedPty(output="REGISTERED\r\n")
    source = FakeSource([None, projection_for(item)])

    result = registrar(
        source,
        FakeFactory(process),
        sleep=lambda seconds: events.append(("sleep", seconds)),
    ).process(item)

    assert result.status == "visible"
    assert events[0][0] == "write"
    assert str(events[0][1]).startswith("\x1b[200~")
    assert events[1] == ("sleep", 0.5)
    assert events[2][0] == "prompt_input"
    assert events[3] == ("write", "\r")


def test_launch_accepts_exact_response_after_paste_auto_submit() -> None:
    item = claim()
    process = FakePty(
        output="REGISTERED\r\n",
        prompt_input_error=_PtyResponseTimeout("terminal_input_disabled"),
    )
    source = FakeSource([None, projection_for(item)])

    result = registrar(source, FakeFactory(process)).process(item)

    assert result.status == "visible"
    assert process.writes[0].startswith("\x1b[200~")
    assert "\r" not in process.writes
    assert process.writes[-1] == "/exit\r"


def test_launch_keeps_auto_submit_without_exact_response_ambiguous() -> None:
    item = claim()
    process = FakePty(
        output="input surface changed\r\n",
        prompt_input_error=_PtyResponseTimeout("terminal_input_disabled"),
    )
    source = FakeSource([None])

    result = registrar(source, FakeFactory(process)).process(item)

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert "\r" not in process.writes


@pytest.mark.parametrize(
    "prompt_input_output",
    [
        "[Pasted text #1 +12 lines]\r\nREGISTERED\r\n",
        (
            "[Pasted text #1 +12 lines]\r\n"
            "REGISTERED\r\n"
            "[Pasted text #1 +12 lines]\r\n"
        ),
    ],
)
def test_launch_accepts_auto_submitted_response_during_prompt_settle(
    prompt_input_output: str,
) -> None:
    item = claim()
    process = FakePty(
        prompt_input_output=prompt_input_output,
        read_error=_PtyResponseTimeout("no_response_output"),
    )
    source = FakeSource([None, projection_for(item)])

    result = registrar(source, FakeFactory(process)).process(item)

    assert result.status == "visible"
    assert "\r" not in process.writes
    assert process.writes[-1] == "/exit\r"


def test_launch_rejects_substantive_text_before_auto_submitted_response() -> None:
    item = claim()
    process = FakePty(
        prompt_input_output="unexpected text\r\nREGISTERED\r\n",
        read_error=_PtyResponseTimeout("no_response_output"),
    )
    source = FakeSource([None])

    result = registrar(source, FakeFactory(process)).process(item)

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert "\r" not in process.writes


def test_launch_keeps_unterminated_auto_submitted_response_ambiguous() -> None:
    item = claim()
    process = FakePty(
        prompt_input_output="[Pasted text #1 +12 lines]\r\nREGISTERED",
        output=" extra\r\n",
    )
    source = FakeSource([None])

    result = registrar(source, FakeFactory(process)).process(item)

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert "\r" not in process.writes
    assert "/exit\r" not in process.writes


def test_launch_untrusted_auto_submitted_wording_yields_to_exact_transcript() -> None:
    item = claim()
    process = FakePty(
        prompt_input_output="NOT REGISTERED\r\n",
        output="REGISTERED\r\n",
    )
    store = FakeStore()
    source = FakeSource([None, projection_for(item)])

    result = registrar(source, FakeFactory(process), store).process(item)

    assert result.status == "visible"
    assert [call[0] for call in store.calls] == ["commit"]
    assert "\r" not in process.writes
    assert "/exit\r" not in process.writes


def test_launch_malformed_auto_submitted_response_without_transcript_stays_ambiguous() -> None:
    item = claim()
    process = FakePty(
        prompt_input_output="NOT REGISTERED\r\n",
        output="REGISTERED\r\n",
    )
    store = FakeStore()

    result = registrar(FakeSource([None]), FakeFactory(process), store).process(item)

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert [call[0] for call in store.calls] == ["retry"]
    assert "\r" not in process.writes
    assert "/exit\r" not in process.writes


def test_winpty_waits_until_multiline_paste_is_visible_before_submit() -> None:
    class Process:
        def __init__(self) -> None:
            self.chunks = iter(
                [
                    "\x1b[?2004h\x1b[2m\u23f5\u23f5 don't ask on\x1b[0m",
                    "[Pasted text #1 +12 lines]\r\n",
                ]
            )

        def read_with_timeout(self, _size: int, _timeout: float) -> str | None:
            return next(self.chunks)

    output = _WinPtyProcess(Process()).read_until_prompt_input(
        1.0, prompt="multiline\nregistration\nprompt"
    )

    assert "[Pasted text #1 +12 lines]" in output


def test_winpty_accepts_current_claude_multiline_editor_hint() -> None:
    output = (
        "firstline\r\nsecond line\r\nthird line\r\n"
        "\x1b[?2004h\x1b[2m\u23f5\u23f5 don't ask on\x1b[0m"
        "ctrl+gtoeditinNotepad"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([output])

        def read_with_timeout(self, _size: int, _timeout: float) -> str | None:
            return next(self.chunks)

    observed = _WinPtyProcess(Process()).read_until_prompt_input(
        1.0, prompt="a much longer multiline registration prompt"
    )

    assert "ctrl+gtoeditinNotepad" in observed


def test_winpty_accepts_cursor_positioned_pasted_text_token() -> None:
    output = (
        "\x1b[2m[Pasted\x1b[1Ctext\x1b[1C#1\x1b[1C+6\x1b[1Clines]\x1b[0m"
        " paste again to expand"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([output])

        def read_with_timeout(self, _size: int, _timeout: float) -> str | None:
            return next(self.chunks)

    observed = _WinPtyProcess(Process()).read_until_prompt_input(
        1.0, prompt="a much longer multiline registration prompt"
    )

    assert "paste again to expand" in observed


def test_winpty_prompt_input_wait_drains_post_acceptance_redraw() -> None:
    class Process:
        def __init__(self) -> None:
            self.chunks = iter(
                [
                    "[Pastedtext#1+6lines] paste again to expand",
                    "\r\nlate redraw",
                ]
            )

        def read_with_timeout(self, _size: int, _timeout: float) -> str | None:
            return next(self.chunks)

    observed = _WinPtyProcess(Process()).read_until_prompt_input(
        1.0, prompt="a much longer multiline registration prompt"
    )

    assert "late redraw" in observed


def test_winpty_hands_auto_submitted_response_to_response_reader() -> None:
    class Process:
        def __init__(self) -> None:
            self.chunks = iter(["REGISTERED\r\n"])

        def read_with_timeout(self, _size: int, _timeout: float) -> str | None:
            return next(self.chunks, None)

    prompt = "a multiline registration prompt"
    process = _WinPtyProcess(Process())

    with pytest.raises(_PtyResponseTimeout) as exc_info:
        process.read_until_prompt_input(0.01, prompt=prompt)

    assert exc_info.value.reason == "terminal_input_disabled"
    assert process.read_until(1.0, prompt=prompt).strip() == "REGISTERED"


@pytest.mark.parametrize(
    ("process", "expected_writes"),
    [
        (FakePty(write_error_at=0), []),
        (FakePty(write_error_at=1), ["prompt"]),
        (FakePty(wait_error=TimeoutError()), ["prompt", "\r", "/exit\r"]),
    ],
)
def test_interactive_write_or_exit_uncertainty_is_creation_ambiguous(
    process: FakePty, expected_writes: list[str]
) -> None:
    item = claim()
    expected = build_claude_registration_prompt(
        candidate(), derive_claude_visibility_identity(candidate(), SECRET), SECRET
    )
    source = FakeSource([None])

    result = registrar(source, FakeFactory(process)).process(item)

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    normalized = [
        "prompt" if value == f"\x1b[200~{expected}\x1b[201~" else value
        for value in process.writes
    ]
    assert normalized == expected_writes
    assert process.terminated and process.closed


def test_malformed_interactive_response_never_sends_exit_command() -> None:
    process = FakePty(output="NOT REGISTERED")

    result = registrar(FakeSource(), FakeFactory(process)).process(claim())

    assert result.status == "failed"
    assert result.error_code == "bridge_conflict"
    assert len(process.writes) == 2
    assert process.writes[0].startswith("\x1b[200~")
    assert process.writes[1] == "\r"
    assert "/exit\r" not in process.writes
    assert process.terminated and process.closed


def test_tui_readiness_timeout_never_writes_registration_prompt() -> None:
    process = FakePty(ready_error=TimeoutError())

    result = registrar(FakeSource(), FakeFactory(process)).process(claim())

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert process.writes == []
    assert process.terminated and process.closed


def test_tui_readiness_timeout_preserves_bounded_phase_diagnostic() -> None:
    process = FakePty(
        ready_error=_PtyReadinessTimeout("known_input_modal"),
    )

    result = registrar(FakeSource(), FakeFactory(process)).process(claim())

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert result.detail == "Claude TUI readiness blocked: known_input_modal"
    assert process.writes == []
    assert process.terminated and process.closed


def test_tui_dialog_marker_without_main_repl_never_writes_registration_prompt() -> None:
    process = FakePty(
        ready_output=(
            "\x1b[?2004hAccessing workspace: Yes, I trust this folder "
            "No, exit Security guide"
        )
    )

    result = registrar(FakeSource(), FakeFactory(process)).process(claim())

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert process.writes == []
    assert process.terminated and process.closed


def test_tui_disabled_bracketed_paste_after_footer_never_writes_prompt() -> None:
    process = FakePty(
        ready_output=(
            "\x1b[?2004h\x1b[2m\u23f5\u23f5don't ask on\x1b[0m"
            "\x1b[?2004l"
        )
    )

    result = registrar(FakeSource(), FakeFactory(process)).process(claim())

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert process.writes == []
    assert process.terminated and process.closed


def test_tui_product_modal_after_footer_never_writes_prompt() -> None:
    process = FakePty(
        ready_output=(
            "\x1b[?2004h\x1b[2m\u23f5\u23f5don't ask on\x1b[0m"
            "\x1b[2JFable 5 is now a standard part of your Max plan\r\n"
            "1. Yes, try it\r\n2. Not now\r\n"
        )
    )

    result = registrar(FakeSource(), FakeFactory(process)).process(claim())

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert process.writes == []
    assert process.terminated and process.closed


@pytest.mark.parametrize(
    "ready_output",
    [
        (
            "\x1b[?2004h\x1b[2JFable 5 is now a standard part of your Max plan\r\n"
            "1. Yes, try it\r\n2. Not now\r\n"
            "\x1b[2m\u23f5\u23f5don't ask on\x1b[0m"
        ),
        (
            "\x1b[?2004h\x1b[2JFable 5 is now a standard part of your Max plan\r\n"
            "1. Yes, try it\r\n2. Not now\r\n"
            "\x1b[2m\u23f5\u23f5don't ask on\x1b[0m"
            "\x1b[2JFable 5 is now a standard part of your Max plan\r\n"
            "1. Yes, try it\r\n2. Not now\r\n"
            "\x1b[2m\u23f5\u23f5don't ask on\x1b[0m"
        ),
    ],
    ids=["modal-before-footer", "repeated-modal-footer-redraw"],
)
def test_tui_modal_history_before_latest_footer_never_writes_prompt(
    ready_output: str,
) -> None:
    process = FakePty(ready_output=ready_output)

    result = registrar(FakeSource(), FakeFactory(process)).process(claim())

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert process.writes == []
    assert process.terminated and process.closed


def test_print_mode_transcript_can_never_commit_native_visibility() -> None:
    item = claim()
    source = FakeSource(
        [None, projection_for(item)],
        entrypoint="sdk-cli",
    )

    result = registrar(source, FakeFactory()).process(item)

    assert result.status == "failed"
    assert result.error_code == "bridge_conflict"


def test_auth_recovery_resumes_exact_uuid_interactively_without_create() -> None:
    item = claim()
    prompt = "bounded same-UUID authentication recovery prompt"
    recovery = {
        "status": "claimed",
        "job_id": item.job_id,
        "reserved_claude_uuid": item.reserved_claude_uuid,
        "lease_digest": "b" * 64,
        "attempt_ordinal": 4,
        "operation_id": "6ae1c4de-0000-4000-8000-000000000001",
        "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "source_cwd": item.source_cwd,
    }
    process = FakePty(output="REGISTERED\r\n", exit_code=0)
    factory = FakeFactory(process)

    outcome = registrar(FakeSource(), factory).resume_auth_recovery(recovery, prompt)

    assert outcome.status == "recovered"
    assert outcome.reserved_claude_uuid == item.reserved_claude_uuid
    assert factory.spawns == [
        (
            [
                "claude",
                "--resume",
                item.reserved_claude_uuid,
                "--settings",
                '{"theme":"light"}',
                "--setting-sources=",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--strict-mcp-config",
                "--no-chrome",
                "--model",
                "haiku",
                "--tools",
                "",
                "--permission-mode",
                "dontAsk",
            ],
            item.source_cwd,
        )
    ]
    assert "--session-id" not in factory.spawns[0][0]
    assert "--print" not in factory.spawns[0][0]
    assert prompt not in factory.spawns[0][0]
    assert "--no-session-persistence" not in factory.spawns[0][0]
    assert process.writes == [
        f"\x1b[200~{prompt}\x1b[201~",
        "\r",
        "/exit\r",
    ]
    assert process.ready_trust_acceptances == [True]


def test_auth_recovery_accepts_exact_response_after_paste_auto_submit() -> None:
    item = claim()
    prompt = "bounded same-UUID authentication recovery prompt"
    recovery = {
        "status": "claimed",
        "job_id": item.job_id,
        "reserved_claude_uuid": item.reserved_claude_uuid,
        "lease_digest": "b" * 64,
        "attempt_ordinal": 4,
        "operation_id": "6ae1c4de-0000-4000-8000-000000000001",
        "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "source_cwd": item.source_cwd,
    }
    process = FakePty(
        output="REGISTERED\r\n",
        prompt_input_error=_PtyResponseTimeout("terminal_input_disabled"),
    )

    outcome = registrar(FakeSource(), FakeFactory(process)).resume_auth_recovery(
        recovery, prompt
    )

    assert outcome.status == "recovered"
    assert "\r" not in process.writes
    assert process.writes[-1] == "/exit\r"


def test_auth_recovery_keeps_auto_submit_without_exact_response_ambiguous() -> None:
    item = claim()
    prompt = "bounded same-UUID authentication recovery prompt"
    recovery = {
        "status": "claimed",
        "job_id": item.job_id,
        "reserved_claude_uuid": item.reserved_claude_uuid,
        "lease_digest": "b" * 64,
        "attempt_ordinal": 4,
        "operation_id": "6ae1c4de-0000-4000-8000-000000000001",
        "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "source_cwd": item.source_cwd,
    }
    process = FakePty(
        output="input surface changed\r\n",
        prompt_input_error=_PtyResponseTimeout("terminal_input_disabled"),
    )

    outcome = registrar(FakeSource(), FakeFactory(process)).resume_auth_recovery(
        recovery, prompt
    )

    assert outcome.status == "retry"
    assert outcome.error_code == "creation_ambiguous"
    assert "\r" not in process.writes


@pytest.mark.parametrize("phase", ["prompt_input", "response"])
def test_auth_recovery_keeps_provider_limit_transient(phase: str) -> None:
    item = claim()
    prompt = "bounded same-UUID authentication recovery prompt"
    recovery = {
        "status": "claimed",
        "job_id": item.job_id,
        "reserved_claude_uuid": item.reserved_claude_uuid,
        "lease_digest": "b" * 64,
        "attempt_ordinal": 4,
        "operation_id": "6ae1c4de-0000-4000-8000-000000000001",
        "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "source_cwd": item.source_cwd,
    }
    limited = "You've hit your limit · resets Jul 20, 4am"
    process = (
        FakePty(prompt_input_output=limited)
        if phase == "prompt_input"
        else FakePty(output=limited)
    )

    outcome = registrar(FakeSource(), FakeFactory(process)).resume_auth_recovery(
        recovery, prompt
    )

    assert outcome.status == "retry"
    assert outcome.error_code == "creation_ambiguous"


def test_auth_recovery_never_discards_malformed_auto_submitted_response() -> None:
    item = claim()
    prompt = "bounded same-UUID authentication recovery prompt"
    recovery = {
        "status": "claimed",
        "job_id": item.job_id,
        "reserved_claude_uuid": item.reserved_claude_uuid,
        "lease_digest": "b" * 64,
        "attempt_ordinal": 4,
        "operation_id": "6ae1c4de-0000-4000-8000-000000000001",
        "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "source_cwd": item.source_cwd,
    }
    process = FakePty(
        prompt_input_output="NOT REGISTERED\r\n",
        output="REGISTERED\r\n",
    )

    outcome = registrar(FakeSource(), FakeFactory(process)).resume_auth_recovery(
        recovery, prompt
    )

    assert outcome.status == "retry"
    assert outcome.error_code == "creation_ambiguous"
    assert "\r" not in process.writes
    assert "/exit\r" not in process.writes


def test_auth_recovery_durably_marks_call_started_before_spawn() -> None:
    item = claim()
    prompt = "bounded same-UUID authentication recovery prompt"
    recovery = {
        "status": "claimed",
        "job_id": item.job_id,
        "reserved_claude_uuid": item.reserved_claude_uuid,
        "lease_digest": "b" * 64,
        "attempt_ordinal": 4,
        "operation_id": "6ae1c4de-0000-4000-8000-000000000001",
        "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "source_cwd": item.source_cwd,
    }
    store = FakeStore()
    factory = FakeFactory(FakePty(output="REGISTERED\r\n", exit_code=0))

    outcome = registrar(FakeSource(), factory, store).resume_auth_recovery(
        recovery, prompt
    )

    assert outcome.status == "recovered"
    assert store.calls == [
        ("begin_auth_recovery", item.job_id, "b" * 64),
    ]
    assert len(factory.spawns) == 1


def test_auth_recovery_malformed_response_terminalizes_when_store_marks_fatal() -> None:
    class FatalStore(FakeStore):
        def retry_claude_auth_recovery(self, *args: Any) -> dict[str, Any]:
            self.calls.append(("retry_auth_recovery", *args))
            return {"state": "failed", "error_code": "bridge_conflict"}

    item = claim()
    prompt = "bounded same-UUID authentication recovery prompt"
    recovery = {
        "status": "claimed",
        "job_id": item.job_id,
        "reserved_claude_uuid": item.reserved_claude_uuid,
        "lease_digest": "b" * 64,
        "attempt_ordinal": 4,
        "operation_id": "6ae1c4de-0000-4000-8000-000000000001",
        "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "source_cwd": item.source_cwd,
    }
    store = FatalStore()

    outcome = registrar(
        FakeSource(), FakeFactory(FakePty(output="NOT REGISTERED", exit_code=0)), store
    ).resume_auth_recovery(recovery, prompt)

    assert outcome.status == "failed"
    assert outcome.error_code == "bridge_conflict"


def test_strict_projection_accepts_exact_2110_resume_scaffold() -> None:
    item = claim()
    value = candidate()
    identity = derive_claude_visibility_identity(value, SECRET)
    original_prompt = build_claude_registration_prompt(value, identity, SECRET)
    recovery_prompt = build_characterization_auth_recovery_prompt(
        item.reserved_claude_uuid or "", item.signed_marker or ""
    )
    messages = [
        ProjectedMessage("original", 0, "user", original_prompt, 10.0),
        ProjectedMessage(
            "auth",
            0,
            "assistant",
            "Failed to authenticate. API Error: 401 Invalid authentication credentials",
            11.0,
        ),
        ProjectedMessage("scaffold", 0, "assistant", "No response requested.", 12.0),
        ProjectedMessage("recovery-user", 0, "user", recovery_prompt, 13.0),
        ProjectedMessage("recovery-assistant", 0, "assistant", "REGISTERED", 14.0),
    ]
    projection = projection_for(item, messages=messages, last_active=14.0)
    store = FakeStore()

    result = registrar(FakeSource([projection]), FakeFactory(), store).process(item)

    assert result.status == "visible"
    assert store.calls[0][0] == "commit"


def test_terminal_echo_and_ansi_are_removed_before_exact_response_check() -> None:
    item = claim()
    expected = build_claude_registration_prompt(
        candidate(), derive_claude_visibility_identity(candidate(), SECRET), SECRET
    )
    echoed = "\r\n".join([
        f"\x1b[32mClaude>\x1b[0m {expected.splitlines()[0]}",
        *expected.splitlines()[1:],
    ])
    process = FakePty(output=f"{echoed}\r\n\x1b[32mREGISTERED\x1b[0m\r\n")
    result = registrar(
        FakeSource([None, projection_for(item)]), FakeFactory(process)
    ).process(item)
    assert result.status == "visible"


@pytest.mark.parametrize(
    "output", ["NOT REGISTERED", "REGISTERED later", "xREGISTERED", "REGISTERED\nextra"]
)
def test_registration_response_requires_exact_bounded_token(output: str) -> None:
    item = claim()
    store = FakeStore()
    process = FakePty(output=output)
    result = registrar(FakeSource(), FakeFactory(process), store).process(item)
    assert result.status == "failed"
    assert result.error_code == "bridge_conflict"
    assert store.calls[0][0] == "fail"
    assert process.closed and process.terminated


_VALID_AUTHORITIES = {
    ("launch", True, True, False),
    ("reconciliation", False, False, True),
}
_INVALID_AUTHORITIES = [
    authority
    for authority in product(
        ("launch", "reconciliation"),
        (False, True),
        (False, True),
        (False, True),
    )
    if authority not in _VALID_AUTHORITIES
] + [(None, True, True, False), ("launch", 1, True, False)]


@pytest.mark.parametrize("authority", _INVALID_AUTHORITIES)
def test_inconsistent_claim_authority_is_rejected_before_lookup_spawn_or_store(
    authority: tuple[Any, Any, Any, Any],
) -> None:
    lease_kind, launch_permitted, registration_reserved, requires_reconciliation = (
        authority
    )
    source = FakeSource()
    store = FakeStore()
    factory = FakeFactory()
    result = registrar(source, factory, store).process(
        claim(
            lease_kind=lease_kind,
            launch_permitted=launch_permitted,
            registration_reserved=registration_reserved,
            requires_exact_id_reconciliation=requires_reconciliation,
        )
    )
    assert result.status == "failed" and result.error_code == "bridge_conflict"
    assert factory.spawns == []
    assert source.lookups == []
    assert store.calls == []


def test_reconciliation_exact_match_commits_without_spawn() -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    store = FakeStore()
    factory = FakeFactory()
    result = registrar(FakeSource([projection_for(item)]), factory, store).process(item)
    assert result.status == "visible"
    assert factory.spawns == []
    assert store.calls[0][0] == "commit"


def test_reconciliation_absence_is_recorded_and_never_launches_same_cycle() -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    store = FakeStore()
    factory = FakeFactory()
    result = registrar(FakeSource([None]), factory, store).process(item)
    assert result.status == "absent"
    assert store.calls[0][0] == "absent"
    assert factory.spawns == []


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"native_id": "00000000-0000-4000-8000-000000000000"}, "uuid_conflict"),
        ({"title": "wrong"}, "name_conflict"),
        ({"cwd": "C:/wrong"}, "cwd_conflict"),
        ({"origin_bridge_id": "wrong"}, "bridge_conflict"),
    ],
)
def test_reconciliation_conflicts_fail(changes: dict[str, Any], code: str) -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    store = FakeStore()
    result = registrar(
        FakeSource([projection_for(item, **changes)]), FakeFactory(), store
    ).process(item)
    assert result.status == "failed" and result.error_code == code
    assert store.calls[0][0] == "fail"


def test_reconciliation_fails_an_exact_uuid_with_wrong_authenticated_marker() -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    projection = projection_for(item)
    messages = list(projection.messages)
    messages[0] = replace(messages[0], content="forged registration prompt")
    store = FakeStore()
    result = registrar(
        FakeSource([replace(projection, messages=messages)]), FakeFactory(), store
    ).process(item)
    assert result.status == "failed" and result.error_code == "marker_conflict"


def test_registration_prompt_must_pair_with_immediate_exact_assistant_reply() -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    projection = projection_for(item)
    prompt_message = projection.messages[0]
    messages = [
        prompt_message,
        replace(projection.messages[1], content="WRONG"),
        replace(prompt_message, native_event_id="u2", content="unrelated user turn"),
        replace(projection.messages[1], native_event_id="a2", content="REGISTERED"),
    ]
    result = registrar(
        FakeSource([replace(projection, messages=messages)]), FakeFactory()
    ).process(item)
    assert result.status == "failed" and result.error_code == "bridge_conflict"


def test_registration_turn_aggregates_split_text_blocks_from_same_assistant_event() -> (
    None
):
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    projection = projection_for(item)
    assistant = projection.messages[1]
    messages = [
        projection.messages[0],
        replace(assistant, ordinal=0, content="REGIS"),
        replace(assistant, ordinal=1, content="TERED"),
    ]
    result = registrar(
        FakeSource([replace(projection, messages=messages)]), FakeFactory()
    ).process(item)
    assert result.status == "visible"


def test_registration_turn_rejects_extra_block_in_same_assistant_event() -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    projection = projection_for(item)
    assistant = projection.messages[1]
    messages = [
        projection.messages[0],
        assistant,
        replace(assistant, ordinal=1, content="extra"),
    ]
    result = registrar(
        FakeSource([replace(projection, messages=messages)]), FakeFactory()
    ).process(item)
    assert result.status == "failed" and result.error_code == "bridge_conflict"


def test_exact_transcript_must_use_windows_encoded_source_project_directory() -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    expected = claude_project_directory_name(item.source_cwd or "")
    assert expected == "C--exact-project-subdir"
    assert (
        registrar(FakeSource([projection_for(item)]), FakeFactory())
        .process(item)
        .status
        == "visible"
    )

    wrong = registrar(
        FakeSource([projection_for(item)], project_name="C--wrong-project"),
        FakeFactory(),
    ).process(replace(item, lease_digest="c" * 64))
    assert wrong.status == "failed" and wrong.error_code == "cwd_conflict"


def test_paid_exact_path_parse_failure_is_terminal_and_never_spawns() -> None:
    item = claim()
    store = FakeStore()
    factory = FakeFactory()
    result = registrar(
        FakeSource([projection_for(item)], parse_error=ValueError("identity changed")),
        factory,
        store,
    ).process(item)
    assert result.status == "failed" and result.error_code == "bridge_conflict"
    assert factory.spawns == []
    assert store.calls[0][0] == "fail"


@pytest.mark.parametrize(
    "source",
    [
        lambda projection: FakeSource([projection], malformed_lines=1),
        lambda projection: FakeSource([projection], unknown_records=1),
    ],
)
def test_registration_transcript_rejects_malformed_or_unknown_records(
    source: Any,
) -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    result = registrar(source(projection_for(item)), FakeFactory()).process(item)
    assert result.status == "failed" and result.error_code == "bridge_conflict"


def test_registration_transcript_rejects_any_unrelated_projected_message() -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    projection = projection_for(item)
    extra = replace(
        projection.messages[1], native_event_id="later", content="unrelated work"
    )
    result = registrar(
        FakeSource([replace(projection, messages=[*projection.messages, extra])]),
        FakeFactory(),
    ).process(item)
    assert result.status == "failed" and result.error_code == "bridge_conflict"


@pytest.mark.parametrize(
    "response",
    [
        "You've hit your limit · resets Jul 20, 4am "
        "(America/New_York)\nAPI Error: 429 rate_limit",
        "You\u2019ve hit your weekly limit · resets Aug 3, 4am "
        "(America/New_York)",
        "You've hit your weekly limit \u00c2\u00b7 resets Aug 3, 4am "
        "(America/New_York)",
    ],
)
def test_exact_uuid_provider_limit_transcript_reconciles_without_replacement(
    response: str,
) -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    projection = projection_for(item, response=response)
    factory = FakeFactory()
    result = registrar(FakeSource([projection]), factory).process(item)

    assert result.status == "visible"
    assert factory.spawns == []


def test_exact_uuid_provider_limit_before_persisted_prompt_reconciles_without_replacement() -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    limited = "You've hit your weekly limit · resets Aug 24, 4am (America/New_York)"
    projection = projection_for(item, response=limited)
    prompt, response = projection.messages
    projection = replace(projection, messages=[response, prompt])
    factory = FakeFactory()

    result = registrar(FakeSource([projection]), factory).process(item)

    assert result.status == "visible"
    assert factory.spawns == []


@pytest.mark.parametrize(
    "messages",
    [
        lambda prompt, limited: [
            replace(prompt, role="assistant", content=limited),
            replace(prompt, native_event_id=prompt.native_event_id),
        ],
        lambda prompt, limited: [
            replace(prompt, role="assistant", content=limited),
            prompt,
            replace(prompt, native_event_id="later", content="unrelated work"),
        ],
        lambda prompt, limited: [
            replace(prompt, role="assistant", content=limited, reasoning="hidden"),
            prompt,
        ],
        lambda prompt, limited: [
            replace(
                prompt,
                role="assistant",
                native_event_id="provider-limit",
                content=f"{limited}\nUNEXPECTED TRAILING TEXT",
            ),
            prompt,
        ],
    ],
)
def test_provider_limit_before_prompt_does_not_bypass_strict_transcript_shape(
    messages: Any,
) -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    projection = projection_for(item)
    prompt = projection.messages[0]
    limited = "You've hit your weekly limit · resets Aug 24, 4am (America/New_York)"
    projection = replace(projection, messages=messages(prompt, limited))

    result = registrar(FakeSource([projection]), FakeFactory()).process(item)

    assert result.status == "failed"
    assert result.error_code == "bridge_conflict"


@pytest.mark.parametrize(
    "response",
    [
        "You've hit your weekly limit? No; usage resets tomorrow.",
        'Assistant quoted: "You\u2019ve hit your weekly limit · resets tomorrow."',
        "You've hit your weekly limits · resets tomorrow.",
    ],
)
def test_exact_uuid_non_limit_text_never_bypasses_registered_response(
    response: str,
) -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    factory = FakeFactory()

    result = registrar(
        FakeSource([projection_for(item, response=response)]),
        factory,
    ).process(item)

    assert result.status == "failed"
    assert result.error_code == "bridge_conflict"
    assert factory.spawns == []


def test_launch_provider_limit_reconciles_created_exact_uuid_in_same_cycle() -> None:
    item = claim()
    limited = "You've hit your limit · resets Jul 20, 4am " \
        "(America/New_York)\nAPI Error: 429 rate_limit"
    source = FakeSource([None, projection_for(item, response=limited)])
    factory = FakeFactory(FakePty(output=limited))

    result = registrar(source, factory).process(item)

    assert result.status == "visible"
    assert len(factory.spawns) == 1
    assert source.lookups == [item.reserved_claude_uuid, item.reserved_claude_uuid]


def test_launch_current_session_limit_wording_retries_without_waiting_for_timeout() -> None:
    item = claim()
    limited = (
        "You've hit your session limit · resets 6:50pm (America/New_York)"
    )
    source = FakeSource([None, None])
    factory = FakeFactory(FakePty(output=limited))

    result = registrar(source, factory).process(item)

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert result.detail == "Claude provider limit interrupted registration"
    assert len(factory.spawns) == 1
    assert source.lookups == [item.reserved_claude_uuid, item.reserved_claude_uuid]


@pytest.mark.parametrize("apostrophe", ["'", "\u2019"])
def test_launch_current_weekly_limit_wording_retries_without_waiting_for_timeout(
    apostrophe: str,
) -> None:
    item = claim()
    limited = (
        f"You{apostrophe}ve hit your weekly limit · "
        "resets Aug 3, 4am (America/New_York)"
    )
    source = FakeSource([None, None])
    factory = FakeFactory(FakePty(output=limited))

    result = registrar(source, factory).process(item)

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert result.detail == "Claude provider limit interrupted registration"
    assert len(factory.spawns) == 1
    assert source.lookups == [item.reserved_claude_uuid, item.reserved_claude_uuid]


@pytest.mark.parametrize(
    "messages",
    [
        lambda projection: [
            replace(
                projection.messages[0], native_event_id="earlier", content="old work"
            ),
            *projection.messages,
        ],
        lambda projection: [
            projection.messages[0],
            replace(projection.messages[1], ordinal=1, content="REGISTERED"),
        ],
        lambda projection: [
            projection.messages[0],
            projection.messages[1],
            replace(projection.messages[1], content=""),
        ],
    ],
)
def test_registration_transcript_rejects_extra_turns_and_bad_block_ordinals(
    messages: Any,
) -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    projection = projection_for(item)
    result = registrar(
        FakeSource([replace(projection, messages=messages(projection))]), FakeFactory()
    ).process(item)
    assert result.status == "failed" and result.error_code == "bridge_conflict"


def test_duplicate_exact_uuid_is_fatal_before_spawn_or_commit() -> None:
    item = claim()
    project = claude_project_directory_name(item.source_cwd or "")
    paths = [
        Path("C:/Users/test/.claude/projects")
        / project
        / f"{item.reserved_claude_uuid}.jsonl",
        Path("D:/other/.claude/projects/C--other")
        / f"{item.reserved_claude_uuid}.jsonl",
    ]
    store = FakeStore()
    factory = FakeFactory()
    result = registrar(
        FakeSource([projection_for(item)], duplicate_paths=paths), factory, store
    ).process(item)
    assert result.status == "failed" and result.error_code == "duplicate_uuid"
    assert factory.spawns == [] and store.calls[0][0] == "fail"


def test_reconciliation_uses_authoritative_stem_lookup_without_legacy_probe() -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    source = ExactStemSource()
    store = FakeStore()

    result = registrar(source, FakeFactory(), store).process(item)

    assert result.status == "absent"
    assert source.exact_stem_calls == [item.reserved_claude_uuid]
    assert store.calls[0][0] == "absent"


def test_reconciliation_can_refuse_launch_authorizing_exact_absence() -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    source = ExactStemSource()
    store = FakeStore()
    factory = FakeFactory()

    result = registrar(source, factory, store).process(item, allow_absence=False)

    assert result.status == "absent"
    assert result.error_code == "native_transcript_not_indexed"
    assert source.exact_stem_calls == [item.reserved_claude_uuid]
    assert factory.spawns == []
    assert store.calls == []


def test_delayed_exact_transcript_is_polled_without_replacement() -> None:
    item = claim()
    source = FakeSource([None, projection_for(item)])
    factory = FakeFactory()
    ticks = iter([0.0, 0.0, 0.1, 0.1, 0.2, 0.2])
    reg = ClaudeNativeRegistrar(
        FakeStore(),
        source,
        marker_secret=SECRET,
        startup_theme="light",
        pty_factory=factory,
        clock=lambda: 100.0,
        monotonic=lambda: next(ticks),
        sleep=lambda _: None,
        process_timeout=2,
        exit_timeout=1,
        discovery_timeout=1,
        retry_delay=5,
    )
    result = reg.process(item)
    assert result.status == "visible"
    assert source.lookups == [item.reserved_claude_uuid, item.reserved_claude_uuid]
    assert len(factory.spawns) == 1


class _TranscriptCreatingLimitPty(FakePty):
    """Persist a strict provider-limit transcript during PTY interaction."""

    def __init__(self, projects_root: Path, item: ClaudeVisibilityClaim) -> None:
        super().__init__()
        self._projects_root = projects_root
        self._item = item

    def read_until_ready(
        self, timeout: float, *, accept_workspace_trust: bool = False
    ) -> str:
        self.write_exact_transcript()
        return super().read_until_ready(timeout, accept_workspace_trust=True)

    def write_exact_transcript(self) -> None:
        value = candidate()
        identity = derive_claude_visibility_identity(value, SECRET)
        prompt = build_claude_registration_prompt(value, identity, SECRET)
        project = claude_project_directory_name(self._item.source_cwd or "")
        directory = self._projects_root / project
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._item.reserved_claude_uuid}.jsonl"
        limited = (
            "You've hit your weekly limit · resets Aug 24, 4am (America/New_York)"
        )
        records = [
            {
                "type": "custom-title",
                "sessionId": self._item.reserved_claude_uuid,
                "customTitle": self._item.native_name,
            },
            {
                "type": "assistant",
                "sessionId": self._item.reserved_claude_uuid,
                "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "timestamp": "2026-08-22T00:00:00Z",
                "cwd": self._item.source_cwd,
                "gitBranch": self._item.git_branch,
                "isSidechain": False,
                "entrypoint": "cli",
                "message": {"role": "assistant", "content": limited},
            },
            {
                "type": "user",
                "sessionId": self._item.reserved_claude_uuid,
                "uuid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "timestamp": "2026-08-22T00:00:01Z",
                "cwd": self._item.source_cwd,
                "gitBranch": self._item.git_branch,
                "isSidechain": False,
                "entrypoint": "cli",
                "message": {"role": "user", "content": prompt},
            },
        ]
        path.write_bytes(
            b"".join(
                json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
                for record in records
            )
        )


def test_launch_provider_limit_freshly_discovers_exact_transcript_after_cached_empty_lookup(
    tmp_path: Path,
) -> None:
    item = claim()
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    store = FakeStore()
    process = _TranscriptCreatingLimitPty(tmp_path, item)
    factory = FakeFactory(process)

    result = registrar(adapter, factory, store).process(item)

    assert result.status == "visible"
    assert result.error_code is None
    assert len(factory.spawns) == 1
    assert process.terminated is False
    assert process.closed is True
    expected = (
        tmp_path
        / claude_project_directory_name(item.source_cwd or "")
        / f"{item.reserved_claude_uuid}.jsonl"
    )
    assert expected.exists()
    assert [call[0] for call in store.calls] == ["commit"]


class _AmbiguousTranscriptPty(_TranscriptCreatingLimitPty):
    """Persist a valid transcript but end the PTY observation ambiguously."""

    def read_until_ready(
        self, timeout: float, *, accept_workspace_trust: bool = False
    ) -> str:
        self.write_exact_transcript()
        return super().read_until_ready(timeout, accept_workspace_trust=True)

    def read_until(self, timeout: float, *, prompt: str | None = None) -> str:
        raise TimeoutError("registration observation ended ambiguously")


def test_launch_ambiguous_response_still_reconciles_exact_transcript_after_verified_cleanup(
    tmp_path: Path,
) -> None:
    item = claim()
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    store = FakeStore()
    process = _AmbiguousTranscriptPty(tmp_path, item)
    factory = FakeFactory(process)

    result = registrar(adapter, factory, store).process(item)

    assert result.status == "visible"
    assert len(factory.spawns) == 1
    assert process.terminated is True
    assert process.closed is True
    assert [call[0] for call in store.calls] == ["commit"]


def test_ambiguous_polling_without_transcript_keeps_original_creation_ambiguity(
    tmp_path: Path,
) -> None:
    item = claim()
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    store = FakeStore()
    process = FakePty(prompt_input_error=_PtyResponseTimeout("blocked"))

    result = registrar(adapter, factory=FakeFactory(process), store=store).process(item)

    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert [call[0] for call in store.calls] == ["retry"]


@pytest.mark.parametrize(
    ("factory", "process", "code"),
    [
        (FakeFactory(error=FileNotFoundError()), None, "claude_executable_unavailable"),
        (FakeFactory(error=RuntimeError("pty unavailable")), None, "pty_unavailable"),
        (
            None,
            FakePty(output="Authentication required"),
            "claude_authentication_unavailable",
        ),
        (
            None,
            FakePty(
                output="Failed to authenticate. API Error: 401 Invalid authentication credentials"
            ),
            "claude_authentication_unavailable",
        ),
        (
            None,
            FakePty(
                output="You've hit your limit · resets Jul 20, 4am "
                "(America/New_York)\nAPI Error: 429 rate_limit"
            ),
            "creation_ambiguous",
        ),
        (None, FakePty(exit_code=7), "clean_exit_not_observed"),
        (None, FakePty(read_error=TimeoutError()), "creation_ambiguous"),
    ],
)
def test_fixed_launch_failure_codes_and_cleanup(
    factory: FakeFactory | None, process: FakePty | None, code: str
) -> None:
    item = claim()
    factory = factory or FakeFactory(process)
    result = registrar(FakeSource(), factory).process(item)
    assert result.error_code == code
    if process is not None:
        assert process.closed and process.terminated
    assert result.detail not in {"pty unavailable", "Authentication required"}


# Ceiling for in-process reader tests whose fake stream ENDS (StopIteration ->
# EOF).  EOF is what returns the read, so this is a guard and never the operative
# deadline.  A ceiling below _RESPONSE_SETTLE_SECONDS (0.5) instead makes itself
# the deadline, turning the assertion into a race against the fake's own sleeps.
# Tests that deliberately assert ON the deadline -- e.g.
# test_winpty_slow_drip_after_candidate_stays_bounded_by_global_timeout, which
# checks `elapsed < 1.0` -- keep their own small value and must NOT use this.
_READER_EOF_GUARD_SECONDS = 30.0


def test_winpty_fallback_reader_observes_cancellation_while_read_is_blocked() -> None:
    stop = threading.Event()
    read_started = threading.Event()
    release_read = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    class Process:
        def read(self, _size: int = 1024) -> str:
            read_started.set()
            release_read.wait()
            raise EOFError

    wrapped = _WinPtyProcess(Process())
    wrapped.set_cancel_event(stop)

    def read() -> None:
        try:
            wrapped.read_until(30.0)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    reader = threading.Thread(target=read)
    reader.start()
    try:
        assert read_started.wait(1.0)
        stop.set()
        assert finished.wait(1.0)
    finally:
        release_read.set()
        reader.join(2.0)

    assert reader.is_alive() is False
    assert len(errors) == 1
    assert str(errors[0]) == "visibility registrar cancelled"


def test_winpty_timed_read_observes_bound_cancellation() -> None:
    stop = threading.Event()

    class Process:
        def read_with_timeout(self, _size: int, _timeout: float) -> None:
            stop.set()
            return None

    wrapped = _WinPtyProcess(Process())
    wrapped.set_cancel_event(stop)

    with pytest.raises(RuntimeError, match="^visibility registrar cancelled$"):
        wrapped.read_until(30.0)


def test_winpty_wait_observes_bound_cancellation() -> None:
    stop = threading.Event()

    class Process:
        exitstatus = None

        def isalive(self) -> bool:
            stop.set()
            return True

    wrapped = _WinPtyProcess(Process())
    wrapped.set_cancel_event(stop)

    with pytest.raises(RuntimeError, match="^visibility registrar cancelled$"):
        wrapped.wait(30.0)


def test_winpty_wrapper_uses_real_read_signature_without_unbounded_keyword() -> None:
    class Process:
        def __init__(self):
            self.calls = 0

        def read(self, size: int = 1024) -> str:
            self.calls += 1
            if self.calls > 1:
                raise EOFError
            return "REGISTERED\r\n"

    process = Process()
    assert _WinPtyProcess(process).read_until(0.2).strip() == "REGISTERED"
    assert process.calls == 2


def test_winpty_reader_does_not_stop_on_registered_text_inside_prompt_echo() -> None:
    value = candidate()
    prompt = build_claude_registration_prompt(
        value, derive_claude_visibility_identity(value, SECRET), SECRET
    )

    class Process:
        def __init__(self):
            self.chunks = iter([prompt + "\r\n", "REGISTERED\r\n"])
            self.calls = 0

        def read(self, size: int = 1024) -> str:
            self.calls += 1
            if self.calls == 2:
                time.sleep(0.01)
            return next(self.chunks)

    process = Process()
    output = _WinPtyProcess(process).read_until(
        _READER_EOF_GUARD_SECONDS, prompt=prompt
    )
    assert output.strip() == "REGISTERED"
    assert process.calls == 3


def test_winpty_reader_ignores_startup_chrome_and_wrapped_prompt_fragments() -> None:
    value = candidate()
    prompt = build_claude_registration_prompt(
        value, derive_claude_visibility_identity(value, SECRET), SECRET
    )

    class Process:
        def __init__(self):
            self.chunks = iter([
                "Claude Code ready\r\nstatus: connected\r\n",
                "Signed marker: wrapped-fragment\r\nmetadata continuation\r\n",
                "\x1b[32mClaude>\x1b[0m REGISTERED\r\n",
            ])
            self.calls = 0

        def read(self, size: int = 1024) -> str:
            self.calls += 1
            return next(self.chunks)

    process = Process()
    output = _WinPtyProcess(process).read_until(0.2, prompt=prompt)
    assert output.strip() == "REGISTERED"
    assert process.calls == 4


def test_winpty_reader_never_treats_authentication_words_in_echo_as_failure() -> None:
    class Process:
        def __init__(self):
            self.chunks = iter([
                "Bounded metadata: authentication required\r\n",
                "REGISTERED\r\n",
            ])
            self.calls = 0

        def read(self, size: int = 1024) -> str:
            self.calls += 1
            return next(self.chunks)

    process = Process()
    output = _WinPtyProcess(process).read_until(
        0.2, prompt="Bounded metadata: authentication required"
    )
    assert output.strip() == "REGISTERED"
    assert process.calls == 3


def test_winpty_timed_reader_returns_live_provider_limit_without_global_timeout() -> None:
    limited = "You've hit your session limit · resets 6:50pm (America/New_York)"

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([limited])
            self.calls = 0

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            self.calls += 1
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

    process = Process()
    started = time.monotonic()
    output = _WinPtyProcess(process).read_until(
        1.0, prompt="registration prompt"
    )

    assert output.strip() == limited
    assert time.monotonic() - started < 0.5
    assert process.calls == 1


def test_winpty_response_timeout_reports_bounded_main_repl_phase() -> None:
    prompt = "registration prompt"

    class Process:
        def __init__(self) -> None:
            self.chunks = iter(
                [
                    prompt
                    + "\r\n\x1b[?2004h\x1b[2m\u23f5\u23f5 don't ask on\x1b[0m"
                ]
            )

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

    with pytest.raises(_PtyResponseTimeout) as failure:
        _WinPtyProcess(Process()).read_until(0.05, prompt=prompt)

    assert failure.value.reason == "main_repl_after_prompt"


def test_winpty_response_timeout_reports_visible_pasted_input() -> None:
    output = (
        "[Pasted text #1 +12 lines]\r\n"
        "\x1b[?2004h\x1b[2m\u23f5\u23f5 don't ask on\x1b[0m"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([output])

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

    with pytest.raises(_PtyResponseTimeout) as failure:
        _WinPtyProcess(Process()).read_until(0.05, prompt="registration prompt")

    assert failure.value.reason == "pasted_input_visible"


def test_winpty_reader_drains_extra_output_after_registered_before_acceptance() -> None:
    class Process:
        def __init__(self):
            self.chunks = iter(["REGISTERED\r\n", "extra\r\n"])
            self.calls = 0

        def read(self, size: int = 1024) -> str:
            self.calls += 1
            if self.calls == 2:
                time.sleep(0.08)
            return next(self.chunks)

    process = Process()
    output = _WinPtyProcess(process).read_until(_READER_EOF_GUARD_SECONDS)
    assert output.strip().splitlines() == ["REGISTERED", "extra"]
    assert process.calls >= 2


def test_winpty_quiet_period_resets_for_each_partial_post_response_chunk() -> None:
    class Process:
        def __init__(self):
            self.chunks = iter(["REGISTERED\r\n", "ex", "tra\r\n"])
            self.calls = 0

        def read(self, size: int = 1024) -> str:
            self.calls += 1
            if self.calls in {2, 3}:
                time.sleep(0.08)
            return next(self.chunks)

    process = Process()
    output = _WinPtyProcess(process).read_until(_READER_EOF_GUARD_SECONDS)
    assert output.strip().splitlines() == ["REGISTERED", "extra"]
    assert process.calls == 4


def test_winpty_retains_substantive_pre_response_output_for_rejection() -> None:
    class Process:
        def __init__(self):
            self.chunks = iter(["UNRELATED WORK\r\n", "REGISTERED\r\n"])

        def read(self, size: int = 1024) -> str:
            return next(self.chunks)

    output = _WinPtyProcess(Process()).read_until(0.3, prompt="registration prompt")
    assert "UNRELATED WORK" in output


def test_winpty_slow_drip_after_candidate_stays_bounded_by_global_timeout() -> None:
    release = threading.Event()

    class Process:
        def __init__(self):
            self.chunks = iter(["REGISTERED\r\n", "e", "x", "t"])

        def read(self, size: int = 1024) -> str:
            try:
                chunk = next(self.chunks)
            except StopIteration:
                release.wait(2)
                raise EOFError
            time.sleep(0.06)
            return chunk

    started = time.monotonic()
    output = _WinPtyProcess(Process()).read_until(0.22)
    elapsed = time.monotonic() - started
    release.set()
    assert output.startswith("REGISTERED")
    # The regression this guards against waits the full two-second release
    # timeout. Leave enough scheduling margin for a loaded Windows test host
    # while still proving the read is bounded well below that blocking wait.
    assert elapsed < 1.0


def test_winpty_reader_accepts_registered_split_across_chunks() -> None:
    class Process:
        def __init__(self):
            self.chunks = iter(["REGIS", "TERED\r\n"])

        def read(self, size: int = 1024) -> str:
            return next(self.chunks)

    assert _WinPtyProcess(Process()).read_until(0.2).strip() == "REGISTERED"


def _close_raw_registrar_process(process: Any) -> None:
    process.stop_transport()
    process.fileobj.close()
    process._server.close()
    process.release_native_pty()


def test_raw_winpty_read_exception_after_exit_preserves_accumulated_output() -> None:
    class Pty:
        pid = 123

        def __init__(self) -> None:
            self.reads = iter(["Authentication required\r\n", OSError("closed")])

        def read(self, size: int, *, blocking: bool) -> str:
            del size, blocking
            value = next(self.reads)
            if isinstance(value, Exception):
                raise value
            return value

        def isalive(self) -> bool:
            return False

    process = _registrar_pywinpty_process_type()(Pty())
    try:
        output = _WinPtyProcess(process).read_until(0.2)
        assert output.strip() == "Authentication required"
    finally:
        _close_raw_registrar_process(process)


def test_raw_winpty_read_exception_while_alive_remains_an_error() -> None:
    class Pty:
        pid = 123

        def read(self, size: int, *, blocking: bool) -> str:
            del size, blocking
            raise OSError("real read failure")

        def isalive(self) -> bool:
            return True

    process = _registrar_pywinpty_process_type()(Pty())
    try:
        with pytest.raises(OSError, match="real read failure"):
            process.read_with_timeout(4096, 0.01)
    finally:
        _close_raw_registrar_process(process)


def test_raw_winpty_empty_read_after_exit_is_eof() -> None:
    class Pty:
        pid = 123

        def read(self, size: int, *, blocking: bool) -> str:
            del size, blocking
            return ""

        def isalive(self) -> bool:
            return False

    process = _registrar_pywinpty_process_type()(Pty())
    try:
        with pytest.raises(EOFError):
            process.read_with_timeout(4096, 0.01)
    finally:
        _close_raw_registrar_process(process)


def test_winpty_readiness_ignores_conpty_prologue_and_requires_main_repl() -> None:
    class Process:
        def __init__(self) -> None:
            self.chunks = iter(
                [
                    "\x1b[?9001h\x1b[?1004h\x1b[?25l\x1b[2J\x1b[m\x1b[H",
                    "\x1b[?20",
                    "04h",
                    "\x1b[2m⏵⏵ don't ask on\x1b[0m",
                ]
            )

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

    output = _WinPtyProcess(Process()).read_until_ready(1.0)

    assert output.startswith("\x1b[?9001h\x1b[?1004h\x1b[?25l")
    assert "\x1b[?2004h" in output
    assert "⏵⏵ don't ask on" in output


def test_winpty_readiness_accepts_claude_216_compact_main_footer() -> None:
    class Process:
        def __init__(self) -> None:
            self.chunks = iter([
                "\x1b[?9001h\x1b[?1004h\x1b[?25l\x1b[2J\x1b[m\x1b[H",
                "\x1b[?2004h",
                "\x1b[2m\u23f5\u23f5don't ask on (shift+tab to cycle)\x1b[0m",
            ])

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

    output = _WinPtyProcess(Process()).read_until_ready(1.0)

    assert "\x1b[?2004h" in output
    assert "\u23f5\u23f5don't ask on" in output


def test_winpty_readiness_accepts_claude_219_permission_indicator() -> None:
    class Process:
        def __init__(self) -> None:
            self.chunks = iter([
                "\x1b[?9001h\x1b[?1004h\x1b[?25l\x1b[2J\x1b[m\x1b[H",
                "\x1b[?2004h",
                "\x1b[2m\u23f5\u23f5 Don't Ask (shift+tab to cycle)\x1b[0m",
            ])

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

    output = _WinPtyProcess(Process()).read_until_ready(1.0)

    assert "\x1b[?2004h" in output
    assert "\u23f5\u23f5 Don't Ask" in output


def test_winpty_readiness_accepts_claude_219_compact_permission_indicator() -> None:
    class Process:
        def __init__(self) -> None:
            self.chunks = iter([
                "\x1b[?9001h\x1b[?1004h\x1b[?25l\x1b[2J\x1b[m\x1b[H",
                "\x1b[?2004h",
                "\x1b[2m\u23f5\u23f5 DontAsk\x1b[0m",
            ])

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

    output = _WinPtyProcess(Process()).read_until_ready(1.0)

    assert "\x1b[?2004h" in output
    assert "\u23f5\u23f5 DontAsk" in output


def test_winpty_readiness_accepts_symbol_only_permission_indicator() -> None:
    class Process:
        def __init__(self) -> None:
            self.chunks = iter([
                "\x1b[?9001h\x1b[?1004h\x1b[?25l\x1b[2J\x1b[m\x1b[H",
                "\x1b[?2004h",
                "\x1b[2m\u23f5\u23f5\x1b[0m",
            ])

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

    output = _WinPtyProcess(Process()).read_until_ready(1.0)

    assert "\x1b[?2004h" in output
    assert "\u23f5\u23f5" in output


def test_winpty_readiness_waits_for_footer_then_product_modal() -> None:
    modal = (
        "\x1b[2JFable 5 is now a standard part of your Max plan\r\n"
        "1. Yes, try it\r\n2. Not now\r\n"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([
                "\x1b[?2004h\x1b[2m\u23f5\u23f5don't ask on\x1b[0m",
                modal,
            ])
            self.reads = 0

        def read_with_timeout(self, _size: int, _timeout: float) -> str:
            self.reads += 1
            try:
                return next(self.chunks)
            except StopIteration as exc:
                raise EOFError from exc

    process = Process()
    with pytest.raises(RuntimeError, match="closed before readiness"):
        _WinPtyProcess(process).read_until_ready(1.0)

    assert process.reads == 3


def test_winpty_readiness_rejects_footer_and_product_modal_in_same_chunk() -> None:
    modal = (
        "\x1b[2JFable 5 is now a standard part of your Max plan\r\n"
        "1. Yes, try it\r\n2. Not now\r\n"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([
                "\x1b[?2004h\x1b[2m\u23f5\u23f5don't ask on\x1b[0m" + modal
            ])
            self.reads = 0

        def read_with_timeout(self, _size: int, _timeout: float) -> str:
            self.reads += 1
            try:
                return next(self.chunks)
            except StopIteration as exc:
                raise EOFError from exc

    process = Process()
    with pytest.raises(RuntimeError, match="closed before readiness"):
        _WinPtyProcess(process).read_until_ready(1.0)

    assert process.reads == 2


def test_main_repl_readiness_rejects_product_modal_before_footer() -> None:
    output = (
        "\x1b[2JFable 5 is now a standard part of your Max plan\r\n"
        "1. Yes, try it\r\n2. Not now\r\n"
        "\x1b[2m\u23f5\u23f5don't ask on\x1b[0m"
    )

    assert not _claude_main_repl_ready(output)


def test_main_repl_readiness_rejects_repeated_modal_footer_redraws() -> None:
    modal = (
        "\x1b[2JFable 5 is now a standard part of your Max plan\r\n"
        "1. Yes, try it\r\n2. Not now\r\n"
    )
    footer = "\x1b[2m\u23f5\u23f5don't ask on\x1b[0m"

    assert not _claude_main_repl_ready(modal + footer + modal + footer)


def test_main_repl_readiness_rejects_auto_default_nudge() -> None:
    output = (
        "\x1b[?2004h\x1b[2JMake auto mode your default permission mode?\r\n"
        "Yes, set auto mode as my default permission mode\r\n"
        "No, keep don't ask\r\n"
        "\x1b[2m\u23f5\u23f5\x1b[0m"
    )

    assert not _claude_main_repl_ready(output)


def test_winpty_readiness_crosses_exact_workspace_trust_gate_once() -> None:
    trust = (
        "\x1b[2JAccessing workspace:\r\n"
        "Yes, I trust this folder\r\nNo, exit\r\nSecurity guide\r\n"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter(
                [
                    "\x1b[?2004h",
                    trust,
                    trust,
                    "\x1b[?2004h",
                    "\x1b[2m⏵⏵ don't ask on\x1b[0m",
                ]
            )
            self.writes: list[str] = []
            self.reads = 0

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            self.reads += 1
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

        def write(self, data: str) -> None:
            self.writes.append(data)

    process = Process()
    output = _WinPtyProcess(process).read_until_ready(
        1.0, accept_workspace_trust=True
    )

    assert "\x1b[?2004h" in output
    assert "⏵⏵ don't ask on" in output
    assert process.writes == ["\r"]
    assert process.reads == 6


def test_winpty_readiness_crosses_restricted_workspace_trust_gate_once() -> None:
    trust = (
        "\x1b[2JAccessing workspace:\r\n"
        "Security guide\r\n"
        "Yes, I trust this folder\r\n"
        "No, continue without these permissions\r\n"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter(
                [
                    "\x1b[?2004h",
                    trust,
                    "\x1b[?2004h",
                    "\x1b[2m\u23f5\u23f5 don't ask on\x1b[0m",
                ]
            )
            self.writes: list[str] = []
            self.reads = 0

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            self.reads += 1
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

        def write(self, data: str) -> None:
            self.writes.append(data)

    process = Process()
    output = _WinPtyProcess(process).read_until_ready(
        1.0, accept_workspace_trust=True
    )

    assert "\u23f5\u23f5 don't ask on" in output
    assert process.writes == ["\r"]
    assert process.reads == 5


def test_winpty_readiness_crosses_cursor_positioned_workspace_trust_gate() -> None:
    trust = (
        "\x1b[2JAccessing\x1b[1Cworkspace:\x1b[2C"
        "Quick\x1b[1Csafety\x1b[1Ccheck\x1b[3C"
        "Security\x1b[1Cguide\x1b[2C"
        "Yes,\x1b[1CI\x1b[1Ctrust\x1b[1Cthis\x1b[1Cfolder\x1b[2C"
        "No,\x1b[1Cexit"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter(
                [
                    "\x1b[?2004h",
                    trust,
                    "\x1b[2m\u23f5\u23f5\x1b[0m",
                ]
            )
            self.writes: list[str] = []
            self.reads = 0

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            self.reads += 1
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

        def write(self, data: str) -> None:
            self.writes.append(data)

    process = Process()
    output = _WinPtyProcess(process).read_until_ready(
        1.0, accept_workspace_trust=True
    )

    assert "\u23f5\u23f5" in output
    assert process.writes == ["\r"]
    assert process.reads == 4


@pytest.mark.parametrize(
    "redraw_chunks, expected_reads",
    [
        (
            [
                "\x1b[2JAccessing workspace:\r\n"
                "Yes, I trust this folder\r\nNo, exit\r\nSecurity guide\r\n",
                "\x1b[?2004h\x1b[2m\u23f5\u23f5 don't ask on\x1b[0m",
            ],
            4,
        ),
        (
            [
                "\x1b[2JAccessing workspace:\r\n"
                "\x1b[1mYes, I trust this folder\x1b[0m\r\n"
                "No, exit\r\nSecurity guide\x1b[0m\r\n"
                "\x1b[?2004h\x1b[2m\u23f5\u23f5 don't ask on\x1b[0m",
            ],
            3,
        ),
    ],
    ids=["redraw-then-footer", "redraw-and-footer-same-chunk"],
)
def test_winpty_readiness_slices_past_latest_trust_redraw_without_resubmitting(
    redraw_chunks: list[str], expected_reads: int
) -> None:
    trust = (
        "\x1b[2JAccessing workspace:\r\n"
        "Yes, I trust this folder\r\nNo, exit\r\nSecurity guide\r\n"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([trust, *redraw_chunks])
            self.writes: list[str] = []
            self.reads = 0

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            self.reads += 1
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

        def write(self, data: str) -> None:
            self.writes.append(data)

    process = Process()
    output = _WinPtyProcess(process).read_until_ready(
        1.0, accept_workspace_trust=True
    )

    assert "\u23f5\u23f5 don't ask on" in output
    assert process.writes == ["\r"]
    assert process.reads == expected_reads


def test_winpty_readiness_keeps_post_trust_product_modal_sticky_across_redraw() -> None:
    trust = (
        "\x1b[2JAccessing workspace:\r\n"
        "Yes, I trust this folder\r\nNo, exit\r\nSecurity guide\r\n"
    )
    modal = (
        "\x1b[2JFable 5 is now a standard part of your Max plan\r\n"
        "1. Yes, try it\r\n2. Not now\r\n"
    )
    footer = "\x1b[?2004h\x1b[2m\u23f5\u23f5 don't ask on\x1b[0m"

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([trust, modal, trust + footer, "\x1b[H"])
            self.writes: list[str] = []

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

        def write(self, data: str) -> None:
            self.writes.append(data)

    process = Process()
    with pytest.raises(_PtyReadinessTimeout) as failure:
        _WinPtyProcess(process).read_until_ready(
            1.0, accept_workspace_trust=True
        )

    assert failure.value.reason == "known_input_modal"
    assert process.writes == ["\r"]


def test_winpty_readiness_partial_post_trust_prefix_invalidates_old_footer() -> None:
    trust = (
        "\x1b[2JAccessing workspace:\r\n"
        "Yes, I trust this folder\r\nNo, exit\r\nSecurity guide\r\n"
    )
    footer = "\x1b[?2004h\x1b[2m\u23f5\u23f5 don't ask on\x1b[0m"
    partial_redraw = (
        "\x1b[2JAccessing workspace:\r\n"
        "Yes, I trust this folder\r\n"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([trust, footer, partial_redraw])
            self.writes: list[str] = []
            self.reads = 0

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            self.reads += 1
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

        def write(self, data: str) -> None:
            self.writes.append(data)

    process = Process()
    with pytest.raises(TimeoutError):
        _WinPtyProcess(process).read_until_ready(
            1.0, accept_workspace_trust=True
        )

    assert process.writes == ["\r"]
    assert process.reads == 4


def test_winpty_readiness_partial_trust_redraw_completes_before_new_footer() -> None:
    trust = (
        "\x1b[2JAccessing workspace:\r\n"
        "Yes, I trust this folder\r\nNo, exit\r\nSecurity guide\r\n"
    )
    footer = "\x1b[?2004h\x1b[2m\u23f5\u23f5 don't ask on\x1b[0m"
    partial_redraw = (
        "\x1b[2JAccessing workspace:\r\n"
        "Yes, I trust this folder\r\n"
    )
    redraw_tail = "No, exit\r\nSecurity guide\r\n" + footer

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([trust, footer, partial_redraw, redraw_tail])
            self.writes: list[str] = []
            self.reads = 0

        def read_with_timeout(self, _size: int, timeout: float) -> str | None:
            self.reads += 1
            try:
                return next(self.chunks)
            except StopIteration:
                time.sleep(timeout)
                return None

        def write(self, data: str) -> None:
            self.writes.append(data)

    process = Process()
    output = _WinPtyProcess(process).read_until_ready(
        1.0, accept_workspace_trust=True
    )

    assert redraw_tail in output
    assert process.writes == ["\r"]
    assert process.reads == 5


def test_winpty_readiness_eof_before_settle_fails_closed() -> None:
    class Process:
        def __init__(self) -> None:
            self.chunks = iter(
                ["\x1b[?2004h\x1b[2m\u23f5\u23f5 don't ask on\x1b[0m"]
            )

        def read_with_timeout(self, _size: int, _timeout: float) -> str:
            try:
                return next(self.chunks)
            except StopIteration as exc:
                raise EOFError from exc

    with pytest.raises(RuntimeError, match="closed before readiness"):
        _WinPtyProcess(Process()).read_until_ready(1.0)


def test_winpty_readiness_classifies_provider_limit_eof_before_main_repl() -> None:
    provider_limit = "You've hit your limit · resets 5am"

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([provider_limit])

        def read_with_timeout(self, _size: int, _timeout: float) -> str:
            try:
                return next(self.chunks)
            except StopIteration as exc:
                raise EOFError from exc

    assert _WinPtyProcess(Process()).read_until_ready(1.0) == provider_limit


def test_winpty_readiness_never_returns_on_trust_dialog_marker_alone() -> None:
    trust = (
        "\x1b[?2004h\x1b[2JAccessing workspace:\r\n"
        "Yes, I trust this folder\r\nNo, exit\r\nSecurity guide\r\n"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([trust])
            self.writes: list[str] = []
            self.reads = 0

        def read_with_timeout(self, _size: int, _timeout: float) -> str:
            self.reads += 1
            try:
                return next(self.chunks)
            except StopIteration as exc:
                raise EOFError from exc

        def write(self, data: str) -> None:
            self.writes.append(data)

    process = Process()
    with pytest.raises(RuntimeError, match="closed before readiness"):
        _WinPtyProcess(process).read_until_ready(
            1.0, accept_workspace_trust=True
        )

    assert process.writes == ["\r"]
    assert process.reads == 2


def test_winpty_readiness_never_accepts_theme_or_onboarding_screen() -> None:
    theme = (
        "\x1b[?2004h\x1b[2JWelcome to Claude Code v2.1.110\r\n"
        "Let's get started.\r\n> 1. Dark mode\r\n2. Light mode\r\n"
        "Syntax theme: Monokai Extended\r\n"
    )

    class Process:
        def __init__(self) -> None:
            self.chunks = iter([theme])
            self.writes: list[str] = []
            self.reads = 0

        def read_with_timeout(self, _size: int, _timeout: float) -> str:
            self.reads += 1
            try:
                return next(self.chunks)
            except StopIteration as exc:
                raise EOFError from exc

        def write(self, data: str) -> None:
            self.writes.append(data)

    process = Process()
    with pytest.raises(RuntimeError, match="closed before readiness"):
        _WinPtyProcess(process).read_until_ready(
            1.0, accept_workspace_trust=True
        )

    assert process.writes == []
    assert process.reads == 2


def test_winpty_reader_timeout_is_bounded_when_underlying_read_blocks() -> None:
    release = threading.Event()

    class Process:
        def read(self, size: int = 1024) -> str:
            release.wait(2)
            raise EOFError

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        _WinPtyProcess(Process()).read_until(0.05)
    assert time.monotonic() - started < 0.5
    release.set()


def test_none_exit_code_is_never_accepted_as_clean() -> None:
    item = claim()
    process = FakePty(exit_code=None)  # type: ignore[arg-type]
    result = registrar(
        FakeSource([None, projection_for(item)]), FakeFactory(process)
    ).process(item)
    assert result.status == "retry"
    assert result.error_code == "clean_exit_not_observed"


def test_cleanup_failure_after_spawn_overrides_success_as_creation_ambiguous() -> None:
    item = claim()
    process = FakePty()
    process.cleanup_result = PtyCleanupResult(False, False, False, 0)
    store = FakeStore()
    result = registrar(
        FakeSource([None, projection_for(item)]), FakeFactory(process), store
    ).process(item)
    assert result.status == "retry" and result.error_code == "creation_ambiguous"
    assert not any(call[0] == "commit" for call in store.calls)


def test_winpty_close_is_idempotent_and_reports_all_cleanup_postconditions() -> None:
    released = threading.Event()

    class Resource:
        def __init__(self, descriptor: int):
            self.descriptor = descriptor

        def close(self) -> None:
            self.descriptor = -1
            released.set()

        def fileno(self) -> int:
            return self.descriptor

    class NativePty:
        fd = 42

        def isalive(self) -> bool:
            return False

        def get_exitstatus(self) -> int:
            return 0

    class Process:
        def __init__(self):
            self.fileobj = Resource(10)
            self._server = Resource(11)
            self.pty = NativePty()
            self.fd = 10
            self.closed = False
            self.exitstatus = 0
            self._thread = threading.Thread(target=lambda: None)
            self._thread.start()

        def read(self, size: int = 1024) -> str:
            released.wait(1)
            raise EOFError

        def isalive(self) -> bool:
            return False

    wrapped = _WinPtyProcess(Process())
    with pytest.raises(TimeoutError):
        wrapped.read_until(0.02)
    first = wrapped.close(0.5)
    second = wrapped.close(0.5)
    assert first == second == PtyCleanupResult(True, True, True, 0)


def test_winpty_unknown_private_resource_layout_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="unsupported pywinpty resource layout"):
        _WinPtyProcess(object(), require_supported_layout=True)


def test_factory_sets_cli_entrypoint_and_update_lock_only_in_child_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    class ProcessType:
        @staticmethod
        def spawn(
            argv: list[str],
            *,
            cwd: str,
            env: dict[str, str],
            dimensions: tuple[int, int],
        ) -> object:
            observed.update(
                argv=argv,
                cwd=cwd,
                env=env,
                dimensions=dimensions,
            )
            return object()

    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.delenv("DISABLE_UPDATES", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", raising=False)
    monkeypatch.delenv("DISABLE_GROWTHBOOK", raising=False)
    monkeypatch.setattr(
        "session_bridge.claude_registrar._registrar_pywinpty_process_type",
        lambda: ProcessType,
    )

    WindowsConPtyFactory()._spawn_process(["claude"], cwd="C:/exact")

    assert observed["env"]["CLAUDE_CODE_ENTRYPOINT"] == "cli"
    assert observed["env"]["DISABLE_UPDATES"] == "1"
    assert observed["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert observed["env"]["DISABLE_GROWTHBOOK"] == "1"
    assert "CLAUDE_CODE_ENTRYPOINT" not in os.environ
    assert "DISABLE_UPDATES" not in os.environ
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in os.environ
    assert "DISABLE_GROWTHBOOK" not in os.environ


@pytest.mark.parametrize(
    ("environment_name", "environment_value"),
    [
        ("CLAUDE_CONFIG_DIR", "C:/reintroduced-config-root"),
        ("CLAUDE_CODE_POWERUP_ONBOARDING", "banner"),
        ("CLAUDE_CODE_POWERUP_ONBOARDING", "step"),
        ("CLAUDE_CODE_TEAM_ONBOARDING", "banner"),
        ("CLAUDE_CODE_TEAM_ONBOARDING", "step"),
    ],
)
def test_factory_rejects_unsafe_environment_reintroduced_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    environment_value: str,
) -> None:
    spawns: list[list[str]] = []

    class ProcessType:
        @staticmethod
        def spawn(argv: list[str], **_kwargs: Any) -> object:
            spawns.append(argv)
            return object()

    factory = WindowsConPtyFactory()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_POWERUP_ONBOARDING", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_TEAM_ONBOARDING", raising=False)
    monkeypatch.setattr(
        "session_bridge.claude_registrar._registrar_pywinpty_process_type",
        lambda: ProcessType,
    )
    monkeypatch.setenv(environment_name, environment_value)

    with pytest.raises(RuntimeError, match="unsafe Claude launch environment"):
        factory._spawn_process(["claude"], cwd="C:/exact")

    assert spawns == []


def test_factory_validation_failure_reclaims_spawned_child_and_descriptors() -> None:
    class Resource:
        def __init__(self, descriptor: int):
            self.descriptor = descriptor

        def close(self) -> None:
            self.descriptor = -1

        def fileno(self) -> int:
            return self.descriptor

    class Process:
        def __init__(self):
            self.fileobj = Resource(10)
            self._server = Resource(11)
            self.fd = 10
            self.dead = False
            self._thread = threading.Thread(target=lambda: None)
            self._thread.start()

        def isalive(self) -> bool:
            return not self.dead

        def terminate(self, force: bool = False) -> bool:
            assert force
            self.dead = True
            return True

    process = Process()

    class Factory(WindowsConPtyFactory):
        def _spawn_process(self, argv: list[str], *, cwd: str) -> object:
            return process

        def _adapt_process(self, spawned: object) -> _WinPtyProcess:
            assert spawned is process
            raise RuntimeError("unsupported pywinpty resource layout")

    with pytest.raises(RuntimeError, match="pty unavailable"):
        Factory().spawn(["ignored"], cwd="C:/ignored")
    assert process.dead
    assert process.fileobj.fileno() == process._server.fileno() == -1
    assert process.fd == -1


def test_factory_surfaces_unconfirmed_post_spawn_process_death() -> None:
    class Resource:
        def close(self) -> None:
            pass

        def fileno(self) -> int:
            return -1

    class Process:
        fileobj = Resource()
        _server = Resource()
        fd = -1
        pid = None
        _thread = threading.Thread(target=lambda: None)

        def isalive(self) -> bool:
            return True

        def terminate(self, force: bool = False) -> bool:
            return False

    Process._thread.start()

    class Factory(WindowsConPtyFactory):
        def _spawn_process(self, argv: list[str], *, cwd: str) -> object:
            return Process()

        def _adapt_process(self, spawned: object) -> _WinPtyProcess:
            raise RuntimeError("unsupported pywinpty resource layout")

    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        Factory().spawn(["ignored"], cwd="C:/ignored")


def test_paid_launch_exact_reconciles_existing_uuid_before_any_spawn() -> None:
    item = claim()
    store = FakeStore()
    factory = FakeFactory()
    result = registrar(FakeSource([projection_for(item)]), factory, store).process(item)
    assert result.status == "visible"
    assert factory.spawns == []
    assert store.calls[0][0] == "commit"


def test_restart_reconciliation_commits_exact_uuid_without_second_spawn_or_usage(
    tmp_path: Path,
) -> None:
    now = [100.0]
    database = SessionDB(tmp_path / "state.db")
    first_store = SessionBridgeStore(
        database, clock=lambda: now[0], local_timezone=timezone.utc
    )
    value = candidate()
    identity = derive_claude_visibility_identity(value, SECRET)
    first_store.enqueue_claude_visibility_job(value, identity, SECRET)
    first_store.upsert_projection(
        SessionProjection(
            provider=Provider.CODEX,
            native_id=value.source_session_id.removeprefix("codex:"),
            title=value.native_name,
            cwd=value.source_cwd,
            started_at=10.0,
            last_active=11.0,
            messages=(ProjectedMessage("source-u1", 0, "user", "request", 10.0),),
            native_path="C:/codex/source-1.jsonl",
            native_cursor="source-cursor",
            native_hash="source-hash",
            origin_kind=OriginKind.NATIVE,
        )
    )
    first = first_store.claim_claude_visibility_job(now[0], 60, 25, "0.50", "0.02")
    assert first.lease_kind == "launch"
    first_factory = FakeFactory(FakePty(read_error=TimeoutError()))
    ambiguous = registrar(FakeSource(), first_factory, first_store).process(first)
    assert ambiguous.error_code == "creation_ambiguous"

    now[0] = 105.0
    restarted_store = SessionBridgeStore(
        database, clock=lambda: now[0], local_timezone=timezone.utc
    )
    reconciliation = restarted_store.claim_claude_visibility_job(
        now[0], 60, 25, "0.50", "0.02"
    )
    assert reconciliation.lease_kind == "reconciliation"
    restarted_factory = FakeFactory()
    visible = registrar(
        FakeSource([projection_for(reconciliation)]), restarted_factory, restarted_store
    ).process(reconciliation)

    assert visible.status == "visible"
    assert len(first_factory.spawns) == 1 and restarted_factory.spawns == []
    assert restarted_store.claude_visibility_status(now[0])["usage"]["attempts"] == 1
    gate = restarted_store.claim_claude_visibility_job(now[0], 60, 25, "0.50", "0.02")
    assert gate.status == "no_due_job" and gate.lease_kind is None
    database.close()


def test_zero_result_ambiguity_records_absence_then_authorizes_same_uuid_only(
    tmp_path: Path,
) -> None:
    now = [100.0]
    database = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(
        database, clock=lambda: now[0], local_timezone=timezone.utc
    )
    value = candidate()
    identity = derive_claude_visibility_identity(value, SECRET)
    store.enqueue_claude_visibility_job(value, identity, SECRET)
    first = store.claim_claude_visibility_job(now[0], 60, 25, "0.50", "0.02")
    first_factory = FakeFactory(FakePty(read_error=TimeoutError()))
    registrar(FakeSource(), first_factory, store).process(first)

    now[0] = 105.0
    reconciliation = store.claim_claude_visibility_job(now[0], 60, 25, "0.50", "0.02")
    reconciliation_factory = FakeFactory()
    absent = registrar(FakeSource([None]), reconciliation_factory, store).process(
        reconciliation
    )
    assert absent.status == "absent" and reconciliation_factory.spawns == []
    assert store.claude_visibility_status(now[0])["usage"]["attempts"] == 1

    second = store.claim_claude_visibility_job(now[0], 60, 25, "0.50", "0.02")
    assert second.lease_kind == "launch"
    assert (
        second.reserved_claude_uuid
        == first.reserved_claude_uuid
        == identity.claude_uuid
    )
    assert second.attempt_ordinal == 2
    assert store.claude_visibility_status(now[0])["usage"]["attempts"] == 2
    assert len(first_factory.spawns) == 1
    database.close()


def test_nonlease_store_gate_has_no_lease_kind(tmp_path: Path) -> None:
    database = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(
        database, clock=lambda: 100.0, local_timezone=timezone.utc
    )
    value = candidate()
    identity = derive_claude_visibility_identity(value, SECRET)
    store.enqueue_claude_visibility_job(value, identity, SECRET)
    gated = store.claim_claude_visibility_job(100.0, 60, 25, "0.01", "0.02")
    assert gated.status == "cost_limit" and gated.lease_kind is None
    database.close()


def test_offline_interactive_fixture_records_frames_exit_and_delayed_index(
    tmp_path: Path,
) -> None:
    record = tmp_path / "record.json"
    fixture = Path(__file__).parent / "fixtures" / "fake_interactive_claude.py"
    env = {
        **os.environ,
        "FAKE_CLAUDE_RECORD": str(record),
        "FAKE_CLAUDE_SCENARIO": "delayed_transcript_indexing",
        "FAKE_CLAUDE_INDEX_DELAY": "0.01",
    }
    # A host entrypoint (e.g. a desktop-launched runner) would be recorded in the
    # fixture's spawn event and break the exact-equality assertion below.
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    process = subprocess.Popen(
        [sys.executable, str(fixture), "--session-id", "offline-uuid"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(b"\x1b[200~offline prompt\x1b[201~\r")
    process.stdin.flush()
    assert b"REGISTERED" in process.stdout.readline()
    process.stdin.write(b"/exit\n")
    process.stdin.flush()
    assert process.wait(timeout=2) == 0

    events = json.loads(record.read_text(encoding="utf-8"))
    assert events[0] == {
        "argv": ["--session-id", "offline-uuid"],
        "cwd": str(tmp_path),
        "event": "spawn",
    }
    assert [event["event"] for event in events] == [
        "spawn",
        "stdin",
        "native_created",
        "index_ready",
        "stdin",
        "exit",
    ]


def test_offline_fixture_reads_complete_multiline_bracketed_paste_frame(
    tmp_path: Path,
) -> None:
    record = tmp_path / "multiline-record.json"
    fixture = Path(__file__).parent / "fixtures" / "fake_interactive_claude.py"
    process = subprocess.Popen(
        [sys.executable, str(fixture), "--session-id", "offline-multiline"],
        cwd=tmp_path,
        env={
            **os.environ,
            "FAKE_CLAUDE_RECORD": str(record),
            "FAKE_CLAUDE_SCENARIO": "registered",
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    frame = b"\x1b[200~first line\r\nsecond line\nthird line\x1b[201~\r"
    process.stdin.write(frame)
    process.stdin.flush()
    assert b"REGISTERED" in process.stdout.readline()
    process.stdin.write(b"/exit\r\n")
    process.stdin.flush()
    assert process.wait(timeout=2) == 0
    events = json.loads(record.read_text(encoding="utf-8"))
    assert events[1] == {
        "event": "stdin",
        "frame": frame.decode("utf-8"),
    }
    assert events[3] == {"event": "stdin", "frame": "/exit\r\n"}


def _fixture_read_frame(data: bytes) -> str:
    fixture = Path(__file__).parent / "fixtures" / "fake_interactive_claude.py"
    namespace = runpy.run_path(str(fixture))
    return namespace["_read_frame"](io.BytesIO(data))


def test_fixture_accepts_close_marker_at_exact_frame_boundary() -> None:
    opening = b"\x1b[200~"
    closing = b"\x1b[201~"
    content = b"x" * (65_536 - len(opening) - len(closing))
    frame = opening + content + closing
    assert _fixture_read_frame(frame + b"\r") == (frame + b"\r").decode()


def test_fixture_returns_bounded_partial_frame_when_close_marker_is_missing_at_eof() -> (
    None
):
    frame = b"\x1b[200~line one\r\nline two"
    assert _fixture_read_frame(frame) == frame.decode()


@pytest.mark.parametrize(
    ("terminator", "consumed"),
    [(b"\r", b"\r"), (b"\n", b"\n"), (b"\r\n", b"\r")],
)
def test_fixture_consumes_one_terminal_normalized_trailing_terminator(
    terminator: bytes, consumed: bytes
) -> None:
    frame = b"\x1b[200~line one\nline two\x1b[201~"
    assert _fixture_read_frame(frame + terminator) == (frame + consumed).decode()


@pytest.mark.parametrize(
    ("scenario", "expected_code", "expected_output"),
    [
        ("authentication_failure", 1, b"Authentication required"),
        ("malformed_response", 0, b"NOT REGISTERED"),
        ("nonzero", 9, b"REGISTERED"),
    ],
)
def test_offline_fixture_named_terminating_scenarios_record_exit(
    tmp_path: Path, scenario: str, expected_code: int, expected_output: bytes
) -> None:
    record = tmp_path / f"{scenario}.json"
    fixture = Path(__file__).parent / "fixtures" / "fake_interactive_claude.py"
    process = subprocess.Popen(
        [sys.executable, str(fixture), "--session-id", "offline-uuid"],
        cwd=tmp_path,
        env={
            **os.environ,
            "FAKE_CLAUDE_RECORD": str(record),
            "FAKE_CLAUDE_SCENARIO": scenario,
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    if scenario != "authentication_failure":
        process.stdin.write(b"\x1b[200~offline prompt\x1b[201~\r")
        process.stdin.flush()
    output = process.stdout.readline()
    assert expected_output in output
    if scenario != "authentication_failure":
        process.stdin.write(b"/exit\n")
        process.stdin.flush()
    assert process.wait(timeout=2) == expected_code
    events = json.loads(record.read_text(encoding="utf-8"))
    assert events[-1] == {
        "event": "exit",
        "scenario": scenario,
        "sequence": expected_code,
    }


# Deadlock guard for the real-ConPTY tests, which spawn a genuine python.exe
# behind a real pseudoconsole.  Nothing asserts on how much of it is consumed, so
# it is sized for the worst host rather than for expected latency: the previous
# 10s readiness budget expired on a loaded box and surfaced as
# `_PtyReadinessTimeout: terminal_input_not_enabled`.  Reads still return as soon
# as the reader settles, so a large ceiling costs nothing on the happy path.
_REAL_CONPTY_GUARD_SECONDS = 120.0


def _wait_for_fixture_event(record: Path, event: str, timeout: float) -> None:
    """Block until the fake Claude fixture has recorded ``event``.

    The record file is rewritten whole on each append, so a concurrent read can
    catch it mid-write; treat a partial parse as "not yet".
    """

    deadline = time.monotonic() + timeout
    while True:
        try:
            events = json.loads(record.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            events = []
        if any(entry.get("event") == event for entry in events):
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"fixture never recorded {event!r} within {timeout}s: {events}"
            )
        time.sleep(0.01)


def _real_conpty_available() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import winpty  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _real_conpty_available(), reason="Windows ConPTY unavailable")
@pytest.mark.parametrize(
    ("scenario", "expected_exit", "expected_lines"),
    [
        ("registered", 0, ["REGISTERED"]),
        ("nonzero", 9, ["REGISTERED"]),
        *(("delayed_extra", 0, ["REGISTERED", "extra"]) for _ in range(20)),
    ],
)
def test_real_windows_conpty_fixture_exit_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_exit: int,
    expected_lines: list[str],
) -> None:
    record = tmp_path / f"{scenario}.json"
    fixture = Path(__file__).parent / "fixtures" / "fake_interactive_claude.py"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", scenario)
    monkeypatch.setenv("FAKE_CLAUDE_EXTRA_DELAY", "0.03")
    process = WindowsConPtyFactory().spawn(
        [sys.executable, str(fixture), "--session-id", "real-conpty-uuid"],
        cwd=str(tmp_path),
    )
    registration_prompt = (
        build_claude_registration_prompt(
            candidate(), derive_claude_visibility_identity(candidate(), SECRET), SECRET
        )
        if scenario == "registered"
        else "registration prompt"
    )
    startup = process.read_until_ready(_REAL_CONPTY_GUARD_SECONDS)
    assert "\x1b[?2004h" in startup
    process.write(f"\x1b[200~{registration_prompt}\x1b[201~\r")
    if scenario == "delayed_extra":
        # Wait for the EFFECT -- the fixture has written and flushed the trailing
        # line -- rather than betting it arrives inside the reader's
        # _RESPONSE_SETTLE_SECONDS (0.5s) window.  That bet races real ConPTY
        # transport latency and loses under load, truncating the drain to
        # ["REGISTERED"].  Whether a *temporally* delayed line still lands inside
        # the settle window is covered in-process, without a subprocess, by
        # test_winpty_reader_drains_extra_output_after_registered_before_acceptance;
        # here both lines only need to be in the stream before the drain starts.
        _wait_for_fixture_event(record, "extra", _REAL_CONPTY_GUARD_SECONDS)
    output = process.read_until(_REAL_CONPTY_GUARD_SECONDS, prompt=registration_prompt)
    process.write("/exit\r")
    assert output.strip().splitlines() == expected_lines
    assert process.wait(_REAL_CONPTY_GUARD_SECONDS) == expected_exit
    cleanup = process.close(_REAL_CONPTY_GUARD_SECONDS)
    assert cleanup == PtyCleanupResult(True, True, True, expected_exit)
    assert cleanup.registrar_reader_stopped is True
    assert cleanup.transport_reader_stopped is True
    assert process._reader_thread is None
    assert process.close(5.0) == cleanup
    events = json.loads(record.read_text(encoding="utf-8"))
    assert events[0]["cwd"] == str(tmp_path)
    assert events[0]["argv"] == ["--session-id", "real-conpty-uuid"]
    assert events[1]["frame"].rstrip("\r\n") == registration_prompt.replace("\n", "")
    assert events[-2]["frame"].strip() == "/exit"


@pytest.mark.skipif(not _real_conpty_available(), reason="Windows ConPTY unavailable")
def test_registrar_completes_genuine_interactive_conpty_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = replace(
        candidate(),
        source_cwd=str(tmp_path),
        git_root=str(tmp_path),
    )
    identity = derive_claude_visibility_identity(value, SECRET)
    item = replace(
        claim(),
        job_id=identity.job_id,
        source_session_id=value.source_session_id,
        reserved_claude_uuid=identity.claude_uuid,
        native_name=value.native_name,
        source_cwd=value.source_cwd,
        git_root=value.git_root,
        signed_marker=identity.signed_marker,
    )
    prompt = build_claude_registration_prompt(value, identity, SECRET)
    projection = SessionProjection(
        provider=Provider.CLAUDE,
        native_id=identity.claude_uuid,
        title=value.native_name,
        cwd=value.source_cwd,
        started_at=10.0,
        last_active=11.0,
        messages=[
            ProjectedMessage("u1", 0, "user", prompt, 10.0),
            ProjectedMessage("a1", 0, "assistant", "REGISTERED", 11.0),
        ],
        native_path=str(tmp_path / f"{identity.claude_uuid}.jsonl"),
        native_hash="c" * 64,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=identity.bridge_id,
    )
    record = tmp_path / "registrar-record.json"
    fixture = Path(__file__).parent / "fixtures" / "fake_interactive_claude.py"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "registered")

    result = ClaudeNativeRegistrar(
        cast(Any, FakeStore()),
        cast(Any, FakeSource([None, projection])),
        marker_secret=SECRET,
        startup_theme="light",
        pty_factory=WindowsConPtyFactory(),
        claude_command=(sys.executable, str(fixture)),
        clock=lambda: 100.0,
        monotonic=time.monotonic,
        sleep=time.sleep,
        process_timeout=10.0,
        exit_timeout=5.0,
        discovery_timeout=0.0,
        retry_delay=5.0,
    ).process(item)

    assert result.status == "visible"
    events = json.loads(record.read_text(encoding="utf-8"))
    spawn_argv = events[0]["argv"]
    assert "--print" not in spawn_argv and "-p" not in spawn_argv
    assert prompt not in spawn_argv
    assert events[0]["entrypoint"] == "cli"
    # ConPTY consumes bracketed-paste controls and normalizes the multiline frame.
    assert events[1]["frame"].rstrip("\r\n") == prompt.replace("\n", "")
    assert events[-2] == {"event": "stdin", "frame": "/exit\r\n"}
    assert events[-1] == {"event": "exit", "scenario": "registered", "sequence": 0}


@pytest.mark.skipif(not _real_conpty_available(), reason="Windows ConPTY unavailable")
def test_real_windows_conpty_timeout_terminates_and_releases_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "timeout.json"
    fixture = Path(__file__).parent / "fixtures" / "fake_interactive_claude.py"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "timeout_after_native_creation")
    process = WindowsConPtyFactory().spawn(
        [sys.executable, str(fixture), "--session-id", "real-timeout-uuid"],
        cwd=str(tmp_path),
    )
    process.write("registration prompt\r")
    with pytest.raises(TimeoutError):
        process.read_until(0.1, prompt="registration prompt")
    assert process.terminate(5.0)
    cleanup = process.close(5.0)
    assert (
        cleanup.process_dead and cleanup.reader_stopped and cleanup.descriptors_closed
    )
    assert cleanup.registrar_reader_stopped is True
    assert cleanup.transport_reader_stopped is True
    assert process._reader_thread is None


@pytest.mark.skipif(not _real_conpty_available(), reason="Windows ConPTY unavailable")
def test_detached_redirected_registrar_cancellation_is_ambiguous_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_marker = "SESSION_BRIDGE_DETACHED_CONPTY_CHILD"
    if os.environ.get(child_marker) != "1":
        stdout_path = tmp_path / "detached-stdout.txt"
        stderr_path = tmp_path / "detached-stderr.txt"
        env = {**os.environ, child_marker: "1"}
        command = [
            sys.executable,
            "-m",
            "pytest",
            f"{Path(__file__).resolve()}::test_detached_redirected_registrar_cancellation_is_ambiguous_and_cleans_up",
            "-q",
            "-s",
        ]
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            child = subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return_code = child.wait(timeout=_REAL_CONPTY_GUARD_SECONDS)
        output = stdout_path.read_text(encoding="utf-8", errors="replace")
        errors = stderr_path.read_text(encoding="utf-8", errors="replace")
        assert return_code == 0, errors
        assert "DETACHED_CONPTY_CANCELLATION_OK" in output
        return

    value = replace(candidate(), source_cwd=str(tmp_path), git_root=str(tmp_path))
    identity = derive_claude_visibility_identity(value, SECRET)
    item = replace(
        claim(),
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        signed_marker=identity.signed_marker,
        source_cwd=value.source_cwd,
        git_root=value.git_root,
    )
    record = tmp_path / "detached-cancel.json"
    fixture = Path(__file__).parent / "fixtures" / "fake_interactive_claude.py"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "timeout_after_native_creation")
    stop = threading.Event()
    store = FakeStore()

    class CapturingFactory(WindowsConPtyFactory):
        process: _WinPtyProcess | None = None
        spawn_count = 0

        def spawn(self, argv: list[str], *, cwd: str) -> _WinPtyProcess:
            self.spawn_count += 1
            spawned = super().spawn(argv, cwd=cwd)
            assert isinstance(spawned, _WinPtyProcess)
            self.process = spawned
            return spawned

    factory = CapturingFactory()

    def cancel_after_creation() -> None:
        _wait_for_fixture_event(record, "native_created", _REAL_CONPTY_GUARD_SECONDS)
        stop.set()

    canceller = threading.Thread(target=cancel_after_creation)
    canceller.start()
    try:
        result = ClaudeNativeRegistrar(
            cast(Any, store),
            cast(Any, FakeSource()),
            marker_secret=SECRET,
            startup_theme="light",
            pty_factory=factory,
            claude_command=(sys.executable, str(fixture)),
            clock=lambda: 100.0,
            monotonic=time.monotonic,
            sleep=time.sleep,
            process_timeout=_REAL_CONPTY_GUARD_SECONDS,
            exit_timeout=5.0,
            discovery_timeout=0.0,
            retry_delay=5.0,
        ).process(item, stop=stop)
    finally:
        canceller.join(_REAL_CONPTY_GUARD_SECONDS)

    assert canceller.is_alive() is False
    assert result.status == "retry"
    assert result.error_code == "creation_ambiguous"
    assert factory.spawn_count == 1
    assert [call[0] for call in store.calls] == ["retry"]
    assert factory.process is not None
    cleanup = factory.process.close(5.0)
    assert cleanup.process_dead is True
    assert cleanup.succeeded
    assert cleanup.registrar_reader_stopped is True
    assert cleanup.transport_reader_stopped is True
    assert factory.process._reader_thread is None
    print("DETACHED_CONPTY_CANCELLATION_OK")


@pytest.mark.skipif(not _real_conpty_available(), reason="Windows ConPTY unavailable")
def test_real_windows_authentication_failure_is_fixed_retry_with_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = replace(candidate(), source_cwd=str(tmp_path), git_root=str(tmp_path))
    identity = derive_claude_visibility_identity(value, SECRET)
    item = replace(
        claim(),
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        signed_marker=identity.signed_marker,
        source_cwd=value.source_cwd,
        git_root=value.git_root,
    )
    record = tmp_path / "authentication_failure.json"
    fixture = Path(__file__).parent / "fixtures" / "fake_interactive_claude.py"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "authentication_failure")

    class CapturingFactory(WindowsConPtyFactory):
        process: _WinPtyProcess | None = None

        def spawn(self, argv: list[str], *, cwd: str) -> _WinPtyProcess:
            spawned = super().spawn(argv, cwd=cwd)
            assert isinstance(spawned, _WinPtyProcess)
            self.process = spawned
            return spawned

    factory = CapturingFactory()
    result = ClaudeNativeRegistrar(
        cast(Any, FakeStore()),
        cast(Any, FakeSource()),
        marker_secret=SECRET,
        startup_theme="light",
        pty_factory=factory,
        claude_command=(sys.executable, str(fixture)),
        clock=lambda: 100.0,
        monotonic=time.monotonic,
        sleep=time.sleep,
        process_timeout=10.0,
        exit_timeout=5.0,
        discovery_timeout=0.0,
        retry_delay=5.0,
    ).process(item)

    assert result.status == "retry"
    assert result.error_code == "claude_authentication_unavailable"
    assert result.reserved_claude_uuid == item.reserved_claude_uuid
    assert factory.process is not None
    cleanup = factory.process.close(5.0)
    assert cleanup.succeeded
    assert cleanup.registrar_reader_stopped is True
    assert cleanup.transport_reader_stopped is True
    assert factory.process._reader_thread is None


@pytest.mark.skipif(not _real_conpty_available(), reason="Windows ConPTY unavailable")
def test_registrar_spawn_does_not_mutate_standard_pywinpty_reader_during_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket
    from winpty import PtyProcess
    from winpty import ptyprocess as winpty_module

    original_reader = winpty_module._read_in_thread
    changed: list[object] = []

    def observing_reader(address: object, pty: object, blocking: bool) -> None:
        del blocking
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(address)  # type: ignore[arg-type]
            while True:
                if winpty_module._read_in_thread is not observing_reader:
                    changed.append(winpty_module._read_in_thread)
                try:
                    data = pty.read(4096, blocking=False)  # type: ignore[attr-defined]
                except Exception:
                    return
                if data:
                    client.sendall(data.encode() if isinstance(data, str) else data)
                if pty.iseof():  # type: ignore[attr-defined]
                    return
                time.sleep(0.001)
        finally:
            client.close()

    monkeypatch.setattr(winpty_module, "_read_in_thread", observing_reader)
    standard = PtyProcess.spawn([
        sys.executable,
        "-c",
        "import time; time.sleep(1); print('STANDARD_OK')",
    ])
    record = tmp_path / "concurrent.json"
    fixture = Path(__file__).parent / "fixtures" / "fake_interactive_claude.py"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "registered")
    registrar_process = WindowsConPtyFactory().spawn(
        [sys.executable, str(fixture), "--session-id", "concurrent-uuid"],
        cwd=str(tmp_path),
    )
    assert winpty_module._read_in_thread is observing_reader
    registrar_process.write("\x1b[200~registration prompt\x1b[201~\r")
    assert "REGISTERED" in registrar_process.read_until(
        10.0, prompt="registration prompt"
    )
    registrar_process.write("/exit\r")
    assert registrar_process.wait(10.0) == 0
    assert registrar_process.close(5.0).succeeded
    standard_output = ""
    output_deadline = time.monotonic() + 5
    while "STANDARD_OK" not in standard_output and time.monotonic() < output_deadline:
        standard_output += standard.read(4096)
    assert "STANDARD_OK" in standard_output
    deadline = time.monotonic() + 5
    while standard.isalive() and time.monotonic() < deadline:
        time.sleep(0.01)
    standard.fileobj.close()
    standard._server.close()
    standard._thread.join(5)
    assert not changed
    monkeypatch.setattr(winpty_module, "_read_in_thread", original_reader)
    assert winpty_module._read_in_thread is original_reader
