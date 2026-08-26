"""국가법령 API → RAW → WIKI → LINT 를 주기적으로 돌린다.

이 데이터베이스에서 가장 위험한 오류는 빠진 자료가 아니라 **낡은 자료**입니다.
2019년에 쓴 주석서는 2023년 개정을 알 리가 없고, 그 사실을 아무도 알려주지
않으면 봇은 개정 전 조문을 자신 있게 인용합니다.

그래서 매일(또는 매주) 이렇게 돕니다.

    ① 우리 서가가 실제로 다루는 법령의 **현행 시행일**을 API로 확인
    ② 우리가 아는 것보다 새 것이면 본문을 받아 ``raw/법령/`` 에 넣는다
    ③ 최근 판례를 받아 ``raw/판례/`` 에 넣는다
    ④ 그 조문·판례를 다루던 **기존 자료 목록**을 뽑아 작업지시서로 만든다
    ⑤ 코덱스가 위키 노트를 쓰고, 낡은 서술을 최신 기준으로 고친다

④가 핵심입니다. 새 법령을 받아 놓기만 하면 낡은 설명은 그대로 남습니다.
"어떤 문서가 이제 틀렸을 수 있는가"까지 짚어 줘야 고쳐집니다.

무엇을 지켜볼지는 **서가가 정합니다** — 그래프에서 가장 많이 인용된 법령이
곧 이 사무실이 실제로 쓰는 법입니다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .citation import normalise_law_name, parse_statute
from .graph import STATUTE_ENTITY, WikiGraph
from .note import CASE, STATUTE, WikiNote

log = logging.getLogger(__name__)

STATE_FILE = "_sync-state.json"
WORKLIST_FILE = "_sync-worklist.jsonl"


def _today() -> str:
    return date.today().isoformat()


def _iso(raw: str) -> str:
    """API 가 주는 '20230601' · '2023.06.01' · '2023-06-01' 을 하나로."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    match = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", str(raw or ""))
    if match is None:
        return ""
    year, month, day = (int(value) for value in match.groups())
    if not (1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


# ── 결과 ─────────────────────────────────────────────────────────────────
@dataclass
class LawUpdate:
    law: str
    effective_on: str
    previous: str = ""
    law_id: str = ""
    title: str = ""
    path: str = ""

    @property
    def is_new(self) -> bool:
        return not self.previous


@dataclass
class CaseUpdate:
    case_no: str
    court: str
    decided_on: str
    title: str = ""
    doc_id: str = ""
    path: str = ""


@dataclass
class SyncResult:
    laws: list[LawUpdate] = field(default_factory=list)
    cases: list[CaseUpdate] = field(default_factory=list)
    affected: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    checked: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.laws or self.cases)

    def summary(self) -> str:
        parts = [f"법령 {len(self.laws)}건 개정", f"판례 {len(self.cases)}건 신규"]
        if self.affected:
            parts.append(f"영향받는 기존 자료 {len(self.affected)}건")
        if self.errors:
            parts.append(f"오류 {len(self.errors)}건")
        return " · ".join(parts)


# ── 무엇을 지켜볼 것인가 ─────────────────────────────────────────────────
def watched_laws(graph: WikiGraph, top: int = 30, extra: list[str] | None = None) -> list[str]:
    """서가가 실제로 다루는 법령. 많이 인용된 순서.

    지켜볼 목록을 손으로 관리하면 반드시 빠뜨립니다. 우리가 가진 자료가
    무엇을 인용하는지가 곧 우리가 쓰는 법입니다.
    """
    counts: dict[str, int] = {}
    for entity in graph.hubs(min_notes=1, kind=STATUTE_ENTITY, limit=5000):
        ref = parse_statute(entity.display)
        if ref is None:
            continue
        counts[ref.law] = counts.get(ref.law, 0) + entity.note_count
    ranked = sorted(counts, key=lambda law: counts[law], reverse=True)[:top]
    for law in extra or []:
        name = normalise_law_name(law)
        if name and name not in ranked:
            ranked.append(name)
    return ranked


