"""대화 전체 보관소 — 방 라벨(이름/키워드+날짜)과 내보내기.

messages 표는 프롬프트 문맥용이라 24턴 트림·90일 정리가 걸려 있습니다.
상담 기록은 별도의 archive 표에 영구히 남고, 방마다 "홍길동-2026-08-29"
또는 "대여금-2026-08-29" 라벨이 붙어 내보내기 파일 이름이 됩니다.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from kakao_legal_bot.app.archive import (
    export_room,
    render_room_markdown,
    room_label_base,
    safe_filename,
)
from kakao_legal_bot.app.iris import IrisClient
from kakao_legal_bot.app.pipeline import Pipeline
from kakao_legal_bot.app.sender import Sender
from kakao_legal_bot.app.services import Services

from .conftest import FakeAgent, FakeSender, make_event


TODAY = time.strftime("%Y-%m-%d")


def wiring(settings, db):
    sender = FakeSender()
    services = Services(
        settings=settings,
        db=db,
        iris=IrisClient(settings),
        sender=sender,
        agent=FakeAgent("답변입니다."),
        semaphore=asyncio.Semaphore(2),
    )
    return Pipeline(services), sender


# ── 보관은 트림·정리와 무관하게 전량 남는다 ──────────────────────────────
def test_the_archive_keeps_everything_the_context_trim_throws_away(db):
    for index in range(40):
        db.add_message("room-a", "user", f"질문 {index}", keep_last=5, archive=True)

    context = db.recent_messages("room-a", limit=100)
    assert len(context) == 5  # 문맥은 트림되고
    assert db.archive_depth("room-a") == 40  # 기록은 전부 남는다
    turns = db.room_archive("room-a")
    assert turns[0].text == "질문 0" and turns[-1].text == "질문 39"


def test_purging_old_context_does_not_touch_the_archive(db):
    db.add_message("room-a", "user", "오래된 질문", archive=True)
    db._exec("UPDATE messages SET created_at = created_at - 200*86400")
    db._exec("UPDATE archive SET created_at = created_at - 200*86400")

    assert db.purge_old_messages(90) == 1
    assert db.archive_depth("room-a") == 1  # 90일 정리는 문맥에만


def test_archive_retention_zero_means_forever(db):
    db.add_message("room-a", "user", "질문", archive=True)
    db._exec("UPDATE archive SET created_at = created_at - 3650*86400")
    assert db.purge_old_archive(0) == 0
    assert db.archive_depth() == 1
    assert db.purge_old_archive(30) == 1  # 기간을 명시했을 때만 지운다


def test_archive_off_stores_nothing_extra(db):
    db.add_message("room-a", "user", "질문", archive=False)
    assert db.archive_depth() == 0


def test_the_archive_keeps_the_display_name_even_when_context_does_not(db):
    # 문맥(messages)은 STORE_RAW_SENDER=false 라 이름이 비지만, 보관소는
    # 변호사의 상담 기록이라 표시이름 그대로 남습니다.
    db.add_message(
        "room-a", "user", "질문", sender="", sender_key="abc123",
        archive=True, archive_sender="홍길동",
    )
    assert db.recent_messages("room-a")[0].sender == ""
    assert db.room_archive("room-a")[0].sender == "홍길동"


# ── 방 라벨 ──────────────────────────────────────────────────────────────
def test_a_known_name_labels_the_room_name_plus_date():
    assert room_label_base("홍길동", "아무 질문") == f"홍길동-{TODAY}"


def test_without_a_name_the_civil_case_type_is_the_keyword():
    label = room_label_base("", "친구가 빌려준 돈을 안 갚아요")
    assert label == f"대여금-{TODAY}"


def test_without_a_name_the_crime_name_is_the_keyword():
    label = room_label_base("", "옆집 사람이 제 물건을 훔쳐갔어요")
    assert label == f"절도-{TODAY}"


def test_without_any_keyword_the_question_snippet_is_used():
    label = room_label_base("", "안녕하세요 뭐 좀 여쭤볼게요")
    assert label.endswith(f"-{TODAY}")
    assert label.startswith("안녕하세요")


def test_an_empty_first_event_still_gets_a_label():
    assert room_label_base("", "") == f"상담-{TODAY}"


def test_duplicate_labels_get_a_counter(db):
    db.upsert_room("room-1")
    db.upsert_room("room-2")
    db.upsert_room("room-3")
    assert db.set_room_label("room-1", "홍길동-2026-08-29") == "홍길동-2026-08-29"
    assert db.set_room_label("room-2", "홍길동-2026-08-29") == "홍길동-2026-08-29-2"
    assert db.set_room_label("room-3", "홍길동-2026-08-29") == "홍길동-2026-08-29-3"


def test_a_labelled_room_keeps_its_first_label(db):
    db.upsert_room("room-1")
    db.set_room_label("room-1", "홍길동-2026-08-29")
    # 다음 날 다시 물어봐도 방 이름은 그대로 — 기록이 흩어지면 안 됩니다.
    assert db.set_room_label("room-1", "홍길동-2026-08-30") == "홍길동-2026-08-29"


def test_safe_filename_strips_path_characters():
    assert safe_filename("홍길동/절도:2026") == "홍길동절도2026"
    assert safe_filename("  ") == "상담"


# ── 파이프라인 통합 — 첫 질문에 라벨이 붙고 알림에 쓰인다 ────────────────
@pytest.mark.asyncio
async def test_the_first_question_names_the_room_after_the_sender(settings, db):
    pipeline, sender = wiring(settings, db)

    await pipeline.handle(make_event("이혼하고 싶어요", room_id="room-p", direct=True))
    for _ in range(40):
        await asyncio.sleep(0.01)
        if sender.lawyer_notes:
            break

    room = db.get_room("room-p")
    assert room["label"] == f"홍길동-{TODAY}"
    note = next(note for note in sender.lawyer_notes if "새 상담" in note)
    assert f"홍길동-{TODAY}" in note  # 알림의 "방:" 이 라벨을 쓴다


@pytest.mark.asyncio
async def test_without_a_sender_name_the_room_is_named_by_keyword(settings, db):
    pipeline, sender = wiring(settings, db)

    await pipeline.handle(
        make_event("전세보증금을 돌려받지 못하고 있어요", room_id="room-k", sender="", direct=True)
    )
    for _ in range(40):
        await asyncio.sleep(0.01)
        if sender.lawyer_notes:
            break

    room = db.get_room("room-k")
    assert room["label"] == f"임대차보증금반환-{TODAY}"


@pytest.mark.asyncio
async def test_both_sides_of_the_conversation_are_archived(settings, db):
    """상담자의 질문(파이프라인)과 봇의 답변(Sender) 둘 다 남아야 기록입니다."""
    pipeline, _ = wiring(settings, db)
    await pipeline.handle(make_event("질문입니다", room_id="room-b", direct=True))

    real_sender = Sender(settings, db, IrisClient(settings))

    async def fake_deliver(room_id, chunk):  # noqa: ANN001
        return True

    real_sender._deliver = fake_deliver
    await real_sender.send("room-b", "봇의 답변입니다")

    turns = db.room_archive("room-b")
    texts = [turn.text for turn in turns]
    assert "질문입니다" in texts and "봇의 답변입니다" in texts
    bot_turn = next(turn for turn in turns if turn.role == "bot")
    assert bot_turn.sender == settings.bot_name
    user_turn = next(turn for turn in turns if turn.role == "user")
    assert user_turn.sender == "홍길동"  # 보관소에는 표시이름 그대로


@pytest.mark.asyncio
async def test_archive_disabled_respects_the_switch(settings, db):
    object.__setattr__(settings, "archive_enabled", False)
    pipeline, _ = wiring(settings, db)
    await pipeline.handle(make_event("질문입니다", room_id="room-off", direct=True))
    assert db.archive_depth("room-off") == 0


# ── 내보내기 ─────────────────────────────────────────────────────────────
def test_the_export_contains_report_and_full_transcript(db, tmp_path):
    db.upsert_room("room-e", "테스트방", "direct")
    db.set_room_label("room-e", "홍길동-2026-08-29")
    db.get_or_create_consultation("room-e", "홍길동")
    intake = db.open_intake("room-e", "내용증명", "대여금")
    db.update_intake(
        int(intake["id"]),
        report="# 상담보고서\n채무자 김철수, 3천만원, 변제기 도과",
        missing="이자 약정 여부",
        status="report_review",
    )
    db.add_message("room-e", "user", "돈을 빌려줬는데 안 갚아요",
                   archive=True, archive_sender="홍길동")
    db.add_message("room-e", "bot", "변제기와 금액을 알려주세요",
                   sender="모아", archive=True)

    path = export_room(db, "room-e", tmp_path)
    assert path.name == "홍길동-2026-08-29.md"
    text = path.read_text(encoding="utf-8")

    assert text.startswith("# 홍길동-2026-08-29")
    assert "접수번호" in text
    assert "변제기 도과" in text  # 상담보고서 전문
    assert "미확인 사항: 이자 약정 여부" in text
    assert "돈을 빌려줬는데 안 갚아요" in text  # 대화 전체
    assert "**홍길동**" in text and "**모아**" in text


def test_an_unlabelled_room_exports_under_its_room_id(db, tmp_path):
    db.upsert_room("room-x")
    db.add_message("room-x", "user", "질문", archive=True)
    path = export_room(db, "room-x", tmp_path)
    assert path.name == "room-x.md"


def test_rendering_an_empty_room_says_so_instead_of_crashing(db):
    db.upsert_room("room-empty")
    text = render_room_markdown(db, "room-empty")
    assert "보관된 대화가 없습니다" in text
