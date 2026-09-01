from __future__ import annotations

import pytest

from session_bridge.codex_adapter import CodexThreadSummary
from session_bridge.config import BridgeConfig
from session_bridge.coordinator import SessionBridgeCoordinator
from session_bridge.models import Provider, SessionProjection, UpsertResult
from session_bridge.store import LocalSessionOwnsCanonicalId


def _projection(native_id: str) -> SessionProjection:
    return SessionProjection(
        provider=Provider.CODEX,
        native_id=native_id,
        title=f"[Codex] {native_id}",
        cwd="C:/workspace/project",
        started_at=10.0,
        last_active=20.0,
        messages=(),
        native_cursor="revision-1",
        native_hash="hash-1",
    )


class _CodexAdapter:
    def __init__(self, projection: SessionProjection) -> None:
        self.projection = projection

    def list_inventory(self, *, archived: bool) -> list[object]:
        if archived:
            return []
        return [
            CodexThreadSummary(
                native_id=self.projection.native_id,
                title=self.projection.title,
                cwd=self.projection.cwd,
                started_at=10.0,
                last_active=self.projection.last_active,
                archived=False,
                revision="revision-1",
            )
        ]

    def project_thread(self, summary: object) -> SessionProjection:
        del summary
        return self.projection


class _IdleClaudeAdapter:
    def discover(self) -> list[object]:
        return []

    def list_inventory(self, *, archived: bool) -> list[object]:
        del archived
        return []


class _OwnedCanonicalIdStore:
    """Every upsert collides with a local `sessions` row that owns the id.

    This is the live 2026-09-01 shape: 1,586 root-DB rows with source='codex'
    and no `external_sessions` row.
    """

    def __init__(self) -> None:
        self.attempts = 0

    def upsert_projection(
        self,
        projection: SessionProjection,
        *,
        rebuild: bool = False,
    ) -> UpsertResult:
        del rebuild
        self.attempts += 1
        raise LocalSessionOwnsCanonicalId(
            f"local session owns codex:{projection.native_id}"
        )


@pytest.mark.asyncio
async def test_declined_thread_is_reported_not_silently_dropped() -> None:
    """failed=0 must stop implying "nothing was declined".

    Without the ScanSummary field the handler still counts `locally_owned`
    internally and logs it, then drops it -- the caller sees
    discovered>=1, indexed=0, failed=0 and cannot distinguish a clean
    no-op scan from one that permanently declined every thread it found.
    """

    store = _OwnedCanonicalIdStore()
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={
            Provider.CLAUDE: _IdleClaudeAdapter(),
            Provider.CODEX: _CodexAdapter(_projection("thread-a")),
        },
    )

    summary = await coordinator.scan_once(Provider.CODEX)

    assert store.attempts >= 1, "the store must actually have been asked"
    assert summary.failed == 0, "a collision is benign and must not degrade"
    assert summary.indexed == 0, "nothing was cataloged"
    assert summary.locally_owned >= 1, (
        "the decline must survive to the summary; this is the whole point"
    )


@pytest.mark.asyncio
async def test_provider_wrapper_does_not_drop_locally_owned() -> None:
    """_scan_provider rebuilds ScanSummary field-by-field.

    Every caller funnels through it, so omitting the field there zeroes it
    for everyone while the inner path counted correctly.
    """

    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=_OwnedCanonicalIdStore(),
        adapters={
            Provider.CLAUDE: _IdleClaudeAdapter(),
            Provider.CODEX: _CodexAdapter(_projection("thread-b")),
        },
    )

    summary = await coordinator.scan_once(Provider.CODEX)

    assert summary.provider is Provider.CODEX
    assert summary.duration_ms >= 0, "wrapper-built summary, not the inner one"
    assert summary.locally_owned >= 1


@pytest.mark.asyncio
async def test_all_provider_aggregate_sums_locally_owned() -> None:
    """The provider=None aggregate sums each field explicitly.

    A field missing from that sum reads as zero on the default
    `scan_once()` path -- the one an operator actually runs.
    """

    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=_OwnedCanonicalIdStore(),
        adapters={
            Provider.CLAUDE: _IdleClaudeAdapter(),
            Provider.CODEX: _CodexAdapter(_projection("thread-c")),
        },
    )

    summary = await coordinator.scan_once()

    assert summary.provider is None
    assert summary.failed == 0
    assert summary.locally_owned >= 1


@pytest.mark.asyncio
async def test_stale_projection_is_not_counted_as_locally_owned() -> None:
    """The claude path caught (LocalSessionOwnsCanonicalId, StaleExternalProjection)
    in ONE clause, so a stale projection would inflate the new field.

    Splitting the clause is behaviour-preserving -- both still `continue`.
    """

    import inspect

    from session_bridge import coordinator as coordinator_module

    source = inspect.getsource(coordinator_module)
    assert (
        "except (LocalSessionOwnsCanonicalId, StaleExternalProjection):" not in source
    ), "combined clause would make locally_owned count stale projections too"
