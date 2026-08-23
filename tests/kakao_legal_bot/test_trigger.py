"""The bot must be silent unless it was actually called."""

from __future__ import annotations

from kakao_legal_bot.app.trigger import Action, decide, extract_email, strip_mention

from .conftest import make_event


def test_group_room_requires_the_bot_name(settings):
    decision = decide(make_event("전세금 못 받으면 어떻게 하죠?"), settings, room_kind="group")
    assert decision.action is Action.IGNORE
    assert decision.reason == "not addressed"


def test_mention_triggers_and_name_is_stripped(settings):
    decision = decide(make_event("모아야, 전세금 못 받으면 어떻게 하죠?"), settings, room_kind="group")
    assert decision.action is Action.ANSWER
    assert decision.question == "전세금 못 받으면 어떻게 하죠?"


def test_trailing_mention_is_also_stripped(settings):
    decision = decide(make_event("전세금 반환 절차 알려줘 모아"), settings, room_kind="group")
    assert decision.action is Action.ANSWER
    assert decision.question == "전세금 반환 절차 알려줘"


def test_direct_room_answers_without_a_mention(settings):
    decision = decide(make_event("계약서 검토 부탁드려요"), settings, room_kind="direct")
    assert decision.action is Action.ANSWER
    assert decision.question == "계약서 검토 부탁드려요"


def test_payload_flag_marks_an_unknown_room_direct(settings):
    decision = decide(make_event("안녕하세요", direct=True), settings, room_kind="unknown")
    assert decision.action is Action.ANSWER


def test_bot_never_answers_itself(settings):
    event = make_event("네, 확인했습니다", sender="모아", sender_id="bot")
    assert decide(event, settings, room_kind="direct").action is Action.IGNORE


def test_muted_room_stays_silent_even_when_called(settings):
    decision = decide(make_event("모아 도와줘"), settings, room_kind="direct", muted=True)
    assert decision.action is Action.IGNORE
    assert decision.reason == "room muted"


def test_lawyer_takeover_silences_unaddressed_messages(settings):
    silent = decide(make_event("그럼 언제 오시나요"), settings, room_kind="direct", lawyer_takeover=True)
    assert silent.action is Action.IGNORE

    called = decide(make_event("모아 조문 좀 찾아줘"), settings, room_kind="direct", lawyer_takeover=True)
    assert called.action is Action.ANSWER


def test_lawyer_chatter_is_not_a_question(settings):
    event = make_event("제가 내일 연락드릴게요", sender_id="lawyer-uid")
    assert decide(event, settings, room_kind="direct").action is Action.IGNORE


def test_non_text_messages_are_ignored(settings):
    event = make_event("사진", msg_type="2")
    assert decide(event, settings, room_kind="direct").action is Action.IGNORE


def test_client_command_is_routed(settings):
    decision = decide(make_event("/이메일 hong@example.com"), settings, room_kind="direct")
    assert decision.action is Action.COMMAND
    assert decision.command == "set_email"
    assert decision.args == "hong@example.com"


def test_lawyer_command_requires_the_lawyer(settings):
    from_client = decide(make_event("/승인 3"), settings, room_kind="direct")
    assert from_client.action is Action.IGNORE

    from_lawyer = decide(make_event("/승인 3", sender_id="lawyer-uid"), settings, room_kind="direct")
    assert from_lawyer.action is Action.COMMAND
    assert from_lawyer.command == "draft_approve"
    assert from_lawyer.args == "3"


def test_bot_name_prefixed_command(settings):
    decision = decide(make_event("/모아 도움말"), settings, room_kind="direct")
    assert decision.command == "help"


def test_strip_mention_handles_particles():
    names = ["모아", "moa"]
    assert strip_mention("모아! 질문있어요", names) == "질문있어요"
    assert strip_mention("모아님 안녕하세요", names) == "안녕하세요"
    assert strip_mention("모아가 좋아요", names) == "가 좋아요"  # not a call, but harmless


def test_extract_email():
    assert extract_email("메일은 hong.kim+law@example.co.kr 입니다") == "hong.kim+law@example.co.kr"
    assert extract_email("없어요") == ""
