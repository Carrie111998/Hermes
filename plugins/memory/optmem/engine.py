"""OptMem engine — portable reimplementation of Taelin's OptMem store.

This is a dependency-free, cross-platform reimplementation of the OptMem
append-only memory. It keeps the EXACT on-disk format of the original
``memo`` tool (fixed-width records, ``LOG.txt`` + ``TREE/<size>``), so logs
are interchangable with https://github.com/VictorTaelin/OptMem.

Differences from the original:
- No ``fcntl`` (POSIX-only). Uses a portable advisory lock: ``msvcrt`` on
  Windows, ``fcntl`` on Unix. Falls back to no-op if neither is available.
- Adds a BM25 ranked search (``recall``) with accent normalization, on top
  of the original regex recall.
- Exposes a small API used by the Hermes ``MemoryProvider`` wrapper:
  ``append``, ``wake_lines``, ``pending_naps``, ``apply_nap``, ``recall``.

Records are fixed width so a memory or block is found by seeking to its
offset — no index file to keep in sync.
"""

from __future__ import annotations

import datetime
import os
import re
import sys
import unicodedata
from collections import defaultdict, deque
from typing import List, Optional, Tuple

# Fixed-width records, identical to the original memo tool so the two are
# byte-compatible on disk.
LOG_REC = 320
TREE_REC = 288

# Blocks up to this many raw memories compress straight from the log.
RAW_MAX = 16

# Sizes (mirror memo defaults).
WAKE_LINES = 208          # ~16k tokens of context printed by wake
ENTRY_CHARS = 280         # longest one memory line, in bytes


# ---------------------------------------------------------------------------
# Portable advisory lock
# ---------------------------------------------------------------------------