def affected_notes(graph: WikiGraph, law: str, effective_on: str) -> list[dict[str, Any]]:
    """그 법을 다루면서 **개정보다 먼저 쓰인** 자료들.

    이 목록이 곧 코덱스의 숙제입니다. 새 법령을 받아 놓기만 해서는 낡은
    설명이 그대로 남습니다.
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entity in graph.hubs(min_notes=1, kind=STATUTE_ENTITY, limit=5000):
        ref = parse_statute(entity.display)
        if ref is None or ref.law != law:
            continue
        for row in graph.notes_for(entity.display, limit=200):
            path = str(row["path"])
            as_of = str(row["as_of"] or "")
            if path in seen or str(row["kind"]) == STATUTE:
                continue
            if as_of and as_of >= effective_on:
                continue  # 개정 이후에 쓰인 자료
            seen.add(path)
            found.append(
                {
                    "path": path,
                    "title": str(row["title"]),
                    "kind": str(row["kind"]),
                    "as_of": as_of,
                    "statute": entity.display,
                    "effective_on": effective_on,
                }
            )
    return found


# ── 동기화 ───────────────────────────────────────────────────────────────
class LawSync:
    """API 에서 받아 ``raw/`` 에 넣고, 무엇이 낡았는지 짚어 낸다.

    LLM 을 쓰지 않습니다. 받아 오고, 견주고, 목록을 만드는 데까지가 여기 일이고,
    글을 고치는 것은 코덱스 몫입니다. 그래야 실패해도 자료가 망가지지 않습니다.
    """

    def __init__(self, client: Any, vault: Path | str, graph: WikiGraph | None = None) -> None:
        self.client = client
        self.vault = Path(vault)
        self.graph = graph
        self.raw = self.vault / "raw"
        self.state_path = self.vault / STATE_FILE

    # ── 상태 ─────────────────────────────────────────────────────────────
    def load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"laws": {}, "cases": {}, "last_run": ""}

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )

    def known_effective(self, state: dict[str, Any], law: str) -> str:
        """우리가 아는 그 법의 시행일 — 상태 파일과 그래프 중 나중 것."""
        recorded = str((state.get("laws") or {}).get(law, {}).get("effective_on") or "")
        from_graph = ""
        if self.graph is not None:
            for entity in self.graph.hubs(min_notes=1, kind=STATUTE_ENTITY, limit=5000):
                ref = parse_statute(entity.display)
                if ref is None or ref.law != law:
                    continue
                for row in self.graph.notes_for(entity.display, limit=50):
                    if str(row["kind"]) == STATUTE and str(row["effective_on"] or "") > from_graph:
                        from_graph = str(row["effective_on"])
        return max(recorded, from_graph)

    # ── 법령 ─────────────────────────────────────────────────────────────
    async def sync_laws(self, laws: list[str], fetch_body: bool = True) -> SyncResult:
        result = SyncResult()
        state = self.load_state()
        state.setdefault("laws", {})

        for law in laws:
            result.checked += 1
            try:
                docs = await self.client.search_law(law, display=3)
            except Exception as exc:  # noqa: BLE001 — 한 법이 실패해도 나머지는 돌아야 합니다
                result.errors.append(f"{law}: {exc}")
                continue
            doc = _best_match(docs, law)
            if doc is None:
                continue

            effective = _iso(doc.extra.get("시행일자") or doc.extra.get("시행일") or doc.date)
            if not effective:
                result.errors.append(f"{law}: 시행일을 읽지 못했습니다")
                continue
            previous = self.known_effective(state, law)
            if previous and effective <= previous:
                state["laws"][law] = {
                    "effective_on": previous,
                    "law_id": doc.doc_id,
                    "checked_at": _today(),
                }
                continue

            body = doc.body
            if fetch_body and doc.doc_id:
                try:
                    full = await self.client.get_law(law_id=doc.doc_id)
                    if full is not None and full.body:
                        body = full.body
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"{law} 본문: {exc}")

            update = LawUpdate(
                law=law,
                effective_on=effective,
                previous=previous,
                law_id=doc.doc_id,
                title=doc.title or law,
            )
            update.path = str(self._write_law(update, body))
            result.laws.append(update)
            state["laws"][law] = {
                "effective_on": effective,
                "law_id": doc.doc_id,
                "checked_at": _today(),
            }
            if self.graph is not None and previous:
                result.affected.extend(affected_notes(self.graph, law, effective))

        state["last_run"] = _today()
        self.save_state(state)
        return result

    def _write_law(self, update: LawUpdate, body: str) -> Path:
        folder = self.raw / "법령"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_safe(update.law)}-{update.effective_on}.md"
        note = WikiNote(
            path=path.name,
            title=f"{update.law} (시행 {update.effective_on})",
            kind=STATUTE,
            source=f"국가법령정보센터 법령ID {update.law_id}",
            effective_on=update.effective_on,
            body=body or "(본문을 받지 못했습니다 — 다시 받아 주세요)",
            extra={"main_law": update.law, "law_id": update.law_id, "fetched_on": _today()},
        )
        note.enrich(default_law=update.law)
        path.write_text(note.to_markdown(), encoding="utf-8")
        return path

    # ── 판례 ─────────────────────────────────────────────────────────────
    async def sync_precedents(
        self, queries: list[str], *, since: str = "", per_query: int = 20, fetch_body: bool = True
    ) -> SyncResult:
        result = SyncResult()
        state = self.load_state()
        state.setdefault("cases", {})
        cutoff = since or (date.today() - timedelta(days=7)).isoformat()

        for query in queries:
            result.checked += 1
            try:
                docs = await self.client.search_precedent(query, display=per_query)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{query}: {exc}")
                continue
            for doc in docs:
                decided = _iso(doc.extra.get("선고일자") or doc.date)
                case_no = str(doc.number or doc.extra.get("사건번호") or "").strip()
                if not case_no or (decided and decided < cutoff):
                    continue
                if case_no in state["cases"]:
                    continue

                body = doc.body
                if fetch_body and doc.doc_id:
                    try:
                        full = await self.client.get_precedent(doc.doc_id)
                        if full is not None and full.body:
                            body = full.body
                    except Exception as exc:  # noqa: BLE001
                        result.errors.append(f"{case_no} 본문: {exc}")

                update = CaseUpdate(
                    case_no=case_no,
                    court=doc.actor or "",
                    decided_on=decided,
                    title=doc.title or case_no,
                    doc_id=doc.doc_id,
                )
                update.path = str(self._write_case(update, body))
                result.cases.append(update)
                state["cases"][case_no] = {"decided_on": decided, "fetched_on": _today()}

        state["last_run"] = _today()
        self.save_state(state)
        return result

    def _write_case(self, update: CaseUpdate, body: str) -> Path:
        folder = self.raw / "판례"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_safe(update.case_no)}.md"
        note = WikiNote(
            path=path.name,
            title=f"{update.court} {update.case_no}".strip(),
            kind=CASE,
            source=f"국가법령정보센터 판례일련번호 {update.doc_id}",
            decided_on=update.decided_on,
            court=update.court,
            case_no=update.case_no,
            case_name=update.title,
            body=body or "(본문을 받지 못했습니다 — 다시 받아 주세요)",
            extra={"fetched_on": _today()},
        )
        note.enrich()
        path.write_text(note.to_markdown(), encoding="utf-8")
        return path

    # ── 작업지시서 ───────────────────────────────────────────────────────
    def write_worklist(self, result: SyncResult) -> Path | None:
        """코덱스가 읽고 고칠 목록. 없으면 파일을 만들지 않습니다."""
        rows: list[dict[str, Any]] = []
        for update in result.laws:
            rows.append(
                {
                    "job": "wiki",
                    "reason": "새 법령" if update.is_new else "법령 개정",
                    "raw": update.path,
                    "law": update.law,
                    "effective_on": update.effective_on,
                    "previous": update.previous,
                }
            )
        for update in result.cases:
            rows.append(
                {
                    "job": "wiki",
                    "reason": "새 판례",
                    "raw": update.path,
                    "case_no": update.case_no,
                    "decided_on": update.decided_on,
                }
            )
        for item in result.affected:
            rows.append({"job": "revise", "reason": "개정 이후 확인 필요", **item})
        if not rows:
            return None
        path = self.vault / WORKLIST_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        return path


def _best_match(docs: list[Any], law: str) -> Any:
    """검색 결과에서 그 법령을 고른다 — 이름이 정확히 같은 것 우선."""
    wanted = law.replace(" ", "")
    for doc in docs:
        if str(doc.title or "").replace(" ", "") == wanted:
            return doc
    return docs[0] if docs else None


_UNSAFE = re.compile(r'[\\/:*?"<>|]')


def _safe(name: str) -> str:
    return _UNSAFE.sub(" ", name or "").strip()[:100] or "무제"


# ── CLI ──────────────────────────────────────────────────────────────────
def _build_client(settings: Any) -> Any:
    from ..lawapi.client import LawApiClient

    return LawApiClient(
        oc=settings.law_oc,
        service_key=settings.data_go_kr_key,
        timeout_s=settings.law_api_timeout_s,
        cache_ttl_s=0,  # 동기화는 캐시를 보면 안 됩니다 — 바뀐 것을 찾는 일이니까요
    )


async def _run(args: Any) -> int:
    from ..config import get_settings

    settings = get_settings()
    vault = Path(args.vault)
    graph_path = Path(args.graph or settings.wiki_graph_path)
    graph = WikiGraph(graph_path) if graph_path.exists() else None
    client = _build_client(settings)
    sync = LawSync(client, vault, graph)

    try:
        laws = list(args.law or [])
        if not laws and graph is not None:
            laws = watched_laws(graph, top=args.top)
        if not laws:
            print("지켜볼 법령이 없습니다. --law 로 지정하거나 먼저 index 를 돌리세요.")
            return 1

        result = SyncResult()
        if args.command in {"laws", "daily"}:
            print(f"법령 {len(laws)}건 확인 중…")
            part = await sync.sync_laws(laws)
            result.laws.extend(part.laws)
            result.affected.extend(part.affected)
            result.errors.extend(part.errors)
            result.checked += part.checked
        if args.command in {"precedents", "daily"}:
            since = args.since or (date.today() - timedelta(days=args.days)).isoformat()
            queries = list(args.query or laws)
            print(f"{since} 이후 판례를 {len(queries)}개 검색어로 확인 중…")
            part = await sync.sync_precedents(queries, since=since, per_query=args.rows)
            result.cases.extend(part.cases)
            result.errors.extend(part.errors)
            result.checked += part.checked

        print(result.summary())
        for update in result.laws:
            mark = "신규" if update.is_new else f"{update.previous} → {update.effective_on}"
            print(f"  · {update.law} ({mark})  {update.path}")
        for update in result.cases:
            print(f"  · {update.court} {update.case_no} ({update.decided_on})")
        for item in result.affected[:20]:
            print(f"  ! 확인 필요: {item['title']} — {item['statute']} ({item['as_of'] or '날짜 없음'})")
        if len(result.affected) > 20:
            print(f"  … 그 밖에 {len(result.affected) - 20}건")
        for error in result.errors[:10]:
            print(f"  ✗ {error}")

        worklist = sync.write_worklist(result)
        if worklist is not None:
            print(f"\n작업지시서: {worklist}")
            print("이제 코덱스에게 wiki_prompt.md 규칙대로 처리하게 하세요:")
            print(f"  python -m kakao_legal_bot.app.wiki.build stub --raw {vault / 'raw'} "
                  f"--wiki {vault / 'wiki'}")
        return 0
    finally:
        if graph is not None:
            graph.close()
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m kakao_legal_bot.app.wiki.sync",
        description="국가법령 API 에서 개정 법령·최근 판례를 받아 raw/ 에 넣고 낡은 자료를 짚어 낸다",
    )
    parser.add_argument("command", choices=("laws", "precedents", "daily"))
    parser.add_argument("--vault", default="./vault")
    parser.add_argument("--graph", default="")
    parser.add_argument("--law", action="append", help="지켜볼 법령 (여러 번 쓸 수 있음)")
    parser.add_argument("--query", action="append", help="판례 검색어 (기본: 지켜보는 법령 이름)")
    parser.add_argument("--top", type=int, default=30, help="서가에서 많이 인용된 법령 상위 N개")
    parser.add_argument("--days", type=int, default=7, help="최근 며칠치 판례")
    parser.add_argument("--since", default="", help="이 날짜 이후 판례 (YYYY-MM-DD)")
    parser.add_argument("--rows", type=int, default=20, help="검색어당 판례 수")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
