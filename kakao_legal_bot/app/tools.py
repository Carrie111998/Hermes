"""The tools 모아 can call while answering.

Retrieval tools return text formatted for a prompt, not JSON — the model
reads them, and every wasted brace is a wasted token. The two action tools
(``request_document_draft`` / ``escalate_to_lawyer``) do not perform the
action inline; they record an intent on the turn so the pipeline can run
the slow part after the client already has an answer in the room.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .criminal import (
    crime_elements_for,
    crime_name_index,
    find_crime,
    find_crimes,
    school_measures_block,
)
from .intake import (
    CIVIL,
    CRIMINAL,
    CRIMINAL_INTAKE_FORM,
    INTAKE_FORM,
    criminal_report_template,
    is_criminal_doc,
    quote_text,
    report_template,
    tier_for,
)
from .knowledge import case_type_index, find_case_type, requisite_facts_for
from .lawapi.client import LawApiClient, LawApiError
from .lawapi.models import LawDoc
from .llm import ToolSpec
from .rag.store import RagStore

log = logging.getLogger(__name__)

MAX_TOOL_CHARS = 6000


@dataclass
class DraftRequest:
    kind: str
    title: str
    instructions: str


@dataclass
class Escalation:
    reason: str
    summary: str


@dataclass
class IntakeAction:
    """An intake step the pipeline must persist after the reply is sent."""

    kind: str  # start | report | case_type
    doc_kind: str = ""
    case_type: str = ""  # 민사 사건유형 또는 형사 죄명
    report: str = ""
    missing: str = ""
    track: str = ""  # civil | criminal (빈 값이면 기존 값을 바꾸지 않는다)


@dataclass
class TurnState:
    """Side effects and citations collected while answering one message."""

    citations: list[str] = field(default_factory=list)
    draft_request: DraftRequest | None = None
    escalation: Escalation | None = None
    intake_actions: list[IntakeAction] = field(default_factory=list)

    def cite(self, text: str) -> None:
        if text and text not in self.citations:
            self.citations.append(text)


def _clip(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…(이하 생략)"


def _format_docs(docs: list[LawDoc], state: TurnState, max_body: int = 800) -> str:
    if not docs:
        return "검색 결과가 없습니다. 다른 검색어로 다시 시도하거나, 조문을 인용하지 말고 답변하세요."
    blocks = []
    for doc in docs:
        state.cite(doc.citation)
        blocks.append(doc.to_prompt_block(max_body=max_body))
    return _clip("\n\n".join(blocks))


def build_tools(
    *,
    state: TurnState,
    rag: RagStore | None,
    law: LawApiClient | None,
    rag_top_k: int = 6,
    embed_query: Any = None,
    graph: Any = None,
    related_limit: int = 6,
) -> list[ToolSpec]:
    tools: list[ToolSpec] = []
    if graph is not None:
        tools.append(_related_tool(state, graph, rag, rag_top_k, related_limit))

    # ── local corpus ─────────────────────────────────────────────────────
    if rag is not None:
        collections = list(rag.collections()) if hasattr(rag, "collections") else []

        async def search_local_docs(arguments: dict[str, Any]) -> str:
            query = str(arguments.get("query") or "").strip()
            if not query:
                return "query 가 비어 있습니다."
            top_k = int(arguments.get("top_k") or rag_top_k)
            wanted = str(arguments.get("collection") or "").strip()
            extra = {"collection": wanted} if wanted and collections else {}
            if embed_query is not None:
                vector = await embed_query(query)
                hits = await asyncio.to_thread(
                    lambda: rag.search_with_embedding(query, vector, top_k, **extra)
                )
            else:
                hits = await asyncio.to_thread(lambda: rag.search(query, top_k, **extra))
            if not hits:
                return "로컬 자료에서 관련 내용을 찾지 못했습니다."
            blocks = []
            for hit in hits:
                state.cite(hit.citation)
                mark = f"[{hit.collection}] " if hit.collection else ""
                blocks.append(f"■ {mark}{hit.citation}\n  {hit.text.strip()}")
            return _clip("\n\n".join(blocks))

        properties: dict[str, Any] = {
            "query": {"type": "string", "description": "검색어. 핵심 법률 키워드 위주로."},
            "top_k": {"type": "integer", "description": "가져올 문단 수 (기본 6)"},
        }
        description = (
            "사무실 로컬 자료(주석서·법률서적·내부 서면 등)를 검색한다. "
            "일반적인 법리 설명이나 실무 관행이 필요할 때 가장 먼저 쓴다."
        )
        if len(collections) > 1:
            # Naming a collection turns a fan-out over every index into one
            # file read. Worth telling the model it can do that.
            properties["collection"] = {
                "type": "string",
                "description": (
                    "찾을 자료를 좁힌다. 비우면 전부 검색. "
                    f"있는 자료: {', '.join(collections)}"
                ),
            }
            description += f" 자료 구분: {', '.join(collections)}."

        tools.append(
            ToolSpec(
                name="search_local_docs",
                description=description,
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": ["query"],
                },
                handler=search_local_docs,
            )
        )

    if law is None:
        return tools

    # ── 국가법령정보 ──────────────────────────────────────────────────────
    async def search_law(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "query 가 비어 있습니다."
        try:
            docs = await law.search_law(query, display=int(arguments.get("limit") or 5))
        except LawApiError as exc:
            return f"법령 검색 실패: {exc}"
        return _format_docs(docs, state)

    async def get_law_text(arguments: dict[str, Any]) -> str:
        law_id = str(arguments.get("law_id") or "").strip()
        if not law_id:
            return "law_id 가 필요합니다. 먼저 search_law 로 법령일련번호를 찾으세요."
        try:
            doc = await law.get_law(law_id=law_id)
        except LawApiError as exc:
            return f"법령 본문 조회 실패: {exc}"
        if doc is None:
            return "해당 법령을 찾지 못했습니다."
        state.cite(doc.citation)
        return _clip(doc.to_prompt_block(max_body=4000))

    async def search_precedent(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        court = str(arguments.get("court") or "").strip()
        case_no = str(arguments.get("case_no") or "").strip()
        if not query and not case_no:
            return "query 또는 case_no 가 필요합니다."
        try:
            docs = await law.search_precedent(
                query, court=court, case_no=case_no, display=int(arguments.get("limit") or 5)
            )
        except LawApiError as exc:
            return f"판례 검색 실패: {exc}"
        if not docs:
            return "판례 검색 결과가 없습니다."
        lines = ["검색된 판례 목록입니다. 본문이 필요하면 get_precedent 로 판례일련번호를 조회하세요.\n"]
        for doc in docs:
            state.cite(doc.citation)
            lines.append(f"■ {doc.citation}\n  판례일련번호: {doc.doc_id}")
        return _clip("\n".join(lines))

    async def get_precedent(arguments: dict[str, Any]) -> str:
        prec_id = str(arguments.get("prec_id") or "").strip()
        if not prec_id:
            return "prec_id(판례일련번호)가 필요합니다."
        try:
            doc = await law.get_precedent(prec_id)
        except LawApiError as exc:
            return f"판례 본문 조회 실패: {exc}"
        if doc is None:
            return "해당 판례를 찾지 못했습니다."
        state.cite(doc.citation)
        return _clip(doc.to_prompt_block(max_body=5000))

    async def search_ordinance(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        try:
            docs = await law.search_ordinance(query, display=int(arguments.get("limit") or 5))
        except LawApiError as exc:
            return f"자치법규 검색 실패: {exc}"
        return _format_docs(docs, state)

    async def search_admin_rule(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        try:
            docs = await law.search_admin_rule(query, display=int(arguments.get("limit") or 5))
        except LawApiError as exc:
            return f"행정규칙 검색 실패: {exc}"
        return _format_docs(docs, state)

    async def search_forms(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        try:
            docs = await law.search_forms(query, display=int(arguments.get("limit") or 5))
        except LawApiError as exc:
            return f"별표·서식 검색 실패: {exc}"
        return _format_docs(docs, state, max_body=300)

    async def search_constitutional(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        limit = int(arguments.get("limit") or 5)
        errors = []
        try:
            docs = await law.search_constitutional_decision(query, display=limit)
            if docs:
                return _format_docs(docs, state)
        except LawApiError as exc:
            errors.append(str(exc))
        # law.go.kr's 헌재결정례 target is patchy; the data.go.kr 헌재
        # service covers the same ground with a different key.
        try:
            docs = await law.cc_precedents(rows=limit, **({"query": query} if query else {}))
            if docs:
                return _format_docs(docs, state)
        except LawApiError as exc:
            errors.append(str(exc))
        return "헌재 결정례를 찾지 못했습니다. " + (" / ".join(errors) if errors else "")

    tools.extend(
        [
            ToolSpec(
                name="search_law",
                description="국가법령정보센터에서 현행 법령을 검색한다. 조문 근거가 필요한 질문이면 반드시 먼저 호출한다.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "법령명 또는 키워드 (예: 주택임대차보호법)"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
                handler=search_law,
            ),
            ToolSpec(
                name="get_law_text",
                description="법령 본문을 조회한다. search_law 결과의 법령일련번호(id)를 넣는다.",
                input_schema={
                    "type": "object",
                    "properties": {"law_id": {"type": "string"}},
                    "required": ["law_id"],
                },
                handler=get_law_text,
            ),
            ToolSpec(
                name="search_precedent",
                description="대법원·각급 법원 판례 목록을 검색한다. 사건번호를 알면 case_no 로 바로 찾는다.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "사건명 또는 쟁점 키워드"},
                        "court": {"type": "string", "description": "법원명 (예: 대법원)"},
                        "case_no": {"type": "string", "description": "사건번호 (예: 2018다255648)"},
                        "limit": {"type": "integer"},
                    },
                },
                handler=search_precedent,
            ),
            ToolSpec(
                name="get_precedent",
                description="판례 본문(판시사항·판결요지·이유)을 조회한다. search_precedent 의 판례일련번호를 넣는다.",
                input_schema={
                    "type": "object",
                    "properties": {"prec_id": {"type": "string"}},
                    "required": ["prec_id"],
                },
                handler=get_precedent,
            ),
            ToolSpec(
                name="search_ordinance",
                description="지방자치단체 조례·규칙(자치법규)을 검색한다. 지역이 걸린 질문에 쓴다.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"],
                },
                handler=search_ordinance,
            ),
            ToolSpec(
                name="search_admin_rule",
                description="행정규칙(훈령·예규·고시)을 검색한다. 인허가·행정처분 기준을 확인할 때 쓴다.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"],
                },
                handler=search_admin_rule,
            ),
            ToolSpec(
                name="search_legal_forms",
                description="법령 별표·서식을 검색한다. 신청서·별지 서식이 필요한 질문에 쓴다.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"],
                },
                handler=search_forms,
            ),
            ToolSpec(
                name="search_constitutional_decision",
                description="헌법재판소 결정례를 검색한다. 위헌·헌법소원 쟁점에 쓴다.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"],
                },
                handler=search_constitutional,
            ),
        ]
    )
    return tools


def _note_line(row: Any, weight_note: str = "") -> str:
    """그래프 조회 결과 한 줄 — 날짜와 연혁 여부가 보여야 합니다."""
    bits = [str(row["kind"] or "")]
    if row["as_of"]:
        bits.append(str(row["as_of"]))
    if weight_note:
        bits.append(weight_note)
    head = f"■ {row['title']} ({' · '.join(bit for bit in bits if bit)})"
    if row["superseded_by"]:
        head += "  ⚠ 개정 전 자료 — 현행 확인 필요"
    summary = str(row["summary"] or "").strip()
    return f"{head}\n  {summary[:300]}" if summary else head


def _related_tool(
    state: TurnState, graph: Any, rag: Any, rag_top_k: int, related_limit: int
) -> ToolSpec:
    """같은 조문·같은 판례를 말하는 문서들을 그래프로 끌어온다.

    키워드 검색과 임베딩은 '비슷한 문장'을 찾습니다. 정작 필요한 질문 —
    "민법 제618조를 다루는 자료 전부" 나 "이 판례를 인용한 문서" — 는 문장이
    닮았는지와 무관하고, 연결로만 답할 수 있습니다.
    """

    async def search_related_docs(arguments: dict[str, Any]) -> str:
        anchor = str(arguments.get("anchor") or "").strip()
        query = str(arguments.get("query") or "").strip()
        limit = int(arguments.get("limit") or related_limit)
        blocks: list[str] = []

        if anchor:
            rows = await asyncio.to_thread(graph.notes_for, anchor, limit)
            if rows:
                entity = await asyncio.to_thread(graph.entity, anchor)
                key = entity.display if entity is not None else anchor
                state.cite(key)
                blocks.append(f"[{key} 를 다루는 자료 {len(rows)}건 — 최신 순]")
                blocks.extend(
                    _note_line(row, f"{int(row['weight'])}회" if row["weight"] > 1 else "")
                    for row in rows
                )
            else:
                blocks.append(f"'{anchor}' 로 연결된 자료가 없습니다. query 로 다시 찾아보세요.")

        if query and rag is not None:
            hits = await asyncio.to_thread(rag.search, query, rag_top_k)
            seeds = await asyncio.to_thread(graph.resolve, [hit.source for hit in hits])
            if seeds:
                related = await asyncio.to_thread(graph.related, seeds, limit)
                if related:
                    blocks.append(f"\n['{query}' 와 같은 조문·판례를 다루는 자료]")
                    for item in related:
                        shared = ", ".join(item.shared[:4])
                        mark = "  ⚠ 개정 전 자료" if item.stale else ""
                        date = f" · {item.as_of}" if item.as_of else ""
                        blocks.append(
                            f"■ {item.title} ({item.kind}{date}){mark}\n  공통: {shared}"
                        )
        if not blocks:
            return (
                "그래프에서 연결된 자료를 찾지 못했습니다. search_local_docs 로 "
                "본문을 직접 검색해 보세요."
            )
        blocks.append(
            "\n본문이 필요하면 search_local_docs 로 그 제목을 검색하세요. "
            "'개정 전 자료' 표시가 붙은 것은 연혁으로만 쓰고 현행 규정을 함께 확인하세요."
        )
        return _clip("\n".join(blocks), limit=8000)

    return ToolSpec(
        name="search_related_docs",
        description=(
            "같은 조문·판례·키워드를 다루는 자료들을 연결로 찾는다. "
            "'민법 제618조를 다루는 자료 전부' 처럼 문장이 아니라 근거로 묶어야 할 때, "
            "또는 어떤 쟁점의 자료를 빠짐없이 훑어야 할 때 쓴다. "
            "조문·판례는 anchor 에, 자연어 질문은 query 에 넣는다."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "anchor": {
                    "type": "string",
                    "description": "조문·판례번호·키워드 (예: 민법 제618조, 2017다12345, 대항력)",
                },
                "query": {"type": "string", "description": "자연어 질문. anchor 를 모를 때."},
                "limit": {"type": "integer"},
            },
        },
        handler=search_related_docs,
    )


async def _school_measures_result() -> str:
    block = school_measures_block()
    if not block:
        return (
            "학교폭력 조치 데이터가 아직 없습니다. 일반적인 절차만 안내하고 "
            "구체적인 조치 번호·내용은 변호사에게 넘기세요."
        )
    return _clip(block, limit=8000)


def build_intake_tools(state: TurnState, lawyer_name: str) -> list[ToolSpec]:
    """The document-intake flow: form → requisite facts → report → quote.

    These tools do the *thinking* work inline (they are pure lookups over
    bundled knowledge, no network) but leave the *writing* to the pipeline,
    which persists the intake after the client already has a reply.
    """

    async def start_document_intake(arguments: dict[str, Any]) -> str:
        doc_kind = str(arguments.get("doc_kind") or "법률문서").strip()
        raw_case = str(arguments.get("case_type") or "").strip()
        criminal = is_criminal_doc(doc_kind) or (
            find_case_type(raw_case) is None and find_crime(raw_case) is not None
        )
        case = find_crime(raw_case) if criminal else find_case_type(raw_case)
        state.intake_actions.append(
            IntakeAction(
                kind="start",
                doc_kind=doc_kind,
                case_type=case.key if case else raw_case,
                track=CRIMINAL if criminal else CIVIL,
            )
        )
        tier = tier_for(doc_kind)
        parts = [
            "아래 [보낼 폼] 을 그대로 상담자에게 보내고 기다리세요. "
            "질문을 덧붙이지 마세요.",
            f"(내부 참고 — 이 문서는 '{tier.label}' 등급, "
            f"{tier.price_krw:,}원 / {tier.lead_time}. 지금 말하지 말고 "
            "상담보고서 확인 단계에서 안내합니다.)",
            "\n[보낼 폼]\n" + (CRIMINAL_INTAKE_FORM if criminal else INTAKE_FORM),
        ]
        if criminal:
            if case is not None:
                parts.append(
                    "\n[내부 참고 — 폼 답변이 오면 이 구성요건과 하나씩 대조하세요]\n"
                    + crime_elements_for(case.key)
                )
                parts.append("\n" + criminal_report_template(doc_kind, case.label))
            else:
                parts.append(
                    "\n[내부 참고] 죄명을 아직 특정하지 못했습니다. **죄명 확정이 먼저입니다.** "
                    "폼 답변이 오면 get_crime_elements 를 호출하세요. 데이터에 있는 죄명:\n"
                    + crime_name_index()
                )
        elif case is not None:
            parts.append(
                f"\n[내부 참고 — 폼 답변이 오면 이 요건사실과 하나씩 대조하세요]\n"
                f"{requisite_facts_for(case.key)}"
            )
            parts.append("\n" + report_template(doc_kind, case.label))
        else:
            parts.append(
                "\n[내부 참고] 사건유형을 아직 특정하지 못했습니다. 폼 답변이 오면 "
                f"get_requisite_facts 를 호출하세요. 가능한 유형:\n{case_type_index()}"
            )
        return _clip("\n".join(parts), limit=14000)

    async def get_requisite_facts(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("case_type") or "").strip()
        case = find_case_type(query)
        if case is None:
            return (
                f"'{query}' 에 맞는 사건유형을 찾지 못했습니다. 아래 중에서 고르세요.\n"
                f"{case_type_index()}"
            )
        state.intake_actions.append(
            IntakeAction(kind="case_type", case_type=case.key, track=CIVIL)
        )
        return _clip(requisite_facts_for(case.key), limit=12000)

    async def get_crime_elements(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("crime") or "").strip()
        crime = find_crime(query)
        if crime is None:
            candidates = find_crimes(query)
            if candidates:
                names = ", ".join(c.name for c in candidates)
                return (
                    f"'{query}' 로는 죄명이 하나로 좁혀지지 않습니다. 후보: {names}\n"
                    "상담자에게 사실관계를 한 가지만 더 물어 죄명을 확정한 뒤 다시 호출하세요."
                )
            return (
                f"'{query}' 에 맞는 죄명을 데이터에서 찾지 못했습니다. **조문을 기억으로 "
                "지어내지 마세요.** 아래 중에 해당하는 것이 있으면 그 이름으로 다시 "
                "호출하고, 없으면 escalate_to_lawyer 로 변호사에게 넘기세요.\n"
                f"{crime_name_index()}"
            )
        state.intake_actions.append(
            IntakeAction(kind="case_type", case_type=crime.key, track=CRIMINAL)
        )
        return _clip(crime_elements_for(crime.key), limit=12000)

    async def submit_consultation_report(arguments: dict[str, Any]) -> str:
        report = str(arguments.get("report") or "").strip()
        doc_kind = str(arguments.get("doc_kind") or "법률문서").strip()
        missing = str(arguments.get("still_missing") or "").strip()
        if not report:
            return "report 가 비어 있습니다. 상담보고서 본문을 넣어 다시 호출하세요."
        state.intake_actions.append(
            IntakeAction(kind="report", doc_kind=doc_kind, report=report, missing=missing)
        )
        return (
            "상담보고서를 저장했습니다. 이제 아래 두 가지를 상담자에게 "
            "**그대로 이어서** 보내고 확인을 받으세요.\n\n"
            "① 위에서 작성하신 상담보고서 전문\n"
            "② 아래 안내문\n\n"
            f"{quote_text(doc_kind, lawyer_name)}\n\n"
            "상담자가 '진행하겠다'고 하면 그때 request_document_draft 를 호출하고, "
            "instructions 에 상담보고서 전문을 넣으세요. 정정 요청이 오면 고쳐서 "
            "다시 확인받으세요."
        )

    return [
        ToolSpec(
            name="start_document_intake",
            description=(
                "상담자가 문서 작성을 요청했을 때 가장 먼저 호출한다. 정보입력폼과 "
                "해당 사건유형의 요건사실을 돌려준다. 초안을 바로 만들지 말고 이것부터."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "doc_kind": {
                        "type": "string",
                        "description": "문서 종류 (내용증명 / 소장 / 답변서 / 준비서면 등)",
                    },
                    "case_type": {
                        "type": "string",
                        "description": "사건유형. 모르면 비워둔다 (예: 대여금, 임대차보증금반환)",
                    },
                },
                "required": ["doc_kind"],
            },
            handler=start_document_intake,
        ),
        ToolSpec(
            name="get_requisite_facts",
            description=(
                "사건유형의 청구원인 요건사실·자주 나오는 항변·관련 민법 요건사실을 가져온다. "
                "상담자에게 무엇을 더 물어야 하는지 판단할 때 쓴다."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_type": {
                        "type": "string",
                        "description": "사건유형 또는 상담자의 표현 (예: 전세금을 못 받았어요)",
                    }
                },
                "required": ["case_type"],
            },
            handler=get_requisite_facts,
        ),
        ToolSpec(
            name="get_crime_elements",
            description=(
                "형사사건(고소장·고발장 포함)에서 **죄명을 확정한 뒤** 그 죄의 "
                "범죄구성요건·미수/예비음모/상습범/과실범 처벌규정·친고죄 여부·"
                "공소시효·질문항목을 가져온다. 형법 조문은 절대 기억으로 말하지 말고 "
                "반드시 이 도구를 거친다."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "crime": {
                        "type": "string",
                        "description": "죄명 또는 상담자의 표현 (예: 절도, 물건을 훔쳐갔어요)",
                    }
                },
                "required": ["crime"],
            },
            handler=get_crime_elements,
        ),
        ToolSpec(
            name="get_school_violence_measures",
            description=(
                "학교폭력 상담에서 학폭위(심의위원회) 조치를 가져온다 — 가해학생 조치 "
                "제1~9호, 피해학생 보호조치, 학생부 기재, 불복 절차. 이것은 형사처벌이 "
                "아니라 행정조치다. 행위 자체가 폭행·상해·모욕 등 범죄에 해당하는지는 "
                "get_crime_elements 로 따로 확인하고, 학폭 상담에서는 두 갈래를 함께 안내한다."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=lambda _arguments: _school_measures_result(),
        ),
        ToolSpec(
            name="submit_consultation_report",
            description=(
                "사실관계 수집이 끝나면 상담보고서를 제출한다. 저장 후 상담자에게 보여줄 "
                "비용·소요기간 안내문을 돌려준다."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "report": {"type": "string", "description": "상담보고서 전문"},
                    "doc_kind": {"type": "string", "description": "문서 종류"},
                    "still_missing": {
                        "type": "string",
                        "description": "아직 확인하지 못한 사항 (없으면 비움)",
                    },
                },
                "required": ["report", "doc_kind"],
            },
            handler=submit_consultation_report,
        ),
    ]


def build_action_tools(state: TurnState) -> list[ToolSpec]:
    """Draft + escalation. Recorded now, executed after the reply is sent."""

    async def request_document_draft(arguments: dict[str, Any]) -> str:
        state.draft_request = DraftRequest(
            kind=str(arguments.get("kind") or "general"),
            title=str(arguments.get("title") or "법률문서 초안"),
            instructions=str(arguments.get("instructions") or ""),
        )
        return (
            "초안 작성 요청이 접수되었습니다. 상담자에게 '변호사 검토 후 이메일로 보내드린다'고 "
            "안내하고, 이메일 주소를 아직 받지 않았다면 이메일 주소를 물어보세요."
        )

    async def escalate_to_lawyer(arguments: dict[str, Any]) -> str:
        state.escalation = Escalation(
            reason=str(arguments.get("reason") or ""),
            summary=str(arguments.get("summary") or ""),
        )
        return (
            "변호사에게 전달되었습니다. 상담자에게 담당 변호사가 직접 확인 후 답변드린다고 "
            "안내하되, 지금 답할 수 있는 일반적인 설명은 함께 해주세요."
        )

    return [
        ToolSpec(
            name="request_document_draft",
            description=(
                "내용증명·합의서·답변서·고소장 등 법률문서 초안 작성을 요청한다. "
                "초안은 담당 변호사 검토 후 상담자 이메일로 발송된다."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "문서 종류 (내용증명 / 합의서 / 답변서 / 고소장 / 내용정리 등)",
                    },
                    "title": {"type": "string", "description": "문서 제목"},
                    "instructions": {
                        "type": "string",
                        "description": "초안에 반드시 들어가야 할 사실관계·청구내용·기한 등을 자세히.",
                    },
                },
                "required": ["kind", "instructions"],
            },
            handler=request_document_draft,
        ),
        ToolSpec(
            name="escalate_to_lawyer",
            description=(
                "담당 변호사에게 이 상담을 즉시 전달한다. 금액·기한이 걸린 판단, 형사 사건, "
                "소송 전략, 상담자가 화가 나 있거나 급한 경우에 호출한다."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "왜 변호사가 필요한지 한 줄"},
                    "summary": {"type": "string", "description": "변호사가 읽을 사실관계 요약"},
                },
                "required": ["reason"],
            },
            handler=escalate_to_lawyer,
        ),
    ]
