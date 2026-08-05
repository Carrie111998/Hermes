from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from gateway import (
    canonical_writer_schema_reconciliation_control_bootstrap as bootstrap,
)
from scripts.canary import full_canary_owner_launcher as launcher


RELEASE_SHA = "a" * 40
SOURCE_RELEASE_SHA = "b" * 40


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _terminal() -> dict[str, object]:
    return {
        "schema": bootstrap.TERMINAL_SCHEMA,
        "ok": True,
        "state": "control_installed_admin_absent_stopped",
        "release_revision": SOURCE_RELEASE_SHA,
        "temporary_control_admin_absent": True,
        "completed_at_unix": 100,
        "terminal_sha256": "c" * 64,
    }


def test_successor_recovery_replays_only_the_exact_source_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guards: list[str] = []
    observed: dict[str, object] = {}

    def fake_bootstrap(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        assert kwargs["release_sha"] == SOURCE_RELEASE_SHA
        guard = kwargs["provenance_guard"]
        assert callable(guard)
        guard(SOURCE_RELEASE_SHA)
        return _terminal()

    monkeypatch.setattr(
        launcher,
        "bootstrap_schema_reconciliation_control",
        fake_bootstrap,
    )
    receipt = launcher.recover_historical_schema_reconciliation_control(
        release_sha=RELEASE_SHA,
        source_release_sha=SOURCE_RELEASE_SHA,
        transport=SimpleNamespace(),
        cloud_sql_client=SimpleNamespace(),
        owner_identity=SimpleNamespace(),
        now=lambda: 101,
        provenance_guard=guards.append,
    )

    assert guards == [RELEASE_SHA, RELEASE_SHA]
    assert observed["transport"].__class__ is SimpleNamespace
    assert receipt["schema"] == (
        launcher.SCHEMA_RECONCILIATION_CONTROL_SUCCESSOR_RECOVERY_SCHEMA
    )
    assert receipt["release_sha"] == RELEASE_SHA
    assert receipt["source_release_sha"] == SOURCE_RELEASE_SHA
    assert receipt["source_terminal"] == _terminal()
    assert receipt["source_terminal_sha256"] == "c" * 64
    assert receipt["temporary_control_admin_absent"] is True
    assert receipt["services_stopped"] is True
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256")
    assert receipt["receipt_sha256"] == hashlib.sha256(
        _canonical(unsigned)
    ).hexdigest()


@pytest.mark.parametrize(
    ("release_sha", "source_release_sha"),
    (
        (RELEASE_SHA, RELEASE_SHA),
        ("not-a-release", SOURCE_RELEASE_SHA),
        (RELEASE_SHA, "not-a-release"),
    ),
)
def test_successor_recovery_rejects_unbounded_source_identity(
    release_sha: str,
    source_release_sha: str,
) -> None:
    with pytest.raises(
        launcher.OwnerLauncherError,
        match="schema_reconciliation_control_successor_recovery_invalid",
    ):
        launcher.recover_historical_schema_reconciliation_control(
            release_sha=release_sha,
            source_release_sha=source_release_sha,
            transport=SimpleNamespace(),
            cloud_sql_client=SimpleNamespace(),
            owner_identity=SimpleNamespace(),
            provenance_guard=lambda _release: None,
        )


def test_successor_recovery_cli_requires_one_distinct_source_release() -> None:
    arguments = launcher._cli_parser().parse_args(
        (
            "--release-sha",
            RELEASE_SHA,
            "--recover-historical-schema-reconciliation-control",
            "--schema-reconciliation-source-release-sha",
            SOURCE_RELEASE_SHA,
        )
    )
    launcher._validate_schema_control_recovery_cli_arguments(
        arguments,
        release_sha=RELEASE_SHA,
    )

    missing = launcher._cli_parser().parse_args(
        (
            "--release-sha",
            RELEASE_SHA,
            "--recover-historical-schema-reconciliation-control",
        )
    )
    with pytest.raises(
        launcher.OwnerLauncherError,
        match="schema_reconciliation_control_successor_recovery_cli_invalid",
    ):
        launcher._validate_schema_control_recovery_cli_arguments(
            missing,
            release_sha=RELEASE_SHA,
        )

    unrelated = launcher._cli_parser().parse_args(
        (
            "--release-sha",
            RELEASE_SHA,
            "--schema-reconciliation-source-release-sha",
            SOURCE_RELEASE_SHA,
        )
    )
    with pytest.raises(
        launcher.OwnerLauncherError,
        match="schema_reconciliation_control_successor_recovery_cli_invalid",
    ):
        launcher._validate_schema_control_recovery_cli_arguments(
            unrelated,
            release_sha=RELEASE_SHA,
        )
