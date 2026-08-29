"""사건파일 — 완결된 상담 하나를 raw 문서 한 장으로.

문서 발송이 끝난 사건은 네 가지가 모두 존재하는 유일한 순간입니다:
상담보고서(관련 법령·판례), 대화 전체(질문·답변), LLM 이 낸 초안 원본,
그리고 변호사가 최종 검수한 문서. 이 넷을 한 파일로 묶어

    DATA_DIR/casefiles/홍길동-2026-08-29-사건12.md

로 남깁니다. 이것이 상담사례 지식의 **raw 층**입니다 — 서버는 여기까지만
기계적으로 하고, 가명화 요약(WIKI 층)은 변호사 PC 의 코덱스가
``_wiki-jobs.jsonl`` 을 보고 씁니다.

동시에 **가명화한 사본**을 RAG 'cases' 컬렉션에 색인해, 다음 상담부터
모아가 "우리 사무실이 실제로 다룬 비슷한 사건"을 검색해 참고합니다.
원본(실명)은 디스크의 사건파일에만 있고, 검색 색인에는 실명·연락처를
지운 사본만 들어갑니다 — 다른 상담자의 답변에 실명이 새지 않게요.

    python -m kakao_legal_bot.app.casefile build 12     # 초안 12번 사건
    python -m kakao_legal_bot.app.casefile rebuild      # 발송 완료 전부
    python -m kakao_legal_bot.app.casefile list
    python -m kakao_legal_bot.app.casefile prompt       # 코덱스용 요약 규칙
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from .archive import safe_filename
from .config import Settings, get_settings
from .db import Database, Draft, Message

_INTAKE_TRACKS = {"criminal": "형사", "civil": "민사"}

# ── 가명화 (검색 색인용 사본에만) ────────────────────────────────────────
_PHONE = re.compile(r"\b0\d{1,2}[-. ]?\d{3,4}[-. ]?\d{4}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_RRN = re.compile(r"\b\d{6}[- ]?[1-4]\d{6}\b")  # 주민등록번호


def redact(text: str, names: list[str] = ()) -> str:  # noqa: B006 — 읽기 전용
    """실명·연락처를 지운 사본. 원본은 손대지 않습니다.

    이름은 우리가 **아는 것만** 확실히 지울 수 있습니다 — 상담자의 카카오
    표시이름과 상담 별칭. 본문 속에 등장한 제3자 이름까지 기계로 다 잡을
    수는 없으므로, 위키 요약 단계(코덱스)의 가명화 규칙이 한 번 더 겁니다.
    """
    result = text
    for index, name in enumerate(
        sorted({n.strip() for n in names if n and len(n.strip()) >= 2}, key=len, reverse=True)
    ):
        result = result.replace(name, f"상담자{chr(ord('A') + (index % 26))}")
    result = _RRN.sub("(주민번호 삭제)", result)
    result = _PHONE.sub("(전화번호 삭제)", result)
    result = _EMAIL.sub("(이메일 삭제)", result)
    return result


# ── 사건파일 만들기 ──────────────────────────────────────────────────────
def _transcript(db: Database, room_id: str, bot_name: str) -> list[str]:
    turns: list[Message] = db.room_archive(room_id)
    if not turns:  # 보관 기능이 켜지기 전의 방이면 문맥이라도
        turns = db.recent_messages(room_id, limit=200)
    speaker_for = {"bot": bot_name, "lawyer": "변호사", "system": "(안내)"}
    lines: list[str] = []
    for turn in turns:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(turn.created_at))
        speaker = turn.sender or speaker_for.get(turn.role, "상담자")
        body = turn.text.strip().replace("\n", "\n  ")
        lines.append(f"- **{speaker}** ({stamp}):\n  {body}")
    return lines


def _client_names(db: Database, room_id: str) -> list[str]:
    names = {
        turn.sender
        for turn in db.room_archive(room_id)
        if turn.role == "user" and turn.sender
    }
    row = db._query_one(  # noqa: SLF001 — 같은 패키지의 읽기 전용 조회
        "SELECT client_alias FROM consultations WHERE room_id = ? ORDER BY id DESC LIMIT 1",
        (room_id,),
    )
    if row is not None and str(row["client_alias"]).strip():
        names.add(str(row["client_alias"]).strip())
    return sorted(names)


def casefile_markdown(db: Database, settings: Settings, draft: Draft) -> tuple[str, str]:
    """``(라벨, 마크다운)``. 네 부분을 순서대로 한 장에."""
    room = db.get_room(draft.room_id)
    label = ""
    if room is not None and "label" in room.keys():
        label = str(room["label"])
    label = label or draft.room_id

    consultation = db._query_one(  # noqa: SLF001
        "SELECT * FROM consultations WHERE room_id = ? ORDER BY id DESC LIMIT 1",
        (draft.room_id,),
    )
    # 이 초안과 연결된 인테이크가 먼저, 없으면 이 방의 마지막 보고서.
    intake = db._query_one(  # noqa: SLF001
        "SELECT * FROM intakes WHERE draft_id = ? ORDER BY id DESC LIMIT 1", (draft.id,)
    ) or db._query_one(  # noqa: SLF001
        "SELECT * FROM intakes WHERE room_id = ? AND report != '' ORDER BY id DESC LIMIT 1",
        (draft.room_id,),
    )

    case_type = str(intake["case_type"]) if intake is not None else ""
    track = _INTAKE_TRACKS.get(str(intake["track"]), "") if intake is not None else ""
    today = time.strftime("%Y-%m-%d")
    keywords = [value for value in (case_type, draft.kind, track) if value]

    front = [
        "---",
        f"title: {label} · {draft.kind or '문서'}",
        "kind: 상담사례",
        "collection: 상담사례",
        f"written_on: {today}",
    ]
    if keywords:
        front.append("keywords: [" + ", ".join(keywords) + "]")
    front.append("---")

    head = [
        "",
        f"# {label} — {draft.kind or '문서'} · {draft.title or '(제목 없음)'}",
        "",
        f"- 방: {draft.room_id}",
    ]
    if consultation is not None:
        head.append(f"- 접수번호: {consultation['id']}")
        if str(consultation["client_alias"]):
            head.append(f"- 상담자: {consultation['client_alias']}")
    if case_type or track:
        head.append(f"- 사건유형: {case_type or '미상'}{f' ({track})' if track else ''}")
    head.append(f"- 사건파일 작성: {today} · 초안 #{draft.id}")

    parts: list[str] = ["\n".join(front), "\n".join(head)]

    # 1. 상담보고서 — 관련 법령·판례는 보고서 본문 안에 있습니다.
    parts.append("\n## 1. 상담보고서 (관련 법령·판례)\n")
    if intake is not None and str(intake["report"]).strip():
        if str(intake["missing"]).strip():
            parts.append(f"> 미확인 사항: {intake['missing']}\n")
        parts.append(str(intake["report"]).strip())
    else:
        parts.append("(상담보고서 없음 — 문답 완료 전에 문서로 직행한 사건입니다.)")

    # 2. 대화 전체.
    parts.append("\n## 2. 대화 전체 (질문·답변)\n")
    transcript = _transcript(db, draft.room_id, settings.bot_name)
    parts.append("\n".join(transcript) if transcript else "(보관된 대화가 없습니다.)")

    # 3. LLM 초안 원본. 변호사가 무엇을 고쳤는지가 4와의 차이에서 보입니다.
    parts.append("\n## 3. LLM 초안 (수정 전 원본)\n")
    parts.append(draft.llm_body.strip() or "(초안 원본이 기록되지 않은 옛 사건입니다.)")

    # 4. 최종본.
    parts.append("\n## 4. 최종 문서 (변호사 검수본)\n")
    parts.append(draft.body.strip() or "(본문 없음)")
    if draft.lawyer_note:
        parts.append(f"\n[변호사 메모] {draft.lawyer_note}")

    return label, "\n".join(parts).rstrip() + "\n"


def build_casefile(db: Database, settings: Settings, draft_id: int) -> Path | None:
    draft = db.get_draft(draft_id)
    if draft is None:
        return None
    label, markdown = casefile_markdown(db, settings, draft)
    settings.casefile_dir.mkdir(parents=True, exist_ok=True)
    path = settings.casefile_dir / f"{safe_filename(label)}-사건{draft.id}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


# ── 상담사례 검색 색인 (가명화 사본) ─────────────────────────────────────
def index_casefile(db: Database, settings: Settings, draft: Draft, path: Path) -> int:
    """가명화한 사본을 RAG 'cases' 컬렉션에 넣는다.

    다음 상담에서 search_local_docs 가 books·commentary 와 함께 이 컬렉션도
    검색합니다 — "우리가 실제로 다룬 비슷한 사건"이 답변의 재료가 됩니다.
    """
    from .rag.store import RagStore, chunk_text  # 지역 임포트: CLI 기동 비용

    text = path.read_text(encoding="utf-8")
    names = _client_names(db, draft.room_id)
    redacted = redact(text, names)
    chunks = chunk_text(redacted, settings.rag_chunk_chars, settings.rag_chunk_overlap)
    store = RagStore(settings.rag_path("cases"))
    try:
        import hashlib

        return store.upsert_document(
            f"casefiles/{path.name}",
            path.stem,
            chunks,
            meta={"kind": "상담사례"},
            sha=hashlib.sha256(redacted.encode()).hexdigest()[:16],
        )
    finally:
        store.close()


# ── 코덱스 작업열 — 가명화 요약(WIKI 층)은 PC 몫 ─────────────────────────
CASE_WIKI_PROMPT = """[상담사례 → 위키 노트 규칙]

