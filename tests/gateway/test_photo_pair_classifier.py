from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tools.pa_photo_pair_classifier import classify_photo_pair
from gateway.platforms.base import MessageEvent, MessageType


def _photo_history_entry(
    timestamp: datetime,
    *,
    file_id: str = "before-file",
    file_url: str = "https://api.telegram.org/file/bot123/photos/before.jpg",
    text: str = "",
) -> dict:
    content = "\n\n".join(
        part
        for part in (
            text,
            "\n".join(
                [
                    "[media#1 image/jpeg",
                    f"  file_id={file_id}",
                    f"  file_url={file_url}",
                    "]",
                ]
            ),
        )
        if part
    )
    return {
        "role": "user",
        "content": content,
        "timestamp": timestamp.isoformat(),
    }


def _photo_event(timestamp: datetime, *, text: str = "") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO,
        message_id="after-message",
        timestamp=timestamp,
        media_refs=[
            {
                "file_id": "after-file",
                "file_url": "https://api.telegram.org/file/bot123/photos/after.jpg",
                "mime_type": "image/jpeg",
            }
        ],
    )


def test_two_photos_thirty_seconds_apart_are_classified_as_pair():
    base = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    history = [
        _photo_history_entry(
            base,
            text="before photo for job no AM/JOB/2605/0112",
        )
    ]
    event = _photo_event(
        base + timedelta(seconds=30),
        text="after photo for job no AM/JOB/2605/0112",
    )

    result = classify_photo_pair(
        event,
        history,
        now=base + timedelta(seconds=31),
    )

    assert result is not None
    assert result.payload == {
        "before": {
            "file_id": "before-file",
            "getFile_url": "https://api.telegram.org/file/bot123/photos/before.jpg",
        },
        "after": {
            "file_id": "after-file",
            "getFile_url": "https://api.telegram.org/file/bot123/photos/after.jpg",
        },
        "confidence": 0.965,
        "classified_at": "2026-05-18T12:00:31Z",
    }


def test_two_photos_ten_minutes_apart_are_not_classified_as_pair():
    base = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    history = [_photo_history_entry(base)]
    event = _photo_event(base + timedelta(minutes=10))

    assert classify_photo_pair(event, history, now=base + timedelta(minutes=10)) is None


def test_matching_job_context_boosts_confidence():
    base = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    event = _photo_event(
        base + timedelta(seconds=60),
        text="after AM/JOB/2605/0112",
    )

    without_context = classify_photo_pair(
        event,
        [_photo_history_entry(base, text="before")],
        now=base + timedelta(seconds=61),
    )
    with_context = classify_photo_pair(
        event,
        [_photo_history_entry(base, text="before AM/JOB/2605/0112")],
        now=base + timedelta(seconds=61),
    )

    assert without_context is not None
    assert with_context is not None
    assert with_context.confidence > without_context.confidence


def test_gateway_wrapper_records_photo_pair_action(monkeypatch):
    from gateway import run as gateway_run

    base = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    recorded = []
    monkeypatch.setattr(
        gateway_run,
        "_record_pa_agent_action",
        lambda _ctx, **kwargs: recorded.append(kwargs) or True,
    )

    payload = gateway_run._classify_and_record_photo_pair(
        SimpleNamespace(),
        SimpleNamespace(job_brief=SimpleNamespace(enabled_toolsets=("pa-photo-pair",))),
        _photo_event(base + timedelta(seconds=30)),
        [_photo_history_entry(base)],
        session_key="whatsapp:chat-1",
        source="whatsapp",
        turn_id="turn-1",
    )

    assert payload is not None
    assert recorded[0]["action_type"] == "photo-pair-classified"
    assert recorded[0]["status"] == "executed"
    assert recorded[0]["payload"]["before"]["file_id"] == "before-file"


def test_gateway_wrapper_skips_photo_pair_without_toolset(monkeypatch):
    from gateway import run as gateway_run

    base = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    recorded = []
    monkeypatch.setattr(
        gateway_run,
        "_record_pa_agent_action",
        lambda _ctx, **kwargs: recorded.append(kwargs) or True,
    )

    payload = gateway_run._classify_and_record_photo_pair(
        SimpleNamespace(),
        SimpleNamespace(job_brief=SimpleNamespace(enabled_toolsets=("memory", "file"))),
        _photo_event(base + timedelta(seconds=30)),
        [_photo_history_entry(base)],
        session_key="whatsapp:chat-1",
        source="whatsapp",
        turn_id="turn-1",
    )

    assert payload is None
    assert recorded == []
