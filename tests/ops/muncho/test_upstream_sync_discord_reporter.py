from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[3]
MODULE = ROOT / "ops/muncho/runtime/upstream_sync_discord_reporter.py"
SPEC = importlib.util.spec_from_file_location(
    "upstream_sync_discord_reporter_test",
    MODULE,
)
assert SPEC and SPEC.loader
reporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)


def _report(created: str = "2026-07-25T05:00:00Z") -> dict[str, object]:
    return {
        "schema": reporter.REPORT_SCHEMA,
        "created_at_utc": created,
        "status": "PARTIAL",
        "muncho": {
            "status": "PARTIAL",
            "outcome": "sync_pr_opened_review_required",
            "source_sha": "a" * 40,
            "upstream_sha": "b" * 40,
            "ahead": 412,
            "behind": 2075,
            "pr_url": "https://github.com/lomliev/hermes-agent/pull/201",
            "blocker": None,
        },
        "skyai": {
            "status": "PASS",
            "outcome": "candidate_pr_ready",
            "source_sha": "c" * 40,
            "upstream_sha": "d" * 40,
            "ahead": 93,
            "behind": 12,
            "pr_url": "https://github.com/lomliev/hermes-agent/pull/178",
            "blocker": None,
        },
    }


def _state_dir(tmp_path: Path) -> Path:
    state = tmp_path / "report-state"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    return state


def _public_report_dir(tmp_path: Path) -> Path:
    public = tmp_path / "public"
    public.mkdir()
    (public / "report-20260725T050000Z.json").write_text(
        json.dumps(_report()),
        encoding="utf-8",
    )
    return public


def test_daily_report_contains_both_components_and_safety() -> None:
    report = _report()
    report["_created"] = datetime(2026, 7, 25, 5, tzinfo=timezone.utc)
    message = reporter.format_daily_report(
        [report],
        now=datetime(2026, 7, 25, 6, tzinfo=timezone.utc),
        timezone_name="Europe/Sofia",
        window_hours=24,
    )

    assert "Muncho + SkyAI upstream sync" in message
    assert "**Muncho/Hermes:** ⚠️ PARTIAL" in message
    assert "**SkyAI:** ✅ PASS" in message
    assert "pull/201" in message
    assert "pull/178" in message
    assert "без auto-merge, deploy" in message
    assert len(message) <= reporter.MAX_MESSAGE_LENGTH


def test_loader_rejects_wrong_schema_and_out_of_window(tmp_path: Path) -> None:
    (tmp_path / "report-good.json").write_text(
        json.dumps(_report()),
        encoding="utf-8",
    )
    wrong = _report()
    wrong["schema"] = "other"
    (tmp_path / "report-wrong.json").write_text(json.dumps(wrong), encoding="utf-8")
    (tmp_path / "report-old.json").write_text(
        json.dumps(_report("2026-07-20T05:00:00Z")),
        encoding="utf-8",
    )

    reports = reporter.load_reports(
        tmp_path,
        now=datetime(2026, 7, 25, 6, tzinfo=timezone.utc),
        window_hours=24,
    )
    assert len(reports) == 1
    assert reports[0]["schema"] == reporter.REPORT_SCHEMA


def test_delivery_is_attempted_once_and_uses_hermes_send() -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_runner(args, **kwargs):
        calls.append((tuple(args), dict(kwargs)))
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"ok": True, "message_id": "123"}),
            stderr="",
        )

    result = reporter.deliver_once(
        "bounded report",
        channel_id="1504852355588423801",
        runner=fake_runner,
    )

    assert result == {"status": "PASS", "message_id": "123"}
    assert len(calls) == 1
    assert calls[0][0] == (
        sys.executable,
        "-m",
        "hermes_cli.main",
        "send",
        "--to",
        "discord:1504852355588423801",
        "--json",
    )
    assert calls[0][1]["input"] == "bounded report"


