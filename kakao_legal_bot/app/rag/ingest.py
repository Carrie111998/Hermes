"""Load the local legal corpus into the RAG index.

    python -m kakao_legal_bot.app.rag.ingest ./corpus
    python -m kakao_legal_bot.app.rag.ingest ./corpus --embed
    python -m kakao_legal_bot.app.rag.ingest --stats

Supported inputs: .txt .md .markdown .json .jsonl .docx .pdf
(.pdf needs ``pypdf``; .docx is read straight out of the zip, no dependency.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

from ..config import get_settings
from .multi import MultiRagStore
from .store import RagStore, chunk_text

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".text"}
SUPPORTED = TEXT_SUFFIXES | {".json", ".jsonl", ".docx", ".pdf"}


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:32]


def read_docx(path: Path) -> str:
    """Pull the visible text out of a .docx without python-docx.

    A .docx is a zip; word/document.xml holds the body. Paragraph breaks
    are </w:p>, and everything inside <w:t> is literal text.
    """
    with zipfile.ZipFile(path) as archive:
        try:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        except KeyError:
            return ""
    xml = re.sub(r"</w:p>", "\n\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>|(\n)", xml, flags=re.DOTALL)
    out = "".join(a or b for a, b in texts)
    out = (
        out.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def read_pdf(path: Path) -> list[tuple[str, str]]:
    """Return ``[(page_locator, text), ...]``; empty when pypdf is missing."""
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        print(f"  ! {path.name}: pypdf 미설치 — `pip install pypdf` 후 다시 실행하세요", file=sys.stderr)
        return []
    reader = PdfReader(str(path))
    pages: list[tuple[str, str]] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — one broken page must not stop ingest
            text = ""
        if text.strip():
            pages.append((f"{number}쪽", text))
    return pages


def iter_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED and not path.name.startswith("."):
            yield path


def ingest_path(store: RagStore, path: Path, root: Path, chunk_size: int, overlap: int) -> int:
    source = str(path.relative_to(root)) if path != root else path.name
    title = path.stem
    suffix = path.suffix.lower()

    if suffix in TEXT_SUFFIXES:
        body = path.read_text(encoding="utf-8", errors="replace")
        chunks = chunk_text(body, chunk_size, overlap)
        return store.upsert_document(source, title, chunks, sha=_sha(body))

    if suffix == ".docx":
        body = read_docx(path)
        chunks = chunk_text(body, chunk_size, overlap)
        return store.upsert_document(source, title, chunks, sha=_sha(body))

    if suffix == ".pdf":
        page_chunks: list[str] = []
        locators: list[str] = []
        for locator, text in read_pdf(path):
            for piece in chunk_text(text, chunk_size, overlap):
                page_chunks.append(piece)
                locators.append(locator)
        body = "\n".join(page_chunks)
        return store.upsert_document(source, title, page_chunks, sha=_sha(body), locators=locators)

    if suffix in {".json", ".jsonl"}:
        # Records shaped like {"title": ..., "text": ..., "locator": ...}.
        raw = path.read_text(encoding="utf-8", errors="replace")
        records: list[dict[str, object]] = []
        if suffix == ".jsonl":
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        else:
            loaded = json.loads(raw)
            records = loaded if isinstance(loaded, list) else [loaded]
        total = 0
        for index, record in enumerate(records):
            text = str(record.get("text") or record.get("content") or "")
            if not text.strip():
                continue
            record_title = str(record.get("title") or title)
            locator = str(record.get("locator") or record.get("article") or "")
            chunks = chunk_text(text, chunk_size, overlap)
            total += store.upsert_document(
                f"{source}#{index}",
                record_title,
                chunks,
                meta={k: v for k, v in record.items() if k not in {"text", "content"}},
                sha=_sha(text),
                locators=[locator] * len(chunks),
            )
        return total

    return 0


def backfill_embeddings(store: RagStore, model: str, api_key: str, base_url: str) -> int:
    import httpx

    if not api_key:
        print("OPENAI_API_KEY 가 없어 임베딩을 건너뜁니다.", file=sys.stderr)
        return 0
    done = 0
    with httpx.Client(timeout=60.0) as client:
        while True:
            batch = store.chunks_without_embeddings(limit=64)
            if not batch:
                break
            response = client.post(
                f"{base_url}/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "input": [text[:8000] for _, text in batch]},
            )
            response.raise_for_status()
            vectors = response.json()["data"]
            for (chunk_id, _), item in zip(batch, vectors):
                store.set_embedding(chunk_id, item["embedding"])
            done += len(batch)
            print(f"  임베딩 {done}개 완료…")
    return done


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="법률 자료를 RAG 인덱스에 적재합니다.")
    parser.add_argument("path", nargs="?", default=str(settings.corpus_dir))
    parser.add_argument(
        "--collection",
        default="",
        help="자료 구분 (books / commentary / cases / skills …). "
        "data/rag/<이름>.sqlite3 에 따로 색인합니다. 크면 반드시 나누세요.",
    )
    parser.add_argument("--db", default="", help="색인 파일 경로를 직접 지정")
    parser.add_argument("--chunk-size", type=int, default=settings.rag_chunk_chars)
    parser.add_argument("--overlap", type=int, default=settings.rag_chunk_overlap)
    parser.add_argument("--embed", action="store_true", help="OpenAI 임베딩까지 생성")
    parser.add_argument("--stats", action="store_true", help="인덱스 통계만 출력")
    parser.add_argument("--search", default="", help="색인 검색 테스트")
    args = parser.parse_args(argv)

    if args.stats and not args.db and not args.collection:
        # No target named — report every collection the server would open.
        store = MultiRagStore.discover(settings.rag_dir, legacy=settings.data_dir / "rag.sqlite3")
        print(json.dumps(store.stats_by_collection(), ensure_ascii=False, indent=2))
        store.close()
        return 0

    if args.db:
        db_path = Path(args.db)
    elif args.collection:
        db_path = settings.rag_path(args.collection)
    else:
        legacy = settings.data_dir / "rag.sqlite3"
        db_path = legacy if legacy.exists() else settings.rag_path("corpus")
    store = RagStore(db_path)
    print(f"색인 파일: {db_path}")

    if args.stats:
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
        return 0

    if args.search:
        for hit in store.search(args.search, top_k=settings.rag_top_k):
            print(f"[{hit.score:.3f}] {hit.citation}\n{hit.text[:300]}\n")
        return 0

    root = Path(args.path)
    if not root.exists():
        print(f"경로를 찾을 수 없습니다: {root}", file=sys.stderr)
        return 1

    total_files = 0
    total_chunks = 0
    for path in iter_files(root):
        count = ingest_path(store, path, root if root.is_dir() else root.parent, args.chunk_size, args.overlap)
        total_files += 1
        total_chunks += count
        status = f"{count} chunks" if count else "변경 없음"
        print(f"  · {path} → {status}")

    print(f"\n문서 {total_files}개 / 청크 {total_chunks}개 적재 완료")

    if args.embed:
        backfill_embeddings(
            store, settings.rag_embedding_model, settings.openai_api_key, settings.openai_base_url
        )

    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
