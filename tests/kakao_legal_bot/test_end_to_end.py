"""One full trip: Iris webhook → law lookup → answer back into the room.

Only the two network edges are faked (the LLM endpoint and Iris itself).
Everything between them — trigger, storage, tool loop, law client, the
5-second race, message splitting, the audit log — is the real code.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from kakao_legal_bot.app.agent import LegalAgent  # noqa: E402
from kakao_legal_bot.app.iris import IrisClient  # noqa: E402
from kakao_legal_bot.app.lawapi.client import LawApiClient  # noqa: E402
from kakao_legal_bot.app.llm import LlmClient  # noqa: E402
from kakao_legal_bot.app.pipeline import Pipeline  # noqa: E402
from kakao_legal_bot.app.rag.store import RagStore  # noqa: E402
from kakao_legal_bot.app.sender import Sender  # noqa: E402
from kakao_legal_bot.app.services import Services  # noqa: E402

try:
    from kakao_legal_bot.app.main import create_app
except RuntimeError as exc:  # pragma: no cover
    pytest.skip(f"fastapi extras missing: {exc}", allow_module_level=True)

LAW_SEARCH = {
    "LawSearch": {
        "law": [
            {
                "법령일련번호": "001234",
                "법령명한글": "주택임대차보호법",
                "시행일자": "20231019",
                "소관부처명": "법무부",
            }
        ]
    }
}


class FakeIris:
    """Records every /reply Iris would have received."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.replies: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        # Async handler: a blocking sleep here would freeze the very event
        # loop whose responsiveness this test is about.
        async def handler(request: httpx.Request) -> httpx.Response:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.replies.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True})

        return httpx.MockTransport(handler)


def llm_transport(script: list[dict], delay: float = 0.0) -> httpx.MockTransport:
    queue = list(script)

    async def handler(request: httpx.Request) -> httpx.Response:
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(200, json=queue.pop(0) if queue else script[-1])

    return httpx.MockTransport(handler)


def law_transport() -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(LAW_SEARCH, ensure_ascii=False))

    return httpx.MockTransport(handler)


def build_stack(settings, db, tmp_path, *, llm_script, llm_delay=0.0, iris_delay=0.0):
    fake_iris = FakeIris(delay=iris_delay)
    iris = IrisClient(settings, client=httpx.AsyncClient(transport=fake_iris.transport()))
    llm = LlmClient(
        provider="anthropic",
        api_key="test",
        base_url="https://llm.test",
        model="test-model",
        client=httpx.AsyncClient(transport=llm_transport(llm_script, llm_delay)),
    )
    law = LawApiClient(
        oc="test-oc", client=httpx.AsyncClient(transport=law_transport()), cache_ttl_s=0
    )
    rag = RagStore(tmp_path / "rag-e2e.sqlite3")
    rag.upsert_document(
        "실무메모.md",
        "임대차 실무 메모",
        ["보증금 반환은 목적물 인도와 동시이행 관계에 있다는 것이 실무의 확립된 태도다."],
    )
    services = Services(
        settings=settings,
        db=db,
        iris=iris,
        sender=Sender(settings, db, iris),
        agent=LegalAgent(settings, llm, rag=rag, law=law),
        rag=rag,
        law=law,
        llm=llm,
        semaphore=asyncio.Semaphore(4),
    )
    app = create_app(services)
    app.state.pipeline = Pipeline(services)
    return app, services, fake_iris


def webhook_payload(text: str, log_id: str = "1") -> dict:
    return {
        "msg": text,
        "room": "김변호사 상담 - 홍길동",
        "sender": "홍길동",
        "json": {
            "_id": log_id,
            "chat_id": "room-e2e",
            "user_id": "uid-hong",
            "type": "1",
            "v": '{"isSingleChat": true}',
        },
    }


def tool_turn(name: str, arguments: dict) -> dict:
    return {
        "content": [{"type": "tool_use", "id": "c1", "name": name, "input": arguments}],
        "stop_reason": "tool_use",
    }


