"""위키 볼트를 그래프로 만들고, 허브노트를 쓰고, 낡은 자료를 잡아낸다.

변호사님 PC에서 도는 **찬 길(cold path)** 입니다. 상담자가 기다리는 동안
하는 일이 아니므로 느려도 됩니다.

    python -m kakao_legal_bot.app.wiki.build stub  --raw ./vault/raw --wiki ./vault/wiki
    python -m kakao_legal_bot.app.wiki.build index --wiki ./vault/wiki
    python -m kakao_legal_bot.app.wiki.build hubs  --wiki ./vault/wiki --out ./vault/hubs
    python -m kakao_legal_bot.app.wiki.build lint  --wiki ./vault/wiki --apply
    python -m kakao_legal_bot.app.wiki.build related "임대차보증금 반환"

``index`` 가 만드는 노트 경로는 ``--wiki`` 폴더 기준 상대경로이고, RAG 색인의
``source`` 와 같은 값입니다. 그래야 키워드 검색으로 찾은 문서에서 그래프로
건너갈 수 있습니다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config import get_settings
from .graph import WikiGraph
from .lint import apply_supersession, lint
from .note import CASE, COMMENTARY, FORM, PRACTICE, STATUTE, WikiNote
from .sources import main_law_of, read_source

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "wiki_prompt.md"

# 폴더 이름으로 자료의 성격을 짐작합니다. 틀리면 노트에서 고치시면 됩니다.
_KIND_HINTS: tuple[tuple[str, str], ...] = (
    ("판례", CASE),
    ("cases", CASE),
    ("법령", STATUTE),
    ("statutes", STATUTE),
    ("주석", COMMENTARY),
    ("commentary", COMMENTARY),
    ("실무", PRACTICE),
    ("편람", PRACTICE),
    ("practice", PRACTICE),
    ("서식", FORM),
    ("forms", FORM),
)


def guess_kind(path: Path) -> str:
    text = str(path).lower()
    for needle, kind in _KIND_HINTS:
        if needle in text:
            return kind
    return "서적"


# 빌드가 스스로 만든 파일들. 이것들을 다시 노트로 읽으면 그래프에 유령이 생깁니다.
_GENERATED = {"LINT.md", "허브 색인.md"}


def iter_notes(directory: Path) -> list[WikiNote]:
    notes: list[WikiNote] = []
    for path in sorted(directory.rglob("*.md")):
        if path.name.startswith((".", "_")) or path.name in _GENERATED:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"  ! {path} 를 읽지 못했습니다 ({exc})", file=sys.stderr)
            continue
        note = WikiNote.from_markdown(text, str(path.relative_to(directory)))
        if note.kind == "허브":
            continue  # 자동 생성된 허브노트
        if note.kind == "기타":
            note.kind = guess_kind(path)
        notes.append(note.enrich(default_law=_default_law(path) or main_law_of(text, str(path))))
    return notes


_SOURCE_SUFFIXES = {".md", ".markdown", ".html", ".htm", ".txt"}


def _source_files(root: Path) -> list[Path]:
    """원문 폴더의 자료 파일. HTML 과 같은 이름의 .md 가 있으면 HTML 을 씁니다."""
    files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in _SOURCE_SUFFIXES
        and not path.name.startswith((".", "_"))
    ]
    html_stems = {
        path.with_suffix("") for path in files if path.suffix.lower() in {".html", ".htm"}
    }
    return [
        path
        for path in files
        if path.suffix.lower() in {".html", ".htm"} or path.with_suffix("") not in html_stems
    ]


def _default_law(path: Path) -> str:
    """폴더·파일 이름에서 그 자료의 주된 법령을 읽는다.

    민법 주석서 폴더 안이라면 "제618조"만 적어도 민법으로 읽힙니다.
    """
    return main_law_of("", str(path))


# ── stub ─────────────────────────────────────────────────────────────────
def make_stubs(raw_dir: Path, wiki_dir: Path, overwrite: bool = False) -> tuple[int, int]:
    """원문마다 위키 노트의 뼈대를 만든다 (frontmatter는 채워서).

    본문은 코덱스가 ``wiki_prompt.md`` 를 보고 씁니다. 여기서 미리 채우는
    조문·판례·키워드는 **원문에서 기계적으로 뽑은 것**이라 틀릴 일이 없고,
    그만큼 코덱스가 지어낼 여지가 줄어듭니다.
    """
    made = skipped = 0
    jobs: list[dict[str, object]] = []
    for path in sorted(_source_files(raw_dir)):
        relative = path.relative_to(raw_dir)
        target = (wiki_dir / relative).with_suffix(".md")
        if target.exists() and not overwrite:
            skipped += 1
            continue
        source = str(Path("raw") / relative)
        note = read_source(path)
        note.path = str(relative.with_suffix(".md"))
        note.source = source
        note.collection = relative.parts[0] if len(relative.parts) > 1 else ""
        if note.kind in {"기타", ""}:
            note.kind = guess_kind(path)
        extracted = note.body
        note.body = (
            f"<!-- TODO: wiki_prompt.md 규칙대로 다시 씁니다. 원문: {source} -->\n"
            f"<!-- frontmatter 의 조문·판례·키워드는 원문에서 기계적으로 뽑은 것입니다. -->\n"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(note.to_markdown(), encoding="utf-8")
        # HTML 은 그대로 두면 코덱스가 못 읽습니다. 구조를 살려 옮긴 본문을
        # 원문 옆에 나란히 둡니다 — 무엇이 근거였는지가 남아야 하니까요.
        if path.suffix.lower() in {".html", ".htm"}:
            path.with_suffix(".md").write_text(extracted, encoding="utf-8")
        jobs.append(
            {
                "raw": str(path),
                "wiki": str(target),
                "kind": note.kind,
                "main_law": note.extra.get("main_law", ""),
                "statutes": note.statutes[:40],
                "cases": note.cases[:40],
                "keywords": note.keywords[:40],
            }
        )
        made += 1
    if jobs:
        (wiki_dir / "_wiki-jobs.jsonl").write_text(
            "\n".join(json.dumps(job, ensure_ascii=False) for job in jobs) + "\n",
            encoding="utf-8",
        )
    return made, skipped


# ── 명령 ─────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m kakao_legal_bot.app.wiki.build",
        description="위키 볼트 → 그래프 · 허브노트 · lint",
    )
    parser.add_argument(
        "command", choices=("stub", "index", "hubs", "lint", "related", "stats", "prompt")
    )
    parser.add_argument("query", nargs="?", default="", help="related 에서 쓸 노트 경로/제목")
    parser.add_argument("--raw", default="./vault/raw")
    parser.add_argument("--wiki", default="./vault/wiki")
    parser.add_argument("--out", default="", help="hubs/lint 결과를 쓸 곳")
    parser.add_argument("--graph", default=str(settings.wiki_graph_path))
    parser.add_argument("--min-notes", type=int, default=2, help="허브노트를 만들 최소 문서 수")
    parser.add_argument("--apply", action="store_true", help="lint 결과를 노트에 반영")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "prompt":
        print(PROMPT_PATH.read_text(encoding="utf-8"))
        return 0

    wiki_dir = Path(args.wiki)

    if args.command == "stub":
        raw_dir = Path(args.raw)
        if not raw_dir.is_dir():
            print(f"원문 폴더가 없습니다: {raw_dir}", file=sys.stderr)
            return 1
        wiki_dir.mkdir(parents=True, exist_ok=True)
        made, skipped = make_stubs(raw_dir, wiki_dir, overwrite=args.overwrite)
        print(f"뼈대 {made}개 생성, {skipped}개는 이미 있어 건너뜀")
        print(f"이제 코덱스에게 {PROMPT_PATH.name} 규칙대로 본문을 채우게 하세요.")
        print(f"작업 목록: {wiki_dir / '_wiki-jobs.jsonl'}")
        return 0

    if not wiki_dir.is_dir():
        print(f"위키 폴더가 없습니다: {wiki_dir}", file=sys.stderr)
        return 1

    if args.command == "lint":
        notes = iter_notes(wiki_dir)
        titles = {note.title for note in notes}
        report = lint(notes, known_titles=titles)
        text = report.to_markdown()
        destination = Path(args.out) if args.out else wiki_dir / "LINT.md"
        destination.write_text(text, encoding="utf-8")
        print(text)
        print(f"→ {destination}")
        if args.apply:
            changed = apply_supersession(notes, report)
            for note in changed:
                (wiki_dir / note.path).write_text(note.to_markdown(), encoding="utf-8")
            print(f"연혁 표시 {len(changed)}개 노트에 반영")
            worklist = report.to_worklist()
            if worklist:
                jobs = wiki_dir / "_lint-worklist.jsonl"
                jobs.write_text(
                    "\n".join(json.dumps(item, ensure_ascii=False) for item in worklist) + "\n",
                    encoding="utf-8",
                )
                print(f"사람이 읽어야 할 {len(worklist)}건 → {jobs}")
        return 1 if report.errors else 0

    graph = WikiGraph(args.graph)
    try:
        if args.command == "index":
            notes = iter_notes(wiki_dir)
            for note in notes:
                graph.upsert_note(note)
            stats = graph.stats()
            print(
                f"노트 {stats['notes']} · 조문 {stats['statutes']} · 판례 {stats['cases']} "
                f"· 키워드 {stats['keywords']} · 연결 {stats['mentions']}"
            )
            print(f"그래프: {args.graph}")
            return 0

        if args.command == "hubs":
            out = Path(args.out) if args.out else wiki_dir.parent / "hubs"
            written = graph.write_hubs(out, min_notes=args.min_notes)
            print(f"허브노트 {written}장 → {out}")
            print("옵시디언 볼트 안에 두시면 백링크가 바로 잡힙니다.")
            return 0

        if args.command == "stats":
            print(json.dumps(graph.stats(), ensure_ascii=False, indent=2))
            print("\n[가장 많이 언급되는 것]")
            for entity in graph.hubs(min_notes=2, limit=15):
                print(f"  {entity.note_count:4}  {entity.display}")
            print("\n[백링크가 많은 문서]")
            for row in graph.important_notes(limit=10):
                print(f"  {row['inbound']:4}  {row['title']}")
            return 0

        if args.command == "related":
            if not args.query:
                print("문서 경로나 제목을 적어주세요.", file=sys.stderr)
                return 1
            paths = graph.resolve([args.query])
            if not paths:
                print("그 문서를 그래프에서 찾지 못했습니다.", file=sys.stderr)
                return 1
            for item in graph.related(paths, limit=15):
                shared = ", ".join(item.shared)
                print(f"  {item.score:6.2f}  {item.title}  ({item.kind})  ← {shared}")
            return 0
    finally:
        graph.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
