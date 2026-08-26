"""문서 그래프 — 같은 조문·같은 판례를 말하는 문서끼리 잇는다.

키워드 검색과 임베딩은 "비슷한 문장"을 찾습니다. 법률 자료에서 정작 필요한
것은 그것이 아닐 때가 많습니다. **민법 제618조를 다루는 문서 전부**, 또는
**이 판례를 인용한 문서 전부** — 이건 문장이 닮았는지와 상관없는 질문이고,
연결로만 답할 수 있습니다.

세 개의 표뿐입니다.

    notes      노트 하나 = 파일 하나
    entities   조문 · 판례 · 키워드
    mentions   어느 노트가 어느 엔티티를 몇 번 말했는가

여기서 백링크 수(= 몇 개의 문서가 이 조문을 말하는가)가 나오고, 그것이 곧
허브노트이자 문서의 중요도입니다.
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .citation import alias_table, entity_key, parse_statute
from .note import CASE, STATUTE, WikiNote

STATUTE_ENTITY = "statute"
CASE_ENTITY = "case"
KEYWORD_ENTITY = "keyword"

_ENTITY_LABELS = {
    STATUTE_ENTITY: "법령",
    CASE_ENTITY: "판례",
    KEYWORD_ENTITY: "키워드",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT '기타',
    collection    TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    as_of         TEXT NOT NULL DEFAULT '',
    effective_on  TEXT NOT NULL DEFAULT '',
    decided_on    TEXT NOT NULL DEFAULT '',
    written_on    TEXT NOT NULL DEFAULT '',
    case_no       TEXT NOT NULL DEFAULT '',
    superseded_by TEXT NOT NULL DEFAULT '',
    verified      INTEGER NOT NULL DEFAULT 0,
    summary       TEXT NOT NULL DEFAULT '',
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_kind ON notes(kind);

CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    display    TEXT NOT NULL DEFAULT '',
    note_count INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_key ON entities(kind, key);

CREATE TABLE IF NOT EXISTS mentions (
    note_id   INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    weight    INTEGER NOT NULL DEFAULT 1,
    in_title  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (note_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_mentions_entity ON mentions(entity_id);
"""


def classify(key: str) -> str:
    """엔티티 키가 조문인지 판례인지 그냥 낱말인지."""
    text = (key or "").strip()
    if not text:
        return KEYWORD_ENTITY
    if re.fullmatch(r"(?:19|20)\d{2}[가-힣]{1,3}\d{1,6}", text.replace(" ", "")):
        return CASE_ENTITY
    # '민618' 처럼 짧게 적힌 것도 조문입니다 — 파서에게 물어봅니다.
    if any(char.isdigit() for char in text) and parse_statute(text) is not None:
        return STATUTE_ENTITY
    return KEYWORD_ENTITY


def _canonical(key: str) -> tuple[str, str, str]:
    """``(종류, 그래프 키, 보일 이름)``.

    키는 띄어쓰기를 지운 형태입니다 — 같은 법이 문서마다 다르게 띄어 쓰여
    두 마디로 갈라지는 것을 막습니다. 보일 이름은 띄어 쓴 쪽을 씁니다.
    """
    kind = classify(key)
    display = re.sub(r"\s+", " ", (key or "").strip())
    if kind == STATUTE_ENTITY:
        ref = parse_statute(display)
        if ref is not None:
            display = ref.key
    return kind, entity_key(display), display


@dataclass(frozen=True)
class Related:
    path: str
    title: str
    kind: str
    score: float
    as_of: str
    shared: tuple[str, ...]
    superseded_by: str = ""

    @property
    def stale(self) -> bool:
        return bool(self.superseded_by)


@dataclass(frozen=True)
class EntityRow:
    kind: str
    key: str
    display: str
    note_count: int


