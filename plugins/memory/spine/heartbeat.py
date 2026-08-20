#!/usr/bin/env python3
"""Spine heartbeat — catches the failures that degrade silently.

Every check below is an incident that already happened, not a hypothetical:

  embedder    Semantic recall was dead for 8 days after a sentence-transformers
              method rename. It failed gracefully, fell back to keyword-only,
              and said so in its own output. Nobody read that output.
  vectors     A rebuild run under the wrong interpreter wiped every observation
              vector, printed a warning, and exited 0.
  hotcore     MEMORY.md grew unchecked to 86KB against a 90KB cap. Over the cap
              the memory tool rejects writes, so Hermes keeps its old memories
              and quietly stops forming new ones.
  sync        The Claude Code profile is a derived copy. If the sync stops
              running, recall keeps answering confidently from a stale mirror.
  divergence  Observation `status` lives only in the DB and is never written
              back to the canonical JSONL, so a rebuild silently reverts
              demotions and re-inflates the hot core.
  consolidate The nightly consolidation ran for weeks doing nothing at all.
  eval        A regression nobody notices is the whole point of this file.

DESIGN RULE, from Chin's own alerting principle: never fire on missing data.
A check that cannot get its evidence returns SKIP, not FAIL. Silent when blind.
And the script is silent when everything is healthy, because a heartbeat that
reports every day is one you stop reading.

Exit 0 = healthy or skipped. Exit 1 = something needs attention.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
import time

OK, FAIL, SKIP = "OK", "FAIL", "SKIP"

MEM_MD = os.path.expanduser("~/.hermes/memories/MEMORY.md")
CLAUDE_MEM_DIR = os.path.expanduser("~/.claude/projects/-Users-0xsteamboat/memory")
CC_PROFILE = "agent:claude-code"

HOTCORE_WARN_BYTES = 20000        # spine's own demote trigger
SYNC_STALE_HOURS = 12             # cron runs every 4h; 3 misses is a real fault
CONSOLIDATE_STALE_HOURS = 48      # cron runs daily
EVAL_REGRESSION_TOLERANCE = 0     # any drop below baseline is a regression


def check_embedder(cfg):
    from spine import embedder
    if not embedder.embedder_available():
        return FAIL, "embedder unavailable — recall has silently dropped to keyword-only"
    dim = embedder.get_embedding_dim()
    con = sqlite3.connect(cfg.db)
    con.execute("PRAGMA query_only = ON")
    row = con.execute("SELECT value FROM dim_meta WHERE key='embedding_dim'").fetchone()
    con.close()
    if not row:
        return SKIP, "no embedding_dim recorded, nothing to compare against"
    if int(row[0]) != dim:
        return FAIL, f"embedder returns dim {dim} but the index was built at {row[0]}"
    return OK, f"loads, dim {dim}"


def check_vectors(cfg):
    con = sqlite3.connect(cfg.db)
    con.execute("PRAGMA query_only = ON")
    total = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    novec = con.execute(
        "SELECT COUNT(*) FROM observations WHERE embedding IS NULL").fetchone()[0]
    wiki_novec = con.execute(
        "SELECT COUNT(*) FROM wiki_chunks WHERE embedding IS NULL").fetchone()[0]
    con.close()
    if not total:
        return SKIP, "no observations indexed"
    if novec or wiki_novec:
        return FAIL, (f"{novec}/{total} observations and {wiki_novec} wiki chunks have no "
                      f"vector — a rebuild probably ran without the embedder")
    return OK, f"{total} observations, all vectorised"


def check_hotcore(cfg):
    if not os.path.exists(MEM_MD):
        return SKIP, "MEMORY.md not found"
    size = os.path.getsize(MEM_MD)
    if size > HOTCORE_WARN_BYTES:
        return FAIL, (f"MEMORY.md is {size:,} bytes, over the {HOTCORE_WARN_BYTES:,} budget "
                      f"— roughly {size // 4:,} tokens on every Hermes call")
    return OK, f"{size:,} bytes ({size * 100 // HOTCORE_WARN_BYTES}% of budget)"


def check_sync(cfg):
    if not os.path.isdir(CLAUDE_MEM_DIR):
        return SKIP, "Claude Code memory dir not reachable"
    n_files = len([f for f in os.listdir(CLAUDE_MEM_DIR)
                   if f.endswith(".md") and f != "MEMORY.md"])
    jsonl = os.path.join(os.path.expanduser(cfg.canonical_root), "observations",
                         f"{CC_PROFILE}.jsonl")
    if not os.path.exists(jsonl):
        return FAIL, f"{CC_PROFILE} has never been synced"
    age_h = (time.time() - os.path.getmtime(jsonl)) / 3600
    con = sqlite3.connect(cfg.db)
    con.execute("PRAGMA query_only = ON")
    n_rows = con.execute("SELECT COUNT(*) FROM observations WHERE profile=?",
                         (CC_PROFILE,)).fetchone()[0]
    con.close()
    if n_rows != n_files:
        return FAIL, (f"{n_files} memory files but {n_rows} indexed rows — "
                      f"the sync is not keeping up")
    if age_h > SYNC_STALE_HOURS:
        return FAIL, f"last sync was {age_h:.0f}h ago, expected within {SYNC_STALE_HOURS}h"
    return OK, f"{n_rows} files mirrored, last sync {age_h:.1f}h ago"


def check_divergence(cfg):
    """DB vs canonical JSONL status drift — the live rebuild trap."""
    obs_dir = os.path.join(os.path.expanduser(cfg.canonical_root), "observations")
    if not os.path.isdir(obs_dir):
        return SKIP, "canonical store not reachable"
    on_disk = {}
    for path in glob.glob(os.path.join(obs_dir, "*.jsonl")):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                d = json.loads(line)
                on_disk[d["id"]] = d.get("status")
    if not on_disk:
        return SKIP, "canonical store is empty"
    con = sqlite3.connect(cfg.db)
    con.execute("PRAGMA query_only = ON")
    diff = sum(1 for i, s in con.execute("SELECT id, status FROM observations")
               if on_disk.get(i) not in (None, s))
    con.close()
    if diff:
        # Not a failure in itself: this is the expected steady state until status
        # write-back is implemented. It is reported so the number stays visible
        # and a rebuild is never run by accident.
        return OK, (f"{diff} rows differ between DB and JSONL — expected, but "
                    f"rebuild-index.py would revert them; do not run it")
    return OK, "DB and canonical store agree"


def check_consolidation(cfg):
    reports = sorted(glob.glob(os.path.join(
        os.path.dirname(os.path.expanduser(cfg.canonical_root)),
        "_system", "consolidation-*.json")))
    if not reports:
        return SKIP, "no consolidation reports found"
    age_h = (time.time() - os.path.getmtime(reports[-1])) / 3600
    if age_h > CONSOLIDATE_STALE_HOURS:
        return FAIL, f"last consolidation was {age_h / 24:.1f} days ago"
    try:
        rep = json.load(open(reports[-1], encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return SKIP, "latest consolidation report unreadable"
    warns = rep.get("warnings") or []
    if warns:
        return FAIL, "consolidation reported: " + "; ".join(warns)
    return OK, f"ran {age_h:.0f}h ago, no warnings"


def check_eval(cfg):
    here = os.path.dirname(os.path.abspath(__file__))
    baselines = sorted(glob.glob(os.path.join(here, "eval_baseline_*.json")))
    if not baselines:
        return SKIP, "no eval baseline saved"
    try:
        base = json.load(open(baselines[-1], encoding="utf-8"))
        sys.path.insert(0, here)
        import eval_run
        now = eval_run.run("*")
    except Exception as e:  # noqa: BLE001 — a broken gate must not mask the others
        return SKIP, f"eval could not run: {type(e).__name__}"
    out = []
    for hop in ("single", "multi"):
        b = base.get("by_hop", {}).get(hop, {}).get("passed")
        n = now.get("by_hop", {}).get(hop, {}).get("passed")
        tot = now.get("by_hop", {}).get(hop, {}).get("total")
        if b is None:
            continue
        out.append(f"{hop} {n}/{tot} (was {b})")
        if n < b - EVAL_REGRESSION_TOLERANCE:
            return FAIL, f"{hop}-hop recall regressed: {n} vs baseline {b}"
    return OK, ", ".join(out) if out else "baseline has no sections to compare"


def check_hotcore_coverage(cfg):
    """Every hot-core block must exist in spine, or trimming it destroys it.

    MEMORY.md has a second writer: Hermes's memory() tool writes straight into
    the file and never touches spine. 27 blocks were in that state when this
    check was written. A cleanup that assumed "these are in spine already"
    would have deleted all of them.
    """
    from spine import coverage
    if not os.path.exists(MEM_MD):
        return SKIP, "MEMORY.md not found"
    con = sqlite3.connect(cfg.db)
    con.execute("PRAGMA query_only = ON")
    try:
        total = len(coverage.hotcore_blocks(MEM_MD))
        uncovered = coverage.uncovered_hotcore(MEM_MD, con)
    finally:
        con.close()
    if not total:
        return SKIP, "hot core is empty"
    if uncovered:
        return FAIL, (f"{len(uncovered)}/{total} hot-core blocks are not retrievable from "
                      f"spine — run sync_hotcore.py before trimming MEMORY.md")
    return OK, f"all {total} hot-core blocks retrievable from spine"


CHECKS = [
    ("embedder", check_embedder),
    ("vectors", check_vectors),
    ("hotcore", check_hotcore),
    ("hotcore_coverage", check_hotcore_coverage),
    ("sync", check_sync),
    ("divergence", check_divergence),
    ("consolidation", check_consolidation),
    ("eval", check_eval),
]


def main() -> None:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from spine.config import load_spine_config
    cfg = load_spine_config()

    results = []
    for name, fn in CHECKS:
        try:
            status, msg = fn(cfg)
        except Exception as e:  # noqa: BLE001
            # A check that crashes is itself a fault worth reporting. Never let
            # one broken check hide the other six.
            status, msg = FAIL, f"check crashed: {type(e).__name__}: {e}"
        results.append((name, status, msg))

    failures = [r for r in results if r[1] == FAIL]

    if failures or verbose:
        print("🫀 **Spine heartbeat**\n")
        for name, status, msg in results:
            icon = {"OK": "✅", "FAIL": "🔴", "SKIP": "⚪"}[status]
            print(f"{icon} `{name}` — {msg}")
        if failures:
            print(f"\n**{len(failures)} check(s) need attention.**")
    # Silent on a clean run: a daily all-clear is a message you stop reading.

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
