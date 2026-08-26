"""국가법령 API 동기화 — 개정 법령·최근 판례를 받아 낡은 자료를 짚어 낸다.

가장 위험한 오류는 빠진 자료가 아니라 낡은 자료입니다. 여기서 붙잡아 두는
것은 두 가지입니다. **바뀐 것만 받아 오는가**, 그리고 **바뀌었을 때 어떤 기존
자료가 이제 틀렸는지 짚어 주는가.** 받아 놓기만 하면 낡은 설명은 그대로 남습니다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from kakao_legal_bot.app.wiki.graph import WikiGraph
from kakao_legal_bot.app.wiki.note import WikiNote
from kakao_legal_bot.app.wiki.sync import (
    LawSync,
    affected_notes,
    watched_laws,
)


@dataclass
class FakeDoc:
    title: str
    doc_id: str = ""
    number: str = ""
    date: str = ""
    actor: str = ""
    body: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class FakeLawApi:
    """법령 API 자리에 세우는 대역. 호출을 기록해 두고 그대로 돌려줍니다."""

    def __init__(self, laws: dict[str, FakeDoc] | None = None, cases: list[FakeDoc] | None = None):
        self.laws = laws or {}
        self.cases = cases or []
        self.searched: list[str] = []
        self.bodies_fetched: list[str] = []
        self.fail_on: set[str] = set()

    async def search_law(self, query: str, display: int = 5) -> list[FakeDoc]:
        self.searched.append(query)
        if query in self.fail_on:
            raise RuntimeError("연결 실패")
        doc = self.laws.get(query)
        return [doc] if doc else []

    async def get_law(self, law_id: str = "", mst: str = "") -> FakeDoc | None:
        self.bodies_fetched.append(law_id)
        for doc in self.laws.values():
            if doc.doc_id == law_id:
                return FakeDoc(title=doc.title, doc_id=law_id, body="제618조 임대차는 …")
        return None

    async def search_precedent(self, query: str, display: int = 5, **_: Any) -> list[FakeDoc]:
        self.searched.append(query)
        return self.cases[:display]

    async def get_precedent(self, prec_id: str) -> FakeDoc | None:
        self.bodies_fetched.append(prec_id)
        return FakeDoc(title="본문", doc_id=prec_id, body="판시사항 …")


CIVIL_618 = FakeDoc(
    title="민법",
    doc_id="001234",
    date="20230601",
    extra={"시행일자": "20230601", "법령명한글": "민법"},
)


@pytest.fixture
def vault(tmp_path):
    return tmp_path / "vault"


@pytest.fixture
def graph(tmp_path):
    store = WikiGraph(tmp_path / "graph.sqlite3")
    store.upsert_note(
        WikiNote(
            path="법령/민법-제618조.md",
            title="민법 제618조",
            kind="법령",
            effective_on="2016-02-04",
            statutes=["민법 제618조"],
        )
    )
    store.upsert_note(
        WikiNote(
            path="주석서/임대차.md",
            title="주석민법 임대차",
            kind="주석서",
            written_on="2019-03-01",
            statutes=["민법 제618조", "민법 제623조"],
        )
    )
    store.upsert_note(
        WikiNote(
            path="서적/최신교재.md",
            title="최신 민법강의",
            kind="서적",
            written_on="2024-05-01",
            statutes=["민법 제618조"],
        )
    )
    store.upsert_note(
        WikiNote(
            path="서적/형법각론.md",
            title="형법각론",
            kind="서적",
            written_on="2023-01-01",
            statutes=["형법 제329조"],
        )
    )
    yield store
    store.close()


# ── 무엇을 지켜볼 것인가 ─────────────────────────────────────────────────
def test_the_library_decides_what_to_watch(graph):
    """지켜볼 목록을 손으로 관리하면 반드시 빠뜨립니다."""
    laws = watched_laws(graph)
    assert laws[0] == "민법"  # 가장 많이 인용된 법
    assert "형법" in laws


def test_an_explicitly_named_law_is_added(graph):
    laws = watched_laws(graph, top=1, extra=["주임법"])
    assert "주택임대차보호법" in laws  # 약칭도 정식명으로


def test_an_empty_library_watches_nothing(tmp_path):
    store = WikiGraph(tmp_path / "empty.sqlite3")
    try:
        assert watched_laws(store) == []
    finally:
        store.close()


# ── 개정 감지 ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_newer_effective_date_is_fetched(vault, graph):
    api = FakeLawApi({"민법": CIVIL_618})
    result = await LawSync(api, vault, graph).sync_laws(["민법"])

    assert len(result.laws) == 1
    update = result.laws[0]
    assert update.effective_on == "2023-06-01"
    assert update.previous == "2016-02-04"  # 그래프가 알던 것
    assert (vault / "raw" / "법령" / "민법-2023-06-01.md").exists()


@pytest.mark.asyncio
async def test_the_same_effective_date_is_not_fetched_twice(vault, graph):
    api = FakeLawApi({"민법": CIVIL_618})
    sync = LawSync(api, vault, graph)
    await sync.sync_laws(["민법"])
    api.bodies_fetched.clear()

    second = await sync.sync_laws(["민법"])
    assert second.laws == []
    assert api.bodies_fetched == []  # 본문을 다시 받지 않는다


@pytest.mark.asyncio
async def test_the_fetched_law_is_written_as_a_note_we_can_read(vault, graph):
    api = FakeLawApi({"민법": CIVIL_618})
    await LawSync(api, vault, graph).sync_laws(["민법"])

    note = WikiNote.load(vault / "raw" / "법령" / "민법-2023-06-01.md")
    assert note.kind == "법령"
    assert note.effective_on == "2023-06-01"
    assert note.missing_required() == []  # 그대로 색인할 수 있다
    assert "민법 제618조" in note.statutes


@pytest.mark.asyncio
async def test_one_law_failing_does_not_stop_the_rest(vault, graph):
    api = FakeLawApi({"민법": CIVIL_618})
    api.fail_on = {"형법"}
    result = await LawSync(api, vault, graph).sync_laws(["형법", "민법"])

    assert [update.law for update in result.laws] == ["민법"]
    assert any("형법" in error for error in result.errors)


@pytest.mark.asyncio
async def test_a_law_the_api_does_not_know_is_skipped_quietly(vault, graph):
    result = await LawSync(FakeLawApi(), vault, graph).sync_laws(["없는법"])
    assert result.laws == [] and result.errors == []
    assert result.checked == 1


# ── 무엇이 이제 낡았는가 ─────────────────────────────────────────────────
def test_documents_older_than_the_amendment_are_named(graph):
    """이 목록이 코덱스의 숙제입니다."""
    affected = affected_notes(graph, "민법", "2023-06-01")
    titles = {item["title"] for item in affected}

    assert "주석민법 임대차" in titles  # 2019년 자료
    assert "최신 민법강의" not in titles  # 2024년 자료 — 개정을 알고 쓴 것
    assert "형법각론" not in titles  # 다른 법


def test_the_statute_note_itself_is_not_its_own_homework(graph):
    affected = affected_notes(graph, "민법", "2023-06-01")
    assert all(item["kind"] != "법령" for item in affected)


@pytest.mark.asyncio
async def test_the_sync_reports_what_needs_revising(vault, graph):
    api = FakeLawApi({"민법": CIVIL_618})
    result = await LawSync(api, vault, graph).sync_laws(["민법"])

    assert result.affected
    assert "영향받는 기존 자료" in result.summary()


@pytest.mark.asyncio
async def test_a_first_time_fetch_does_not_flag_everything(vault, tmp_path):
    """처음 받아 오는 법이면 개정된 것이 아니므로 숙제도 없습니다."""
    store = WikiGraph(tmp_path / "g.sqlite3")
    try:
        store.upsert_note(
            WikiNote(path="책.md", title="책", kind="서적", written_on="2019-01-01",
                     statutes=["민법 제618조"])
        )
        result = await LawSync(FakeLawApi({"민법": CIVIL_618}), vault, store).sync_laws(["민법"])
        assert result.laws[0].is_new
        assert result.affected == []
    finally:
        store.close()


# ── 판례 ─────────────────────────────────────────────────────────────────
RECENT = FakeDoc(
    title="임대차보증금반환",
    doc_id="p1",
    number="2026다12345",
    date="20260801",
    actor="대법원",
    extra={"선고일자": "20260801"},
)
OLD = FakeDoc(
    title="옛 판례",
    doc_id="p2",
    number="2015다1",
    date="20150101",
    actor="대법원",
    extra={"선고일자": "20150101"},
)


@pytest.mark.asyncio
async def test_only_precedents_after_the_cutoff_are_taken(vault, graph):
    api = FakeLawApi(cases=[RECENT, OLD])
    result = await LawSync(api, vault, graph).sync_precedents(["임대차"], since="2026-01-01")

    assert [update.case_no for update in result.cases] == ["2026다12345"]
    assert (vault / "raw" / "판례" / "2026다12345.md").exists()


@pytest.mark.asyncio
async def test_a_precedent_already_taken_is_not_taken_again(vault, graph):
    api = FakeLawApi(cases=[RECENT])
    sync = LawSync(api, vault, graph)
    await sync.sync_precedents(["임대차"], since="2026-01-01")

    second = await sync.sync_precedents(["임대차"], since="2026-01-01")
    assert second.cases == []


@pytest.mark.asyncio
async def test_the_precedent_note_carries_what_a_citation_needs(vault, graph):
    api = FakeLawApi(cases=[RECENT])
    await LawSync(api, vault, graph).sync_precedents(["임대차"], since="2026-01-01")

    note = WikiNote.load(vault / "raw" / "판례" / "2026다12345.md")
    assert note.case_no == "2026다12345"
    assert note.court == "대법원"
    assert note.decided_on == "2026-08-01"
    assert note.missing_required() == []


# ── 작업지시서 ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_worklist_has_both_jobs(vault, graph):
    api = FakeLawApi({"민법": CIVIL_618}, cases=[RECENT])
    sync = LawSync(api, vault, graph)
    result = await sync.sync_laws(["민법"])
    cases = await sync.sync_precedents(["임대차"], since="2026-01-01")
    result.cases.extend(cases.cases)

    path = sync.write_worklist(result)
    assert path is not None
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    jobs = {row["job"] for row in rows}
    assert jobs == {"wiki", "revise"}
    revise = [row for row in rows if row["job"] == "revise"]
    assert revise[0]["effective_on"] == "2023-06-01"
    assert "path" in revise[0] and "statute" in revise[0]


@pytest.mark.asyncio
async def test_nothing_changed_means_no_worklist_file(vault, graph):
    from kakao_legal_bot.app.wiki.sync import SyncResult

    sync = LawSync(FakeLawApi(), vault, graph)
    assert sync.write_worklist(SyncResult()) is None
    assert not (vault / "_sync-worklist.jsonl").exists()


@pytest.mark.asyncio
async def test_the_state_file_remembers_between_runs(vault, graph):
    api = FakeLawApi({"민법": CIVIL_618})
    sync = LawSync(api, vault, graph)
    await sync.sync_laws(["민법"])

    state = json.loads((vault / "_sync-state.json").read_text(encoding="utf-8"))
    assert state["laws"]["민법"]["effective_on"] == "2023-06-01"
    assert state["last_run"]


def test_a_corrupt_state_file_does_not_stop_the_run(vault, graph):
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "_sync-state.json").write_text("{망가진", encoding="utf-8")
    assert LawSync(FakeLawApi(), vault, graph).load_state()["laws"] == {}


# ── 서버가 알려 주는 몫 ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_server_tells_the_lawyer_what_changed(settings, db, tmp_path):
    """PC가 꺼져 있어도 변호사는 개정 사실을 압니다."""
    import asyncio as _asyncio

    from kakao_legal_bot.app.iris import IrisClient
    from kakao_legal_bot.app.main import run_law_sync_if_due
    from kakao_legal_bot.app.services import Services

    from .conftest import FakeAgent, FakeSender

    store = WikiGraph(tmp_path / "g.sqlite3")
    store.upsert_note(
        WikiNote(path="법령/민법.md", title="민법 제618조", kind="법령",
                 effective_on="2016-02-04", statutes=["민법 제618조"])
    )
    store.upsert_note(
        WikiNote(path="주석서/임대차.md", title="주석민법 임대차", kind="주석서",
                 written_on="2019-03-01", statutes=["민법 제618조"])
    )
    sender = FakeSender()
    for field_name, value in (
        ("law_sync_enabled", True),
        ("wiki_vault", tmp_path / "vault"),
    ):
        object.__setattr__(settings, field_name, value)
    services = Services(
        settings=settings,
        db=db,
        iris=IrisClient(settings),
        sender=sender,
        agent=FakeAgent("답변"),
        graph=store,
        law=FakeLawApi({"민법": CIVIL_618}),
        semaphore=_asyncio.Semaphore(1),
    )
    try:
        message = await run_law_sync_if_due(services, force=True)

        assert "민법 개정" in message
        assert "2016-02-04 → 2023-06-01" in message
        assert "주석민법 임대차" in message  # 이제 확인이 필요한 자료
        assert sender.lawyer_notes and "법령·판례 변동" in sender.lawyer_notes[0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_server_stays_quiet_when_nothing_changed(settings, db, tmp_path):
    import asyncio as _asyncio

    from kakao_legal_bot.app.iris import IrisClient
    from kakao_legal_bot.app.main import run_law_sync_if_due
    from kakao_legal_bot.app.services import Services

    from .conftest import FakeAgent, FakeSender

    store = WikiGraph(tmp_path / "g.sqlite3")
    store.upsert_note(
        WikiNote(path="법령/민법.md", title="민법 제618조", kind="법령",
                 effective_on="2023-06-01", statutes=["민법 제618조"])
    )
    sender = FakeSender()
    object.__setattr__(settings, "law_sync_enabled", True)
    object.__setattr__(settings, "wiki_vault", tmp_path / "vault")
    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=sender,
        agent=FakeAgent("답변"), graph=store, law=FakeLawApi({"민법": CIVIL_618}),
        semaphore=_asyncio.Semaphore(1),
    )
    try:
        assert await run_law_sync_if_due(services, force=True) == ""
        assert sender.lawyer_notes == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_sync_is_off_unless_switched_on(settings, db, tmp_path):
    import asyncio as _asyncio

    from kakao_legal_bot.app.iris import IrisClient
    from kakao_legal_bot.app.main import run_law_sync_if_due
    from kakao_legal_bot.app.services import Services

    from .conftest import FakeAgent, FakeSender

    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=FakeSender(),
        agent=FakeAgent("답변"), law=FakeLawApi(), semaphore=_asyncio.Semaphore(1),
    )
    assert await run_law_sync_if_due(services, force=True) == ""


@pytest.mark.asyncio
async def test_the_sync_does_not_run_twice_within_the_interval(settings, db, tmp_path):
    import asyncio as _asyncio

    from kakao_legal_bot.app.iris import IrisClient
    from kakao_legal_bot.app.main import run_law_sync_if_due
    from kakao_legal_bot.app.services import Services

    from .conftest import FakeAgent, FakeSender

    store = WikiGraph(tmp_path / "g.sqlite3")
    store.upsert_note(
        WikiNote(path="법령/민법.md", title="민법 제618조", kind="법령",
                 effective_on="2016-02-04", statutes=["민법 제618조"])
    )
    object.__setattr__(settings, "law_sync_enabled", True)
    object.__setattr__(settings, "wiki_vault", tmp_path / "vault")
    api = FakeLawApi({"민법": CIVIL_618})
    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=FakeSender(),
        agent=FakeAgent("답변"), graph=store, law=api, semaphore=_asyncio.Semaphore(1),
    )
    try:
        await run_law_sync_if_due(services, force=True)
        api.searched.clear()
        assert await run_law_sync_if_due(services) == ""  # 아직 하루가 안 지났다
        assert api.searched == []
    finally:
        store.close()
