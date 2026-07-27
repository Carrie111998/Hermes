from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pytest

from session_bridge.coordinator import SidebarHydrationClaim
from session_bridge.models import (
    BridgeMarkerPayload,
    HydrationMarkerPayload,
    Provider,
    encode_bridge_marker,
)
from session_bridge.sidebar import (
    SidebarCandidate,
    build_hydration_message,
    build_registration_prompt,
    encode_hydration_marker,
    sidebar_bridge_id,
)
from session_bridge.sidebar_hydration_executor import (
    NativeTurnAmbiguous,
    SidebarHydrationExecutor,
)


SOURCE_ID = "claude:hydration-source"
BRIDGE_ID = sidebar_bridge_id(SOURCE_ID)
THREAD_ID = "019f8927-8012-77d0-beb0-4cd5f8cc2400"
MARKER_SECRET = b"hydration-executor-marker-secret"
HYDRATION_MARKER = encode_hydration_marker(
    HydrationMarkerPayload(
        bridge_id=BRIDGE_ID,
        codex_thread_id=THREAD_ID,
        preview_digest="a" * 64,
        preview_version=1,
        source_cursor="cursor-1",
        source_hash="hash-1",
        source_session_id=SOURCE_ID,
    ),
    MARKER_SECRET,
)


def _legacy_prompt() -> str:
    candidate = SidebarCandidate(
        source_session_id=SOURCE_ID,
        provider=Provider.CLAUDE,
        bridge_id=BRIDGE_ID,
        title="[Claude] Hydration source",
        cwd="C:/workspace",
        git_root="C:/workspace",
        git_branch="main",
        git_head="a" * 40,
        worktree_id="worktree-1",
        eligible_at=100.0,
    )
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=BRIDGE_ID,
            source_session_id=SOURCE_ID,
            target_provider=Provider.CODEX,
            policy_generation=1,
        ),
        MARKER_SECRET,
    )
    return build_registration_prompt(candidate, marker)


def _claim(*, send_reserved: bool = False) -> SidebarHydrationClaim:
    return SidebarHydrationClaim(
        lease_token="hydration-lease",
        source_session_id=SOURCE_ID,
        bridge_id=BRIDGE_ID,
        codex_thread_id=THREAD_ID,
        source_cursor="cursor-1",
        source_hash="hash-1",
        preview_version=1,
        preview_digest="a" * 64,
        hydration_marker=HYDRATION_MARKER,
        hydration_message=build_hydration_message(
            preview_rendered=(
                "# Imported Claude Code Session\n\n"
                "## Continuation Brief\n\n"
                "Preserve the exact native task.\n"
            ),
            source_session_id=SOURCE_ID,
            hydration_marker=HYDRATION_MARKER,
            send_reserved=False,
        ),
        cwd="C:/workspace",
        git_root="C:/workspace",
        send_reserved=send_reserved,
    )


@dataclass
class FakeStore:
    committed: int = 0
    reserved: int = 0
    failed_code: str | None = None

    def reserve_sidebar_hydration_send(self, *, lease_token: str, now: float):
        assert lease_token == "hydration-lease"
        assert now > 0
        self.reserved += 1
        return {"state": "hydration_leased", "send_reserved_at": now}

    def commit_sidebar_hydration_job(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        hydration_marker: str,
        now: float,
    ):
        assert lease_token == "hydration-lease"
        assert codex_thread_id == THREAD_ID
        assert hydration_marker == HYDRATION_MARKER
        assert now > 0
        self.committed += 1
        return {"state": "hydration_visible"}

    def fail_sidebar_hydration_job(
        self,
        *,
        lease_token: str,
        error_code: str,
        codex_thread_id: str,
        now: float,
    ):
        assert lease_token == "hydration-lease"
        assert codex_thread_id == THREAD_ID
        assert now > 0
        self.failed_code = error_code
        return {
            "state": (
                "hydration_retry"
                if error_code == "hydration_send_ambiguous"
                else "hydration_failed"
            ),
            "error_code": error_code,
        }


