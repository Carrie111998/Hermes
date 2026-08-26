"""여러 색인을 하나처럼 검색한다.

한 파일에 다 넣으면 안 되는 이유는 속도입니다. 1.27GB짜리 단일 색인에서
BM25 검색이 2,050ms 였고, 자료별로 쪼개니 85ms 였습니다. 5초 룰에서 2초는
답변 하나를 통째로 날리는 값입니다.

그래서 ``data/rag/`` 안의 ``*.sqlite3`` 를 각각 열고, 질문이 오면 필요한
것만 — 또는 전부를 동시에 — 뒤집니다. 컬렉션 이름은 파일 이름입니다.

    data/rag/books.sqlite3        → "books"
    data/rag/commentary.sqlite3   → "commentary"
    data/rag/cases.sqlite3        → "cases"
    data/rag/skills.sqlite3       → "skills"
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from .store import Hit, RagStore

log = logging.getLogger(__name__)

# Reciprocal-rank fusion constant. BM25 scores from *different* indexes are
# not comparable — each index has its own term statistics — so merging by
# raw score quietly favours whichever corpus happens to be smallest. Ranks
# are comparable, and 60 is the value the RRF paper settled on.
RRF_K = 60


class MultiRagStore:
    """A dict of collection → RagStore, searched together."""

    def __init__(self, stores: dict[str, RagStore]) -> None:
        self._stores = dict(stores)

    # ── construction ─────────────────────────────────────────────────────
    @classmethod
    def discover(cls, directory: Path | str, legacy: Path | str | None = None) -> MultiRagStore:
        """Open every ``*.sqlite3`` under ``directory``.

        ``legacy`` is the pre-split single index; it is opened as the
        "corpus" collection so an existing deployment keeps working after
        an upgrade without re-indexing anything.
        """
        stores: dict[str, RagStore] = {}
        folder = Path(directory)
        if folder.is_dir():
            for path in sorted(folder.glob("*.sqlite3")):
                if path.name.endswith(("-wal", "-shm")):
                    continue
                stores[path.stem] = RagStore(path)
        legacy_path = Path(legacy) if legacy else None
        if legacy_path is not None and legacy_path.exists() and legacy_path.stem not in stores:
            stores[legacy_path.stem] = RagStore(legacy_path)
        if not stores:
            # Nothing indexed yet — keep the legacy path so the first
            # ingest lands somewhere the running server will find.
            target = legacy_path or folder / "corpus.sqlite3"
            stores[target.stem] = RagStore(target)
        return cls(stores)

    def close(self) -> None:
        for store in self._stores.values():
            store.close()

    # ── introspection ────────────────────────────────────────────────────
    def collections(self) -> list[str]:
        return sorted(self._stores)

    def store(self, collection: str) -> RagStore | None:
        return self._stores.get(collection)

    def stats(self) -> dict[str, int]:
        total = {"documents": 0, "chunks": 0, "embedded": 0, "collections": len(self._stores)}
        for store in self._stores.values():
            for key, value in store.stats().items():
                total[key] = total.get(key, 0) + value
        return total

    def stats_by_collection(self) -> dict[str, dict[str, int]]:
        return {name: store.stats() for name, store in self._stores.items()}

    # ── query ────────────────────────────────────────────────────────────
    def _targets(self, collection: str) -> list[tuple[str, RagStore]]:
        if collection and collection in self._stores:
            return [(collection, self._stores[collection])]
        if collection:
            log.info("unknown rag collection %r — searching all", collection)
        return list(self._stores.items())

    def _fan_out(self, targets: list[tuple[str, RagStore]], work) -> list[list[Hit]]:  # noqa: ANN001
        if len(targets) == 1:
            name, store = targets[0]
            return [self._tag(work(store), name)]
        # A client is waiting: 4 indexes sequentially is 4× the latency of
        # 4 indexes at once, and these are file reads, not CPU work.
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            futures = [(name, pool.submit(work, store)) for name, store in targets]
            results = []
            for name, future in futures:
                try:
                    results.append(self._tag(future.result(), name))
                except Exception:  # noqa: BLE001 — one bad index must not lose the rest
                    log.exception("rag collection %s failed", name)
                    results.append([])
            return results

    @staticmethod
    def _tag(hits: list[Hit], collection: str) -> list[Hit]:
        return [replace(hit, collection=collection) for hit in hits]

    @staticmethod
    def _fuse(ranked: list[list[Hit]], top_k: int) -> list[Hit]:
        scored: list[tuple[float, Hit]] = []
        for hits in ranked:
            for rank, hit in enumerate(hits):
                scored.append((1.0 / (RRF_K + rank + 1), hit))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [hit for _score, hit in scored[:top_k]]

    def search(
        self, query: str, top_k: int = 6, candidates: int = 40, *, collection: str = ""
    ) -> list[Hit]:
        targets = self._targets(collection)
        ranked = self._fan_out(targets, lambda store: store.search(query, top_k, candidates))
        if len(ranked) == 1:
            return ranked[0][:top_k]
        return self._fuse(ranked, top_k)

    def search_with_embedding(
        self,
        query: str,
        query_vector: Sequence[float],
        top_k: int = 6,
        candidates: int = 40,
        *,
        collection: str = "",
    ) -> list[Hit]:
        targets = self._targets(collection)
        ranked = self._fan_out(
            targets,
            lambda store: store.search_with_embedding(query, query_vector, top_k, candidates),
        )
        if len(ranked) == 1:
            return ranked[0][:top_k]
        return self._fuse(ranked, top_k)