def test_delivery_failure_has_no_retry_loop() -> None:
    attempts = 0

    def fake_runner(args, **kwargs):
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

    result = reporter.deliver_once(
        "bounded report",
        channel_id="1504852355588423801",
        runner=fake_runner,
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "discord_delivery_failed"
    assert attempts == 1


def test_sender_interpreter_digest_drift_blocks_before_delivery(
    tmp_path: Path,
) -> None:
    sender = tmp_path / "python"
    sender.write_bytes(b"reviewed interpreter")
    sender.chmod(0o755)
    attempts = 0

    def fake_runner(args, **kwargs):
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

    result = reporter.deliver_once(
        "bounded report",
        channel_id="1504852355588423801",
        sender_python=sender,
        sender_python_sha256=hashlib.sha256(b"different").hexdigest(),
        runner=fake_runner,
    )

    assert result == {
        "status": "BLOCKED",
        "blocker": "sender_python_digest_drifted",
    }
    assert attempts == 0


def test_report_set_digest_is_exact_and_ordered() -> None:
    first = _report("2026-07-25T02:00:00Z")
    first["_created"] = datetime(2026, 7, 25, 2, tzinfo=timezone.utc)
    second = _report("2026-07-25T05:00:00Z")
    second["_created"] = datetime(2026, 7, 25, 5, tzinfo=timezone.utc)

    digest = reporter.report_set_sha256([first, second])

    assert len(digest) == 64
    assert reporter.report_set_sha256([first, second]) == digest
    assert reporter.report_set_sha256([second, first]) != digest
    changed = dict(second)
    changed["status"] = "BLOCKED"
    assert reporter.report_set_sha256([first, changed]) != digest


def test_reservation_is_at_most_once_for_exact_sofia_date_and_binding(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    now = datetime(2026, 7, 25, 6, tzinfo=timezone.utc)
    binding = reporter.delivery_binding(
        now=now,
        channel_id=reporter.DEFAULT_CHANNEL_ID,
        window_hours=24,
        report_count=1,
        report_set_digest="a" * 64,
        message_digest="b" * 64,
        message_bytes=42,
    )

    first, first_created = reporter.reserve_delivery_attempt(
        state,
        binding=binding,
        reserved_at=now,
    )
    second, second_created = reporter.reserve_delivery_attempt(
        state,
        binding=binding,
        reserved_at=now,
    )

    assert first_created is True
    assert second_created is False
    assert second == first
    assert first["binding"]["sofia_date"] == "2026-07-25"
    assert (state / "attempt-2026-07-25.json").stat().st_mode & 0o777 == 0o600


def test_same_sofia_date_with_changed_report_binding_fails_closed(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    now = datetime(2026, 7, 25, 6, tzinfo=timezone.utc)
    first = reporter.delivery_binding(
        now=now,
        channel_id=reporter.DEFAULT_CHANNEL_ID,
        window_hours=24,
        report_count=1,
        report_set_digest="a" * 64,
        message_digest="b" * 64,
        message_bytes=42,
    )
    changed = {**first, "report_set_sha256": "c" * 64}
    reporter.reserve_delivery_attempt(state, binding=first, reserved_at=now)

    with pytest.raises(
        reporter.DeliveryStateError,
        match="daily_delivery_binding_conflict",
    ):
        reporter.reserve_delivery_attempt(state, binding=changed, reserved_at=now)


def test_reservation_rejects_timestamp_from_a_different_sofia_date(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    binding = reporter.delivery_binding(
        now=datetime(2026, 7, 25, 20, tzinfo=timezone.utc),
        channel_id=reporter.DEFAULT_CHANNEL_ID,
        window_hours=24,
        report_count=1,
        report_set_digest="a" * 64,
        message_digest="b" * 64,
        message_bytes=42,
    )

    with pytest.raises(
        reporter.DeliveryStateError,
        match="delivery_binding_date_mismatch",
    ):
        reporter.reserve_delivery_attempt(
            state,
            binding=binding,
            reserved_at=datetime(2026, 7, 25, 22, tzinfo=timezone.utc),
        )

    assert os.listdir(state) == []


def test_partial_attempt_receipt_after_crash_blocks_re_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_dir(tmp_path)
    public = _public_report_dir(tmp_path)
    partial = state / "attempt-2026-07-25.json"
    partial.write_bytes(b'{"schema":')
    partial.chmod(0o600)
    monkeypatch.setattr(
        reporter,
        "deliver_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid pre-reservation must suppress delivery")
        ),
    )

    result = reporter.main(
        [
            "--public-report-dir",
            str(public),
            "--state-dir",
            str(state),
            "--now",
            "2026-07-25T06:00:00Z",
        ]
    )

    assert result == 1
    assert partial.read_bytes() == b'{"schema":'


def test_network_send_starts_only_after_durable_reservation_and_restart_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_dir(tmp_path)
    public = _public_report_dir(tmp_path)
    calls = 0

    def deliver(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        attempt = state / "attempt-2026-07-25.json"
        assert attempt.is_file()
        assert attempt.stat().st_mode & 0o777 == 0o600
        payload = json.loads(attempt.read_text(encoding="ascii"))
        assert payload["schema"] == reporter.ATTEMPT_SCHEMA
        assert payload["network_send_authorized"] is True
        return {"status": "PASS", "message_id": "123"}

    monkeypatch.setattr(reporter, "deliver_once", deliver)
    argv = [
        "--public-report-dir",
        str(public),
        "--state-dir",
        str(state),
        "--now",
        "2026-07-25T06:00:00Z",
    ]

    assert reporter.main(argv) == 0
    assert reporter.main(argv) == 0

    assert calls == 1
    attempt = json.loads(
        (state / "attempt-2026-07-25.json").read_text(encoding="ascii")
    )
    outcome = json.loads(
        (state / "outcome-2026-07-25.json").read_text(encoding="ascii")
    )
    assert outcome["attempt_receipt_sha256"] == attempt["attempt_receipt_sha256"]
    assert outcome["binding_sha256"] == attempt["binding_sha256"]
    assert outcome["result"] == {
        "status": "PASS",
        "blocker": None,
        "message_id": "123",
    }
    assert json.loads((state / "latest.json").read_text(encoding="ascii")) == outcome


def test_crash_after_reservation_before_send_is_missed_not_duplicated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_dir(tmp_path)
    public = _public_report_dir(tmp_path)
    argv = [
        "--public-report-dir",
        str(public),
        "--state-dir",
        str(state),
        "--now",
        "2026-07-25T06:00:00Z",
    ]
    calls = 0

    def crash(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated process crash")

    monkeypatch.setattr(reporter, "deliver_once", crash)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        reporter.main(argv)

    monkeypatch.setattr(
        reporter,
        "deliver_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("restart must not retry an ambiguous attempt")
        ),
    )
    assert reporter.main(argv) == 0
    assert calls == 1
    assert (state / "attempt-2026-07-25.json").is_file()
    assert not (state / "outcome-2026-07-25.json").exists()


def test_state_directory_symlink_is_rejected_before_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = _state_dir(tmp_path)
    public = _public_report_dir(tmp_path)
    alias = tmp_path / "state-alias"
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(
        reporter,
        "deliver_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("state symlink must suppress delivery")
        ),
    )

    result = reporter.main(
        [
            "--public-report-dir",
            str(public),
            "--state-dir",
            str(alias),
            "--now",
            "2026-07-25T06:00:00Z",
        ]
    )

    assert result == 1
    assert os.listdir(real) == []