def text_turn(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


ANSWER = (
    "결론부터 말씀드리면 보증금 반환은 집을 비워주는 것과 동시에 이뤄집니다.\n\n"
    "주택임대차보호법이 적용되고, 실무상 동시이행 관계로 봅니다.\n\n"
    "지금 하실 일: ① 계약 종료 통지 내역 확인 ② 내용증명 발송 검토"
)


def test_full_round_trip_with_a_law_lookup(settings, db, tmp_path):
    app, services, fake_iris = build_stack(
        settings,
        db,
        tmp_path,
        llm_script=[
            tool_turn("search_law", {"query": "주택임대차보호법"}),
            tool_turn("search_local_docs", {"query": "보증금 반환 동시이행"}),
            text_turn(ANSWER),
        ],
    )

    with TestClient(app) as client:
        started = time.monotonic()
        response = client.post("/iris/webhook", json=webhook_payload("전세금을 안 돌려줘요"))
        webhook_ms = (time.monotonic() - started) * 1000

        assert response.status_code == 200
        assert webhook_ms < 500  # Iris is never made to wait on the model

        _wait_until(lambda: any(ANSWER.split("\n")[0] in r["data"] for r in fake_iris.replies))

        _wait_until(lambda: any("[3/3]" in r["data"] for r in fake_iris.replies))

        # Assert inside the context: leaving it runs the lifespan shutdown,
        # which closes the database this stack is sharing.
        assert {reply["room"] for reply in fake_iris.replies} == {"room-e2e", "lawyer-room"}
        to_client = [r["data"] for r in fake_iris.replies if r["room"] == "room-e2e"]
        to_lawyer = [r["data"] for r in fake_iris.replies if r["room"] == "lawyer-room"]

        joined = "\n".join(to_client)
        assert "모아입니다" in joined  # first contact greeting
        assert "동시이행" in joined

        # The lawyer learns who applied before the answer exists, and what
        # the client was finally told. Two alerts — the answer was quick,
        # so there was no 90-second mark to report.
        assert len(to_lawyer) == 2
        assert "[1/3]" in to_lawyer[0]
        assert "홍길동" in to_lawyer[0]
        assert "전세금을 안 돌려줘요" in to_lawyer[0]
        assert "[3/3]" in to_lawyer[1]
        assert "주택임대차보호법" in to_lawyer[1]  # 근거까지 함께

        logged = db._query("SELECT * FROM answers")
        assert len(logged) == 1
        citations = json.loads(logged[0]["citations"])
        assert any("주택임대차보호법" in citation for citation in citations)
        assert json.loads(logged[0]["tools_used"]) == ["search_law", "search_local_docs"]

        history = [message.text for message in db.recent_messages("room-e2e")]
        assert "전세금을 안 돌려줘요" in history
        assert ANSWER in history


def test_slow_model_still_gets_a_message_out_inside_the_deadline(settings, db, tmp_path):
    object.__setattr__(settings, "ack_deadline_ms", 300)
    app, _services, fake_iris = build_stack(
        settings, db, tmp_path, llm_script=[text_turn("느리게 도착한 답변입니다.")], llm_delay=1.2
    )
    db.upsert_room("room-e2e", "상담방", "direct")
    db.set_room_flag("room-e2e", "intro_sent", 1)
    db.set_room_flag("room-e2e", "first_alerts_done", 1)  # not first contact

    with TestClient(app) as client:
        started = time.monotonic()
        client.post("/iris/webhook", json=webhook_payload("복잡한 질문"))

        _wait_until(lambda: bool(fake_iris.replies))
        first_message_s = time.monotonic() - started
        _wait_until(lambda: len(fake_iris.replies) >= 2)

    # The whole point: something is in the room well inside Kakao's window.
    assert first_message_s < 3.0
    assert fake_iris.replies[0]["data"] == settings.ack_text
    assert fake_iris.replies[-1]["data"] == "느리게 도착한 답변입니다."


def test_unreachable_iris_falls_back_to_the_outbox(settings, db, tmp_path):
    object.__setattr__(settings, "iris_send_mode", "hybrid")
    app, services, _fake_iris = build_stack(
        settings, db, tmp_path, llm_script=[text_turn("답변입니다")]
    )

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    services.iris._client = httpx.AsyncClient(transport=httpx.MockTransport(dead))
    db.upsert_room("room-e2e", "상담방", "direct")
    db.set_room_flag("room-e2e", "intro_sent", 1)
    db.set_room_flag("room-e2e", "first_alerts_done", 1)  # not first contact

    with TestClient(app) as client:
        client.post("/iris/webhook", json=webhook_payload("질문"))
        _wait_until(lambda: db.outbox_depth() > 0)

        # The relay would now pull it, exactly as moa_relay.py does.
        pulled = client.get("/outbox", headers={"X-Outbox-Token": "outbox-token"}).json()

    assert pulled["messages"][0]["text"] == "답변입니다"
    assert pulled["messages"][0]["room"] == "room-e2e"


def test_long_answers_arrive_as_several_readable_bubbles(settings, db, tmp_path):
    object.__setattr__(settings, "kakao_max_chars", 200)
    long_answer = "\n\n".join(f"{index}번 문단입니다. " + "내용" * 60 for index in range(3))
    app, _services, fake_iris = build_stack(
        settings, db, tmp_path, llm_script=[text_turn(long_answer)]
    )
    db.upsert_room("room-e2e", "상담방", "direct")
    db.set_room_flag("room-e2e", "intro_sent", 1)
    db.set_room_flag("room-e2e", "first_alerts_done", 1)  # not first contact

    with TestClient(app) as client:
        client.post("/iris/webhook", json=webhook_payload("긴 답변 주세요"))
        _wait_until(lambda: len(fake_iris.replies) >= 3)

    assert all(len(reply["data"]) <= 200 for reply in fake_iris.replies)
    assert "0번 문단입니다." in fake_iris.replies[0]["data"]


def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")
