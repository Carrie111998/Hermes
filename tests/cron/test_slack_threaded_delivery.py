"""Regression tests for Slack root-summary → thread-body cron delivery."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from cron import scheduler
from gateway.config import Platform
import gateway.delivery as delivery
import tools.send_message_tool as send_message_tool


def test_build_slack_cron_summary_is_five_lines_or_less() -> None:
    summary = scheduler._build_slack_cron_summary(
        {"id": "job-1", "name": "무즈린 리서치"},
        "## 다음 액션\n- 첫 액션\n- 둘째 액션\n- 셋째 액션\n- 넷째 액션",
        duration_seconds=8,
        success=True,
    )

    assert len(summary.splitlines()) == 5
    assert summary.splitlines()[0].startswith("✅ 무즈린 리서치 완료")
    assert summary.splitlines()[-1] == "전체 본문은 이 메시지의 thread에 남겼습니다."


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"message_id": "171.001"}, "171.001"),
        ({"ts": "171.002"}, "171.002"),
        ({"raw_response": {"ts": "171.003"}}, "171.003"),
        (SimpleNamespace(message_id="171.004", raw_response=None), "171.004"),
    ],
)
def test_slack_message_id_from_result_accepts_native_send_shapes(result, expected) -> None:
    assert scheduler._slack_message_id_from_result(result) == expected


def test_slack_channel_delivery_sends_summary_root_then_full_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict]] = []

    async def fake_send(_platform, _config, _chat_id, content, **kwargs):
        sent.append((content, kwargs))
        return {"success": True, "message_id": "171.100" if len(sent) == 1 else "171.101"}

    monkeypatch.setattr(send_message_tool, "_send_to_platform", fake_send)
    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [{"platform": "slack", "chat_id": "C_TEST", "thread_id": None}],
    )
    monkeypatch.setattr(scheduler, "_resolve_origin", lambda _job: {})
    monkeypatch.setattr(
        scheduler,
        "load_config",
        lambda: {"cron": {"wrap_response": True, "slack_threaded_delivery": True}},
    )
    monkeypatch.setattr(
        delivery,
        "resolve_delivery_transport",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        __import__("gateway.config", fromlist=["load_gateway_config"]),
        "load_gateway_config",
        lambda: SimpleNamespace(
            platforms={Platform.SLACK: SimpleNamespace(enabled=True, extra={})}
        ),
    )

    error = scheduler._deliver_result(
        {"id": "job-1", "name": "무즈린 리서치", "deliver": "slack:C_TEST"},
        "## 핵심\n전체 상세 본문입니다.",
        duration_seconds=7,
        success=True,
    )

    assert error is None
    assert len(sent) == 2
    root, root_kwargs = sent[0]
    body, body_kwargs = sent[1]
    assert len(root.splitlines()) <= 5
    assert root_kwargs["thread_id"] is None
    assert body == "Cronjob Response: 무즈린 리서치\n(job_id: job-1)\n-------------\n\n## 핵심\n전체 상세 본문입니다.\n\nTo stop or manage this job, send me a new message (e.g. \"stop reminder 무즈린 리서치\")."
    assert body_kwargs["thread_id"] == "171.100"


def test_slack_channel_delivery_fails_without_a_root_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_send(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"success": True}

    monkeypatch.setattr(send_message_tool, "_send_to_platform", fake_send)
    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [{"platform": "slack", "chat_id": "C_TEST", "thread_id": None}],
    )
    monkeypatch.setattr(scheduler, "_resolve_origin", lambda _job: {})
    monkeypatch.setattr(
        scheduler,
        "load_config",
        lambda: {"cron": {"wrap_response": True, "slack_threaded_delivery": True}},
    )
    monkeypatch.setattr(delivery, "resolve_delivery_transport", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        __import__("gateway.config", fromlist=["load_gateway_config"]),
        "load_gateway_config",
        lambda: SimpleNamespace(
            platforms={Platform.SLACK: SimpleNamespace(enabled=True, extra={})}
        ),
    )

    error = scheduler._deliver_result(
        {"id": "job-2", "name": "무즈리아 QA", "deliver": "slack:C_TEST"},
        "상세 본문",
    )

    assert calls == 1
    assert error == "Slack threaded cron delivery could not determine root message id"