사건파일(raw)을 읽고 위키 노트 한 장을 씁니다. 반드시:

1. **가명화가 먼저입니다.** 실명은 전부 "상담자A"·"상대방B" 식으로,
   전화번호·이메일·주소·계좌·사업자번호는 삭제. 사건을 특정할 수 있는
   고유 지명·상호도 일반화합니다 ("서울 ○○구 상가"). 이 규칙을 어긴
   노트는 없느니만 못합니다 — 변호사 비밀유지의무가 걸린 자료입니다.
2. frontmatter: kind: 상담사례 / collection: 상담사례 / written_on: 사건파일
   작성일 / keywords 에 분야 키워드([[사해행위취소]], [[채권양도]],
   [[제척기간]] 같은 조합 — 사건유형 하나가 아니라 쟁점 단위로).
3. 본문 구성: ## 사건 개요(6하원칙, 가명) → ## 쟁점 → ## 적용 법령·판례
   (전부 [[민법 제406조]]·[[2020다12345]] 형식) → ## 진행 결과(작성한
   문서와 요지) → ## 재사용 포인트(다음 비슷한 상담에서 무엇을 물어야
   했고, 초안에서 변호사가 무엇을 고쳤는지).
4. 노트를 쓴 뒤 분야 허브를 갱신합니다: python -m kakao_legal_bot.app.wiki.build
   index → hubs. 같은 분야 사례가 3건 이상 모이면 "분야 총설" 노트를
   만들어 사례들을 [[링크]]로 묶고 공통 법리를 한 단락으로 적습니다.