class FakeNative:
    def __init__(
        self,
        *,
        marker_present: bool = False,
        send_error: Exception | None = None,
    ) -> None:
        self.marker_present = marker_present
        self.send_error = send_error
        self.read_calls = 0
        self.marker_calls = 0
        self.send_calls = 0

    def read_thread_initial_prompt(self, *, thread_id: str, deadline: float) -> str:
        assert thread_id == THREAD_ID
        assert deadline > 0
        self.read_calls += 1
        return _legacy_prompt()

    def thread_has_exact_marker(
        self,
        *,
        thread_id: str,
        marker: str,
        deadline: float,
    ) -> bool:
        assert thread_id == THREAD_ID
        assert marker == HYDRATION_MARKER
        assert deadline > 0
        self.marker_calls += 1
        return self.marker_present

    def start_text_turn_and_verify_marker(
        self,
        *,
        thread_id: str,
        message: str,
        marker: str,
        deadline: float,
    ) -> None:
        assert thread_id == THREAD_ID
        assert message.startswith("# Imported Claude Code Session")
        assert marker == HYDRATION_MARKER
        assert deadline > 0
        self.send_calls += 1
        if self.send_error is not None:
            raise self.send_error
        self.marker_present = True


def _executor(
    claims: Callable[[], tuple[SidebarHydrationClaim, ...]],
    store: FakeStore,
    native: FakeNative,
) -> SidebarHydrationExecutor:
    return SidebarHydrationExecutor(
        claim_once=claims,
        store=store,  # type: ignore[arg-type]
        native=native,
        marker_secret=MARKER_SECRET,
        clock=lambda: 100.0,
        monotonic=lambda: 200.0,
    )


def test_hydration_executor_returns_idle_without_native_mutation() -> None:
    store = FakeStore()
    native = FakeNative()

    result = _executor(lambda: (), store, native).run_once()

    assert result.status == "idle"
    assert native.read_calls == 0
    assert native.send_calls == 0


def test_hydration_executor_commits_preexisting_marker_without_resend() -> None:
    store = FakeStore()
    native = FakeNative(marker_present=True)

    result = _executor(lambda: (_claim(),), store, native).run_once()

    assert result.status == "visible"
    assert store.committed == 1
    assert store.reserved == 1
    assert native.send_calls == 0


def test_hydration_executor_reserves_sends_once_verifies_and_commits() -> None:
    store = FakeStore()
    native = FakeNative()

    result = _executor(lambda: (_claim(),), store, native).run_once()

    assert result.status == "visible"
    assert store.reserved == 1
    assert store.committed == 1
    assert native.send_calls == 1


def test_hydration_executor_reserved_retry_reconciles_without_resend() -> None:
    store = FakeStore()
    native = FakeNative(marker_present=False)

    result = _executor(
        lambda: (_claim(send_reserved=True),),
        store,
        native,
    ).run_once()

    assert result.status == "retry"
    assert result.error_code == "hydration_send_ambiguous"
    assert store.failed_code == "hydration_send_ambiguous"
    assert store.reserved == 0
    assert native.send_calls == 0


def test_hydration_executor_records_post_dispatch_ambiguity_without_resend() -> None:
    store = FakeStore()
    native = FakeNative(send_error=NativeTurnAmbiguous())

    result = _executor(lambda: (_claim(),), store, native).run_once()

    assert result.status == "retry"
    assert result.error_code == "hydration_send_ambiguous"
    assert store.reserved == 1
    assert store.failed_code == "hydration_send_ambiguous"
    assert native.send_calls == 1


def test_hydration_executor_rejects_mismatched_authenticated_task() -> None:
    store = FakeStore()
    native = FakeNative()
    wrong_claim = _claim()
    wrong_claim = SidebarHydrationClaim(
        **{
            **wrong_claim.__dict__,
            "source_session_id": "claude:different-source",
        }
    )

    result = _executor(lambda: (wrong_claim,), store, native).run_once()

    assert result.status == "failed"
    assert result.error_code == "source_identity_mismatch"
    assert store.failed_code == "source_identity_mismatch"
    assert native.send_calls == 0
