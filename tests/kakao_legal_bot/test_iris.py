"""Iris payloads have changed shape across releases; parsing must not care."""

from __future__ import annotations

from kakao_legal_bot.app.iris import IrisEvent, split_for_kakao


def test_parses_the_common_payload_shape():
    event = IrisEvent.parse(
        {
            "msg": "안녕하세요",
            "room": "김변호사 상담",
            "sender": "홍길동",
            "json": {
                "_id": "4242",
                "chat_id": "18446744073709551615",
                "user_id": "777",
                "message": "안녕하세요",
                "type": "1",
                "created_at": "1700000000",
            },
        }
    )
    assert event.room_id == "18446744073709551615"
    assert event.room_name == "김변호사 상담"
    assert event.sender_name == "홍길동"
    assert event.sender_id == "777"
    assert event.text == "안녕하세요"
    assert event.is_text
    assert event.event_key == "log:4242"


def test_parses_a_flat_payload():
    event = IrisEvent.parse(
        {"chat_id": "1", "message": "테스트", "sender_name": "김철수", "type": "1", "id": "9"}
    )
    assert event.room_id == "1"
    assert event.text == "테스트"
    assert event.sender_name == "김철수"


def test_json_field_may_arrive_as_a_string():
    event = IrisEvent.parse(
        {"msg": "hi", "room": "방", "json": '{"chat_id": "55", "user_id": "2", "_id": "3"}'}
    )
    assert event.room_id == "55"
    assert event.sender_id == "2"


def test_room_id_falls_back_to_the_room_name():
    event = IrisEvent.parse({"msg": "hi", "room": "이름만있는방"})
    assert event.room_id == "이름만있는방"


def test_millisecond_timestamps_are_normalised():
    event = IrisEvent.parse({"msg": "hi", "chat_id": "1", "created_at": "1700000000000"})
    assert 1_699_999_000 < event.created_at < 1_700_001_000


def test_single_chat_flag_is_read_from_v():
    event = IrisEvent.parse(
        {"msg": "hi", "chat_id": "1", "json": {"v": '{"isSingleChat": true}'}}
    )
    assert event.is_direct_chat is True


def test_media_messages_are_not_text():
    assert not IrisEvent.parse({"msg": "", "chat_id": "1", "type": "2"}).is_text


def test_missing_log_id_still_yields_a_stable_dedupe_key():
    payload = {"msg": "같은 말", "chat_id": "1", "user_id": "7"}
    assert IrisEvent.parse(payload).event_key == IrisEvent.parse(payload).event_key


def test_split_keeps_short_answers_whole():
    assert split_for_kakao("짧은 답변", 900) == ["짧은 답변"]


def test_split_breaks_on_paragraphs():
    text = "\n\n".join(["가" * 400, "나" * 400, "다" * 400])
    chunks = split_for_kakao(text, 900)
    assert len(chunks) == 2
    assert all(len(chunk) <= 900 for chunk in chunks)


def test_split_handles_one_oversized_paragraph():
    chunks = split_for_kakao("라" * 2500, 900)
    assert len(chunks) == 3
    assert "".join(chunks) == "라" * 2500


def test_split_of_empty_text_sends_nothing():
    assert split_for_kakao("   ", 900) == []