class WikiGraph:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 흔한 조문일수록 이어질 값어치가 낮습니다 — '민법 제1조'로 이어진
        # 두 문서는 사실 아무 상관이 없을 수 있습니다.
        self._conn.create_function("idf", 1, lambda n: 1.0 / math.log(2.0 + max(int(n or 0), 0)))
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 적재 ─────────────────────────────────────────────────────────────
    def upsert_note(self, note: WikiNote, summary: str = "") -> int:
        """노트 하나와 그 노트가 말하는 엔티티를 통째로 갈아 끼운다."""
        path = note.path or note.title
        keys = note.entity_keys
        weights = note.weights or {}
        with self._lock:
            row = self._conn.execute("SELECT id FROM notes WHERE path = ?", (path,)).fetchone()
            fields = (
                note.title,
                note.kind,
                note.collection,
                note.source,
                note.as_of,
                note.effective_on,
                note.decided_on,
                note.written_on,
                note.case_no,
                note.superseded_by,
                1 if note.verified else 0,
                summary or _summary_of(note),
                time.time(),
            )
            if row is None:
                cursor = self._conn.execute(
                    "INSERT INTO notes(path, title, kind, collection, source, as_of, "
                    "effective_on, decided_on, written_on, case_no, superseded_by, verified, "
                    "summary, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (path, *fields),
                )
                note_id = int(cursor.lastrowid or 0)
            else:
                note_id = int(row["id"])
                self._conn.execute(
                    "UPDATE notes SET title=?, kind=?, collection=?, source=?, as_of=?, "
                    "effective_on=?, decided_on=?, written_on=?, case_no=?, superseded_by=?, "
                    "verified=?, summary=?, updated_at=? WHERE id=?",
                    (*fields, note_id),
                )
                self._conn.execute("DELETE FROM mentions WHERE note_id = ?", (note_id,))

            heading = f"{note.title}\n{note.case_no}"
            for key in keys:
                entity_id = self._entity_id_locked(key)
                self._conn.execute(
                    "INSERT OR REPLACE INTO mentions(note_id, entity_id, weight, in_title) "
                    "VALUES(?,?,?,?)",
                    (note_id, entity_id, int(weights.get(key, 1)), 1 if key in heading else 0),
                )
            self._conn.commit()
            self._recount_locked()
            return note_id

    def _entity_id_locked(self, key: str) -> int:
        kind, canonical, display = _canonical(key)
        row = self._conn.execute(
            "SELECT id, display FROM entities WHERE kind = ? AND key = ?", (kind, canonical)
        ).fetchone()
        if row is not None:
            # 띄어 쓴 이름이 나중에 나오면 그쪽으로 갈아 끼웁니다 — 허브노트
            # 제목은 사람이 읽는 것이니까요.
            if display.count(" ") > str(row["display"] or "").count(" "):
                self._conn.execute(
                    "UPDATE entities SET display = ? WHERE id = ?", (display, row["id"])
                )
            return int(row["id"])
        cursor = self._conn.execute(
            "INSERT INTO entities(kind, key, display, note_count) VALUES(?,?,?,0)",
            (kind, canonical, display),
        )
        return int(cursor.lastrowid or 0)

    def _recount_locked(self) -> None:
        self._conn.execute(
            "UPDATE entities SET note_count = "
            "(SELECT COUNT(*) FROM mentions WHERE mentions.entity_id = entities.id)"
        )
        self._conn.commit()

    def forget_note(self, path: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT id FROM notes WHERE path = ?", (path,)).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM mentions WHERE note_id = ?", (row["id"],))
            self._conn.execute("DELETE FROM notes WHERE id = ?", (row["id"],))
            self._conn.commit()
            self._recount_locked()
            return True

    # ── 조회 ─────────────────────────────────────────────────────────────
    def entity(self, key: str) -> EntityRow | None:
        kind, canonical, _display = _canonical(key)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM entities WHERE kind = ? AND key = ?", (kind, canonical)
            ).fetchone()
        return (
            EntityRow(row["kind"], row["key"], row["display"], int(row["note_count"]))
            if row
            else None
        )

    def notes_for(self, key: str, limit: int = 50) -> list[sqlite3.Row]:
        """이 조문·판례·키워드를 말하는 문서들. 최신 것이 위로 옵니다."""
        entity = self.entity(key)
        if entity is None:
            return []
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT n.*, m.weight AS weight, m.in_title AS in_title "
                    "  FROM mentions m JOIN notes n ON n.id = m.note_id "
                    "  JOIN entities e ON e.id = m.entity_id "
                    " WHERE e.kind = ? AND e.key = ? "
                    " ORDER BY m.in_title DESC, n.as_of DESC, m.weight DESC LIMIT ?",
                    (entity.kind, entity.key, limit),
                ).fetchall()
            )

    def related(self, paths: list[str], limit: int = 8, exclude_stale: bool = True) -> list[Related]:
        """시드 문서와 **같은 조문·판례·키워드를 말하는** 다른 문서들.

        FTS5나 임베딩이 찾아준 몇 개를 씨앗으로 넣으면, 문장이 닮지 않아도
        같은 것을 다루는 문서가 따라 나옵니다.
        """
        if not paths:
            return []
        marks = ",".join("?" for _ in paths)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT n.path AS path, n.title AS title, n.kind AS kind, n.as_of AS as_of,
                       n.superseded_by AS superseded_by,
                       SUM(m2.weight * idf(e.note_count)) AS score,
                       GROUP_CONCAT(DISTINCT e.key) AS shared
                  FROM mentions m1
                  JOIN entities e ON e.id = m1.entity_id
                  JOIN mentions m2 ON m2.entity_id = m1.entity_id
                  JOIN notes n ON n.id = m2.note_id
                 WHERE m1.note_id IN (SELECT id FROM notes WHERE path IN ({marks}))
                   AND n.path NOT IN ({marks})
                 GROUP BY n.id
                 ORDER BY score DESC
                 LIMIT ?
                """,  # noqa: S608 — placeholders only
                (*paths, *paths, limit * 3),
            ).fetchall()

        out: list[Related] = []
        for row in rows:
            if exclude_stale and row["superseded_by"]:
                continue
            out.append(
                Related(
                    path=str(row["path"]),
                    title=str(row["title"]),
                    kind=str(row["kind"]),
                    score=float(row["score"] or 0.0),
                    as_of=str(row["as_of"] or ""),
                    shared=tuple((row["shared"] or "").split(",")[:6]),
                    superseded_by=str(row["superseded_by"] or ""),
                )
            )
            if len(out) >= limit:
                break
        return out

    def resolve(self, hints: list[str]) -> list[str]:
        """경로·파일이름·제목 중 무엇으로 불러도 노트 경로를 찾아 준다.

        RAG 색인이 돌려주는 ``source`` 와 그래프의 ``path`` 가 어긋나도(폴더를
        옮기셨거나 색인을 다른 뿌리에서 돌리셨거나) 이어지게 하는 다리입니다.
        """
        found: list[str] = []
        with self._lock:
            for hint in hints:
                text = (hint or "").strip()
                if not text:
                    continue
                stem = Path(text).stem
                row = self._conn.execute(
                    "SELECT path FROM notes WHERE path = ? OR title = ? OR path LIKE ? "
                    "ORDER BY LENGTH(path) LIMIT 1",
                    (text, text, f"%{stem}%"),
                ).fetchone()
                if row is not None and str(row["path"]) not in found:
                    found.append(str(row["path"]))
        return found

    def note(self, path: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM notes WHERE path = ?", (path,)).fetchone()

    def hubs(self, min_notes: int = 2, kind: str = "", limit: int = 500) -> list[EntityRow]:
        """문서 여러 개가 함께 말하는 엔티티 = 허브 후보."""
        sql = "SELECT * FROM entities WHERE note_count >= ?"
        params: list[object] = [min_notes]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY note_count DESC, key LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            EntityRow(row["kind"], row["key"], row["display"], int(row["note_count"]))
            for row in rows
        ]

    def important_notes(self, limit: int = 20) -> list[sqlite3.Row]:
        """백링크가 많은 문서 = 이 서가의 중심 문서.

        '내가 제목으로 내건 것을 남들이 얼마나 말하는가'로 셉니다. 그냥 남을
        많이 인용한 문서가 아니라, 남들이 찾아오는 문서가 중요한 문서입니다.
        """
        with self._lock:
            return list(
                self._conn.execute(
                    """
                    SELECT n.*, COUNT(DISTINCT m2.note_id) AS inbound
                      FROM notes n
                      JOIN mentions m1 ON m1.note_id = n.id AND m1.in_title = 1
                      JOIN mentions m2 ON m2.entity_id = m1.entity_id AND m2.note_id != n.id
                     GROUP BY n.id
                     ORDER BY inbound DESC, n.as_of DESC
                     LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def dangling_keywords(self, limit: int = 200) -> list[EntityRow]:
        """한 문서에서만 나오는 키워드 — 오타이거나, 아직 안 쓴 문서입니다."""
        return [entity for entity in self.hubs(min_notes=1, limit=limit) if entity.note_count == 1]

    def stats(self) -> dict[str, int]:
        with self._lock:
            notes = self._conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
            mentions = self._conn.execute("SELECT COUNT(*) AS n FROM mentions").fetchone()["n"]
            by_kind = {
                str(row["kind"]): int(row["n"])
                for row in self._conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM entities GROUP BY kind"
                )
            }
        return {
            "notes": int(notes),
            "mentions": int(mentions),
            "statutes": by_kind.get(STATUTE_ENTITY, 0),
            "cases": by_kind.get(CASE_ENTITY, 0),
            "keywords": by_kind.get(KEYWORD_ENTITY, 0),
        }

    # ── 허브노트 ─────────────────────────────────────────────────────────
    def render_hub(self, entity: EntityRow) -> str:
        """옵시디언이 그대로 읽는 허브노트 한 장."""
        rows = self.notes_for(entity.key, limit=200)
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["kind"]), []).append(row)

        head = {
            "title": entity.display,
            "kind": "허브",
            "entity": _ENTITY_LABELS.get(entity.kind, entity.kind),
            "note_count": str(entity.note_count),
        }
        lines = ["---"]
        lines.extend(f"{key}: {value}" for key, value in head.items())
        lines.append("---")
        lines.append("")
        lines.append(f"# {entity.display}")
        lines.append("")
        lines.append(f"이 {_ENTITY_LABELS.get(entity.kind, '항목')}을 다루는 문서 {entity.note_count}개.")
        lines.append("")
        lines.append("> 자동 생성된 허브노트입니다. 직접 고치면 다음 빌드에서 덮어씁니다.")
        lines.append("")

        for kind in (CASE, STATUTE, "주석서", "서적", "실무편람", "서식", "기타"):
            group = grouped.pop(kind, [])
            if not group:
                continue
            lines.append(f"## {kind}")
            for row in group:
                lines.append(_hub_line(row))
            lines.append("")
        for kind, group in grouped.items():
            lines.append(f"## {kind}")
            for row in group:
                lines.append(_hub_line(row))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def write_hubs(self, directory: Path | str, min_notes: int = 2) -> int:
        """허브노트를 폴더에 쏟아 낸다. 옵시디언 볼트 안에 두시면 됩니다."""
        root = Path(directory)
        written = 0
        for entity in self.hubs(min_notes=min_notes):
            folder = root / _ENTITY_LABELS.get(entity.kind, "기타")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{safe_filename(entity.display)}.md").write_text(
                self.render_hub(entity), encoding="utf-8"
            )
            written += 1
        if written:
            (root / "허브 색인.md").write_text(self._render_index(), encoding="utf-8")
        return written

    def _render_index(self) -> str:
        lines = ["# 허브 색인", "", "가장 많이 언급되는 것부터.", ""]
        for kind in (STATUTE_ENTITY, CASE_ENTITY, KEYWORD_ENTITY):
            entities = self.hubs(min_notes=2, kind=kind, limit=40)
            if not entities:
                continue
            lines.append(f"## {_ENTITY_LABELS[kind]}")
            for entity in entities:
                folder = _ENTITY_LABELS[kind]
                lines.append(
                    f"- [[{folder}/{safe_filename(entity.display)}|{entity.display}]] "
                    f"— {entity.note_count}개 문서"
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _hub_line(row: sqlite3.Row) -> str:
    stem = Path(str(row["path"])).stem or str(row["title"])
    bits = []
    if row["as_of"]:
        bits.append(str(row["as_of"]))
    if row["weight"] and int(row["weight"]) > 1:
        bits.append(f"{int(row['weight'])}회")
    if row["superseded_by"]:
        bits.append(f"⚠ 연혁 — {row['superseded_by']} 로 갈음")
    tail = f" — {' · '.join(bits)}" if bits else ""
    return f"- [[{stem}|{row['title']}]]{tail}"


def _summary_of(note: WikiNote) -> str:
    text = re.sub(r"[#>*`\[\]]", " ", note.body or "")
    return re.sub(r"\s+", " ", text).strip()[:400]


_UNSAFE = re.compile(r'[\\/:*?"<>|]')


def safe_filename(name: str) -> str:
    cleaned = _UNSAFE.sub(" ", name or "").strip()
    return re.sub(r"\s+", " ", cleaned)[:120] or "무제"


def known_law_names() -> list[str]:
    return list(alias_table().full_names)