def _make_lock(path: str):
    """Return a context manager granting an exclusive lock on ``path``.

    Uses msvcrt on Windows, fcntl on Unix. No-op if neither is importable.
    """
    lockf = open(path + ".lock", "a")
    if sys.platform == "win32":
        try:
            import msvcrt
        except Exception:
            return _NullLock(lockf)
        def _acquire():
            # LK_NBLCK never blocks; spin with backoff so parallel processes
            # (Taelin's target: many concurrent sessions) queue instead of
            # raising 'Resource deadlock avoided' like LK_LOCK does under load.
            import time as _t
            waited = 0.0
            while True:
                try:
                    msvcrt.locking(lockf.fileno(), msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    if waited > 30.0:
                        raise
                    _t.sleep(min(0.01 + waited * 0.2, 0.25))
                    waited += 0.01
        def _release():
            try:
                msvcrt.locking(lockf.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        return _Flock(lockf, _acquire, _release)
    try:
        import fcntl
    except Exception:
        return _NullLock(lockf)
    def _acquire():
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
    def _release():
        try:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
    return _Flock(lockf, _acquire, _release)


class _Flock:
    def __init__(self, f, acquire, release):
        self._f = f
        self._acquire = acquire
        self._release = release
    def __enter__(self):
        self._acquire()
        return self
    def __exit__(self, *exc):
        self._release()
        return False


class _NullLock:
    def __init__(self, f):
        self._f = f
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """lowercase + strip diacritics so 'caçula' == 'cacula'."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def _tokenize(s: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", _normalize(s)) if t]


def _pad(text: str, rec: int) -> bytes:
    b = text.encode("utf-8")
    if len(b) > rec - 1:
        raise ValueError("entry too long: %d bytes, limit %d" % (len(b), rec - 1))
    return b + b" " * (rec - 1 - len(b)) + b"\n"


def _parse(line: str) -> Tuple[int, str, str]:
    head, _, rest = line.partition(" ")
    date, _, text = rest.partition(" ")
    return int(head[1:]), date, text


# ---------------------------------------------------------------------------
# Cover / decay tree (identical math to memo)
# ---------------------------------------------------------------------------

def _cover(T: int, alpha: float) -> List[Tuple[int, int]]:
    """Tile [0,T) with aligned power-of-two blocks; keep a block whole iff its
    size is at most ``alpha`` times its age. Bigger alpha = coarser."""
    root = 1
    while root < T:
        root *= 2
    out: List[Tuple[int, int]] = []
    stack = [(0, root)]
    while stack:
        lo, hi = stack.pop()
        if lo >= T:
            continue
        size = hi - lo
        if size > 1 and (hi > T or size > alpha * (T - lo)):
            mid = (lo + hi) // 2
            stack.append((mid, hi))
            stack.append((lo, mid))
        else:
            out.append((lo, hi))
    out.sort()
    return out


def cover(T: int, budget: int) -> List[Tuple[int, int]]:
    """The blocks ``wake`` prints: at most ``budget`` of them, finest near T.

    Detail decays with age: recent memories stay verbatim, ancient ones
    collapse. If everything fits, nothing is compressed at all.
    """
    if T <= 0:
        return []
    if T <= budget:
        return [(i, i + 1) for i in range(T)]
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if len(_cover(T, mid)) > budget:
            lo = mid
        else:
            hi = mid
    out = _cover(T, hi)
    while len(out) < budget:
        i = max((i for i, b in enumerate(out) if b[1] - b[0] > 1), default=None)
        if i is None:
            break
        lo_, hi_ = out[i]
        mid = (lo_ + hi_) // 2
        out[i:i + 1] = [(lo_, mid), (mid, hi_)]
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class OptMemEngine:
    """Append-only memory store with a binary decay tree and BM25 search."""

    def __init__(self, memory_dir: str):
        self.dir = memory_dir
        os.makedirs(os.path.join(self.dir, "TREE"), exist_ok=True)
        self.log_path = os.path.join(self.dir, "LOG.txt")
        if not os.path.exists(self.log_path):
            open(self.log_path, "a").close()

    # -- low level ----------------------------------------------------------

    def _lock(self):
        return _make_lock(self.log_path)

    def _count(self, path: str, rec: int) -> int:
        try:
            return os.path.getsize(path) // rec
        except FileNotFoundError:
            return 0

    def log_len(self) -> int:
        return self._count(self.log_path, LOG_REC)

    def _repair(self, path: str, rec: int) -> None:
        try:
            n = os.path.getsize(path)
        except FileNotFoundError:
            return
        if n % rec:
            with open(path, "r+b") as f:
                f.truncate(n - n % rec)

    def _log_slice(self, lo: int, hi: int) -> List[Tuple[int, str, str]]:
        with open(self.log_path, "rb") as f:
            f.seek(lo * LOG_REC)
            buf = f.read((hi - lo) * LOG_REC)
        out = []
        for i in range(len(buf) // LOG_REC):
            raw = buf[i * LOG_REC:(i + 1) * LOG_REC].decode("utf-8", "replace").rstrip()
            if not raw.strip():
                continue
            try:
                mid, date, text = _parse(raw)
            except Exception:
                continue
            out.append((mid, date, text))
        return out

    def _tree_path(self, size: int) -> str:
        return os.path.join(self.dir, "TREE", str(size))

    def _tree_get(self, lo: int, hi: int) -> Optional[str]:
        size = hi - lo
        p = self._tree_path(size)
        try:
            with open(p, "rb") as f:
                f.seek((lo // size) * TREE_REC)
                rec = f.read(TREE_REC)
        except FileNotFoundError:
            return None
        try:
            return rec.decode("utf-8", "replace").rstrip() or None
        except Exception:
            return None

    def _tree_put(self, lo: int, hi: int, text: str) -> bool:
        size = hi - lo
        p = self._tree_path(size)
        with self._lock():
            self._repair(p, TREE_REC)
            if self._count(p, TREE_REC) != lo // size:
                return False
            with open(p, "ab") as f:
                f.write(_pad(text, TREE_REC))
                f.flush()
                os.fsync(f.fileno())
        return True

    # -- public writes ------------------------------------------------------

    def append(self, text: str, date: Optional[str] = None) -> int:
        """Append one memory line. Returns its id."""
        text = text.strip()
        if not text:
            raise ValueError("empty memory")
        if "\n" in text or "\r" in text:
            raise ValueError("a memory is one line")
        b = text.encode("utf-8")
        if len(b) > ENTRY_CHARS:
            raise ValueError("too long: %d bytes, limit %d" % (len(b), ENTRY_CHARS))
        if date is None:
            date = datetime.date.today().isoformat()
        with self._lock():
            self._repair(self.log_path, LOG_REC)
            base = self.log_len()
            with open(self.log_path, "ab") as f:
                f.write(_pad("#%d %s %s" % (base, date, text), LOG_REC))
                f.flush()
                os.fsync(f.fileno())
        return base

    def import_lines(self, lines: List[Tuple[str, str]]) -> int:
        """Bulk append (date, text) pairs. Returns first id."""
        with self._lock():
            self._repair(self.log_path, LOG_REC)
            base = self.log_len()
            with open(self.log_path, "ab") as f:
                for k, (date, text) in enumerate(lines):
                    f.write(_pad("#%d %s %s" % (base + k, date, text), LOG_REC))
                f.flush()
                os.fsync(f.fileno())
        return base

    # -- reads --------------------------------------------------------------

    def wake_lines(self, budget: int = WAKE_LINES) -> List[str]:
        """Return the memory context (recent verbatim, old collapsed)."""
        T = self.log_len()
        if T == 0:
            return []
        out: List[str] = []
        for lo, hi in cover(T, budget):
            if hi - lo == 1:
                mid, date, text = self._log_slice(lo, hi)[0]
                out.append("#%d %s %s" % (mid, date, text))
            else:
                s = self._tree_get(lo, hi)
                out.append("#%d-%d %s" % (lo, hi - 1, s or ""))
        return out

    # -- naps (compression) -------------------------------------------------

    def _pending(self, T: int, limit: Optional[int] = None) -> List[Tuple[int, int]]:
        todo: List[Tuple[int, int]] = []
        size = 2
        while size <= T:
            have = self._count(self._tree_path(size), TREE_REC)
            for k in range(have, T // size):
                todo.append((k * size, (k + 1) * size))
                if limit and len(todo) >= limit:
                    return todo
            size *= 2
        return todo

    def pending_naps(self, limit: Optional[int] = None) -> List[Tuple[int, int]]:
        """Blocks that can be built and have not been, smallest first."""
        return self._pending(self.log_len(), limit)

    def nap_prompt(self, lo: int, hi: int) -> str:
        """Build the compression instruction for block [lo,hi)."""
        if hi - lo <= RAW_MAX:
            body = "\n".join("  #%d %s %s" % e for e in self._log_slice(lo, hi))
        else:
            mid = (lo + hi) // 2
            halves = []
            for a, b in ((lo, mid), (mid, hi)):
                s = self._tree_get(a, b)
                if s is None:
                    s = "(missing — rebuild)"
                halves.append("  #%d-%d %s" % (a, b - 1, s))
            body = "\n".join(halves)
        left = len(self.pending_naps()) - 1
        tail = "" if left <= 0 else (
            "\n%d compressions remain" % left)
        return (
            "Compress memories #%d-%d into one line of at most %d characters.\n"
            "Keep what has lasting effect, drop what does not. Invent nothing.\n\n"
            "%s%s\n"
            % (lo, hi - 1, ENTRY_CHARS, body, tail)
        )

    def next_nap(self) -> Optional[Tuple[Tuple[int, int], str]]:
        todo = self.pending_naps(limit=1)
        if not todo:
            return None
        lo, hi = todo[0]
        return (lo, hi), self.nap_prompt(lo, hi)

    def apply_nap(self, lo: int, hi: int, summary: str) -> bool:
        """Store a compression. Returns False if block order changed meanwhile."""
        summary = summary.strip()
        if not summary:
            raise ValueError("empty summary")
        if len(summary.encode("utf-8")) > TREE_REC - 1:
            raise ValueError("summary too long")
        return self._tree_put(lo, hi, summary)

    def forget(self, lo: int, hi: int) -> None:
        """Drop a summary and everything built on top of it; log untouched."""
        size = hi - lo
        with self._lock():
            while size <= self.log_len():
                p = self._tree_path(size)
                k = lo // size
                n = self._count(p, TREE_REC)
                if n > k:
                    with open(p, "r+b") as f:
                        f.truncate(k * TREE_REC)
                size *= 2

    # -- search (BM25 + regex) ----------------------------------------------

    def _all_records(self) -> List[Tuple[int, str, str, List[str]]]:
        T = self.log_len()
        docs = []
        for i in range(T):
            try:
                mid, date, text = self._log_slice(i, i + 1)[0]
            except Exception:
                continue
            docs.append((mid, date, text, _tokenize(text)))
        return docs

    # -- index (built once, reused) ---------------------------------------

    def build_index(self) -> None:
        """Build the in-memory BM25 index from the whole log.

        Call once after writes settle (or before a batch of recall calls).
        Avoids re-tokenizing every record on every query. The index is
        invalidated automatically when the log grows (see ``_index_len``).
        """
        docs = self._all_records()
        self._index_docs = docs
        self._index_len = len(docs)
        N = len(docs)
        df = defaultdict(int)
        for _, _, _, toks in docs:
            for t in set(toks):
                df[t] += 1
        self._index_idf = {t: (N - df[t] + 0.5) / (df[t] + 0.5) for t in df}
        self._index_avgdl = sum(len(t) for _, _, _, t in docs) / N if N else 0
        self._index_n = N

    def _index_stale(self) -> bool:
        """True if the log changed since the index was built."""
        if getattr(self, "_index_docs", None) is None:
            return True
        return self.log_len() != getattr(self, "_index_len", -1)

    def recall(self, query: str, topk: int = 5, regex: bool = False,
               use_index: bool = True) -> List[Tuple[float, int, str, str]]:
        """Ranked recall. Accent-normalized BM25 by default; regex if requested.

        With ``use_index`` (default), builds/caches the BM25 index and rebuilds
        it lazily only when the log has grown since the last build — so new
        notes become searchable without re-tokenizing every record each call.
        """
        if not self.log_len():
            return []
        if regex:
            if use_index and self._index_stale():
                self.build_index()
            docs = self._index_docs if getattr(self, "_index_docs", None) is not None else self._all_records()
            pat = re.compile(query, re.I)
            hits = [(1.0, mid, date, text) for mid, date, text, _ in docs if pat.search(text)]
            return hits[:topk]
        if use_index and self._index_stale():
            self.build_index()
        docs = self._index_docs if use_index else self._all_records()
        if not docs:
            return []
        q = _tokenize(query)
        if not q:
            return []
        idf = self._index_idf if use_index else None
        avgdl = self._index_avgdl if use_index else None
        if not use_index:
            N = len(docs)
            df = defaultdict(int)
            for _, _, _, toks in docs:
                for t in set(toks):
                    df[t] += 1
            idf = {t: (N - df[t] + 0.5) / (df[t] + 0.5) for t in df}
            avgdl = sum(len(t) for _, _, _, t in docs) / N if N else 0
        k1, b = 1.5, 0.75
        scored = []
        for mid, date, text, toks in docs:
            dl = len(toks)
            tf = defaultdict(int)
            for t in toks:
                tf[t] += 1
            score = 0.0
            for term in set(q):
                if term not in idf:
                    continue
                f = tf.get(term, 0)
                if f == 0:
                    continue
                denom = f + k1 * (1 - b + b * dl / avgdl)
                score += idf[term] * (f * (k1 + 1)) / denom
            if score > 0:
                scored.append((score, mid, date, text))
        scored.sort(reverse=True)
        return scored[:topk]