5. 조문·판례 표기는 위키 전체 규칙(wiki_prompt.md)과 동일 — 근거 없는
   인용 금지, 사건파일에 없는 법리를 지어내지 않습니다.
"""


def append_wiki_job(settings: Settings, path: Path, draft: Draft, case_type: str) -> None:
    job = {
        "kind": "consult_case",
        "raw": str(path),
        "wiki": str(settings.wiki_vault / "wiki" / "상담사례" / path.name),
        "case_type": case_type,
        "doc_kind": draft.kind,
        "rule": "python -m kakao_legal_bot.app.casefile prompt",
    }
    jobs = settings.casefile_dir / "_wiki-jobs.jsonl"
    with jobs.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(job, ensure_ascii=False) + "\n")


# ── 한 번에: 만들고, 색인하고, 작업열에 올리기 ──────────────────────────
def archive_case(db: Database, settings: Settings, draft_id: int) -> Path | None:
    """발송 완료 훅. 실패해도 발송 자체를 깨뜨리면 안 되므로 조용히 None."""
    path = build_casefile(db, settings, draft_id)
    if path is None:
        return None
    draft = db.get_draft(draft_id)
    assert draft is not None
    intake = db._query_one(  # noqa: SLF001
        "SELECT case_type FROM intakes WHERE room_id = ? AND case_type != '' "
        "ORDER BY id DESC LIMIT 1",
        (draft.room_id,),
    )
    index_casefile(db, settings, draft, path)
    append_wiki_job(settings, path, draft, str(intake["case_type"]) if intake else "")
    return path


# ── CLI ──────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="사건파일 — 완결 상담의 4종 합본과 색인")
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("build", help="초안 번호로 사건파일 생성 + 색인")
    build.add_argument("draft_id", type=int)
    sub.add_parser("rebuild", help="발송 완료(sent)된 초안 전부 다시 생성 + 색인")
    sub.add_parser("list", help="쌓인 사건파일 목록")
    sub.add_parser("prompt", help="코덱스용 상담사례 위키 요약 규칙 출력")
    args = parser.parse_args(argv)

    if args.cmd == "prompt":
        print(CASE_WIKI_PROMPT)
        return 0

    settings = get_settings()
    db = Database(settings.db_path)
    try:
        if args.cmd == "build":
            path = archive_case(db, settings, args.draft_id)
            if path is None:
                print(f"#{args.draft_id} 초안을 찾을 수 없습니다.")
                return 1
            print(path)
            return 0
        if args.cmd == "rebuild":
            for draft in db.list_drafts("sent", 1000):
                path = archive_case(db, settings, draft.id)
                if path is not None:
                    print(path)
            return 0
        if args.cmd == "list":
            folder = settings.casefile_dir
            files = sorted(folder.glob("*.md")) if folder.is_dir() else []
            if not files:
                print("사건파일이 없습니다. (문서 발송이 끝나면 자동으로 쌓입니다)")
                return 0
            for file in files:
                print(file.name)
            return 0
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
