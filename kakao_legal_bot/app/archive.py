"""상담 기록 보관 — 방 라벨 짓기와 .md 내보내기.

모든 대화는 ``archive`` 표에 영구 저장되고(트림·90일 정리의 영향 없음),
방마다 첫 질문 순간에 보관용 이름이 붙습니다:

* 상담자 이름을 알면        →  ``홍길동-2026-08-29``
* 모르면 첫 질문의 키워드로 →  ``대여금-2026-08-29`` / ``절도-2026-08-29``
* 그마저 없으면 질문 앞부분 →  ``월세 보증금을 못 받고-2026-08-29``

내보내기는 방 하나를 파일 하나로 만듭니다 — 머리말(접수번호·상담자·기간),
완성된 상담보고서 전문, 그리고 전체 대화. 변호사 PC로 가져가 코덱스가
읽거나 사건 폴더에 넣기 좋은 평문 마크다운입니다.

    python -m kakao_legal_bot.app.archive list
    python -m kakao_legal_bot.app.archive export            # 전부
    python -m kakao_legal_bot.app.archive export --room <id>
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from .config import Settings, get_settings
from .db import Database

_INTAKE_TRACKS = {"criminal": "형사", "civil": "민사"}


# ── 방 라벨 ──────────────────────────────────────────────────────────────
def room_label_base(sender_name: str, question: str, when: float | None = None) -> str:
    """"이름-날짜", 이름이 없으면 "핵심키워드-날짜". 중복 처리는 db 몫."""
    date = time.strftime("%Y-%m-%d", time.localtime(when) if when else time.localtime())
    name = " ".join((sender_name or "").split())
    if name:
        return f"{name}-{date}"
    return f"{_question_keyword(question)}-{date}"


def _question_keyword(question: str) -> str:
    """첫 질문에서 사건을 알아볼 만한 한 단어 — 사건유형, 죄명, 없으면 요약."""
    text = " ".join((question or "").split())
    if text:
        # 지역 임포트: criminal 은 죄명 233건을 읽어들이므로 필요할 때만.
        from .criminal import find_crime
        from .knowledge import find_case_type

        case = find_case_type(text)
        if case is not None:
            return case.key
        crime = find_crime(text)
        if crime is not None:
            return crime.name
    return text[:20].strip() or "상담"


def safe_filename(label: str) -> str:
    """라벨을 파일 이름으로 — 경로 문자만 걸러내고 나머지는 그대로."""
    cleaned = "".join(ch for ch in label if ch not in '/\\:*?"<>|\0')
    cleaned = " ".join(cleaned.split()).strip(". ")
    return cleaned or "상담"


# ── 내보내기 ─────────────────────────────────────────────────────────────
def render_room_markdown(db: Database, room_id: str, bot_name: str = "모아") -> str:
    """방 하나의 상담 기록 전체를 마크다운 한 장으로."""
    room = db.get_room(room_id)
    label = str(room["label"]) if room is not None and room["label"] else ""
    room_name = str(room["room_name"]) if room is not None else ""

    turns = db.room_archive(room_id)
    consultation = db._query_one(  # noqa: SLF001 — 같은 패키지의 읽기 전용 조회
        "SELECT * FROM consultations WHERE room_id = ? ORDER BY id DESC LIMIT 1", (room_id,)
    )
    intakes = db._query(  # noqa: SLF001
        "SELECT * FROM intakes WHERE room_id = ? AND report != '' ORDER BY id", (room_id,)
    )

    lines: list[str] = [f"# {label or room_name or room_id}", ""]
    if consultation is not None:
        lines.append(f"- 접수번호: {consultation['id']}")
        if str(consultation["client_alias"]):
            lines.append(f"- 상담자: {consultation['client_alias']}")
    if turns:
        first = time.strftime("%Y-%m-%d %H:%M", time.localtime(turns[0].created_at))
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(turns[-1].created_at))
        lines.append(f"- 기간: {first} ~ {last} · {len(turns)}건")
    lines.append(f"- 방 id: {room_id}")

    for intake in intakes:
        track = _INTAKE_TRACKS.get(str(intake["track"]), str(intake["track"]))
        head = f"## 상담보고서 — {intake['doc_kind'] or '문서 미정'}"
        if str(intake["case_type"]):
            head += f" · {intake['case_type']} ({track})"
        lines += ["", head, ""]
        if str(intake["missing"]):
            lines.append(f"> 미확인 사항: {intake['missing']}")
            lines.append("")
        lines.append(str(intake["report"]).strip())

    lines += ["", "## 대화 전체", ""]
    if not turns:
        lines.append("(보관된 대화가 없습니다 — 보관은 이 기능이 켜진 뒤의 대화부터 남습니다.)")
    speaker_for = {"bot": bot_name, "lawyer": "변호사", "system": "(안내)"}
    for turn in turns:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(turn.created_at))
        speaker = turn.sender or speaker_for.get(turn.role, "상담자")
        body = turn.text.strip().replace("\n", "\n  ")
        lines.append(f"- **{speaker}** ({stamp}):\n  {body}")

    return "\n".join(lines).rstrip() + "\n"


def export_room(db: Database, room_id: str, out_dir: Path, bot_name: str = "모아") -> Path:
    room = db.get_room(room_id)
    label = str(room["label"]) if room is not None and room["label"] else room_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{safe_filename(label)}.md"
    path.write_text(render_room_markdown(db, room_id, bot_name), encoding="utf-8")
    return path


def export_all(db: Database, out_dir: Path, bot_name: str = "모아") -> list[Path]:
    """보관 기록이 있는 방을 전부 내보낸다 (변호사 알림 방 등 빈 방은 제외)."""
    rows = db._query(  # noqa: SLF001
        "SELECT DISTINCT room_id FROM archive ORDER BY room_id"
    )
    return [export_room(db, str(row["room_id"]), out_dir, bot_name) for row in rows]


# ── CLI ──────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="상담 기록 보관소 — 목록·내보내기")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="보관 기록이 있는 방과 라벨")
    export = sub.add_parser("export", help="방을 .md 로 내보내기 (기본: 전부)")
    export.add_argument("--room", default="", help="이 방만")
    export.add_argument("--out", default="", help="저장 폴더 (기본: DATA_DIR/archive)")
    args = parser.parse_args(argv)

    settings: Settings = get_settings()
    db = Database(settings.db_path)
    try:
        if args.cmd == "list":
            rows = db._query(  # noqa: SLF001
                "SELECT a.room_id, COUNT(*) AS n, MAX(a.created_at) AS last, "
                "  COALESCE(r.label, '') AS label "
                "FROM archive a LEFT JOIN rooms r ON r.room_id = a.room_id "
                "GROUP BY a.room_id ORDER BY last DESC"
            )
            if not rows:
                print("보관된 대화가 없습니다.")
                return 0
            for row in rows:
                stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["last"]))
                print(f"{row['label'] or row['room_id']}  ·  {row['n']}건  ·  마지막 {stamp}")
            return 0

        out_dir = Path(args.out) if args.out else settings.archive_dir
        if args.room:
            path = export_room(db, args.room, out_dir, settings.bot_name)
            print(path)
        else:
            for path in export_all(db, out_dir, settings.bot_name):
                print(path)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
