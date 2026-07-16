#!/usr/bin/env python3
"""Standalone canonical-memstore seeding + dreams driver.

A thin, dependency-light wrapper around ``agent.memstore_seeding`` and
``agent.memstore_files`` for use outside the ``hermes`` CLI — CI, a setup
script, or a cron job that keeps an agent profile primed.

The canonical markdown file tree is the store of record (USER.md, AGENTS.md,
IDENTITY.md, …, plus memories/daily/*.md), so it is fully provider-agnostic —
any framework can read the files. When an external provider is configured it
is mirrored as a bonus.

Examples
--------
    # Seed the file tree under a scratch HERMES_HOME from the bundled samples:
    python scripts/seed_memstore.py \
        --persona docs/memstore-seed/persona.md \
        --transcript docs/memstore-seed/transcript.json \
        --home /tmp/hermes-home

    # Dry-run: report the extraction without writing files:
    python scripts/seed_memstore.py --persona docs/memstore-seed/persona.md --dry-run

    # Roll up the daily tree into MEMORY.md / USER.md:
    python scripts/seed_memstore.py --home /tmp/hermes-home --rollup
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from a checkout without installation.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent.memstore_files import CanonicalMemstore  # noqa: E402
from agent.memstore_seeding import (  # noqa: E402
    DreamConsolidator,
    MemstoreSeeder,
    build_corpus_from_sources,
    load_transcript,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seed_memstore",
        description="Seed and consolidate the canonical markdown memstore.",
    )
    p.add_argument("--persona", action="append", default=[], metavar="FILE",
                   help="Markdown persona/'about' doc (repeatable)")
    p.add_argument("--transcript", action="append", default=[], metavar="FILE",
                   help="Conversation transcript JSON/JSONL (repeatable)")
    p.add_argument("--home", metavar="DIR",
                   help="Memstore root (default: HERMES_HOME)")
    p.add_argument("--date", metavar="YYYY-MM-DD",
                   help="Fallback date for undated transcript messages")
    p.add_argument("--rollup", action="store_true",
                   help="Roll up the daily tree into MEMORY.md/USER.md and exit")
    p.add_argument("--no-dream", action="store_true", help="Skip consolidation")
    p.add_argument("--no-daily", action="store_true", help="Skip per-day digests")
    p.add_argument("--mirror", action="store_true",
                   help="Also mirror facts into the active external provider")
    p.add_argument("--dry-run", action="store_true",
                   help="Report without writing anything")
    p.add_argument("--min-trust", type=float, default=0.25,
                   help="Prune facts below this trust during dreams (default: 0.25)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store = CanonicalMemstore(root=args.home or None)

    # Roll-up mode.
    if args.rollup:
        report = store.roll_up(consolidator=DreamConsolidator(min_trust=args.min_trust))
        if report.days_read == 0:
            print(f"No daily digests under {store.daily}")
            return 0
        print(f"Roll-up: {report.summary()}")
        return 0

    if not args.persona and not args.transcript:
        print("error: provide --persona/--transcript, or --rollup", file=sys.stderr)
        return 2

    corpus = build_corpus_from_sources(
        persona_paths=args.persona, transcript_paths=args.transcript
    )
    if not corpus:
        print("No memory-worthy facts found in the provided sources.")
        return 0

    if not args.no_dream:
        corpus, dream_report = DreamConsolidator(min_trust=args.min_trust).consolidate(corpus)
        print(f"Dreams: {dream_report.summary()}")
    print(f"Corpus: {len(corpus)} fact(s)")

    if args.dry_run:
        by_cat: dict[str, int] = {}
        for f in corpus:
            by_cat[f.category] = by_cat.get(f.category, 0) + 1
        print("Dry-run — by category:", ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items())))
        return 0

    # 1. Canonical file tree.
    print(f"Files: {store.seed_facts(corpus).summary()}")

    # 2. Per-day digests.
    if not args.no_daily and args.transcript:
        msgs = []
        for path in args.transcript:
            try:
                msgs.extend(load_transcript(path))
            except OSError:
                pass
        if msgs:
            written = store.write_daily_digests(msgs, default_date=args.date)
            print(f"Daily: {len(written)} digest(s) → {', '.join(sorted(written))}")

    # 3. Optional provider mirror.
    if args.mirror:
        from agent.memory_manager import MemoryManager
        from plugins.memory import _get_active_memory_provider, load_memory_provider

        manager = MemoryManager()
        name = _get_active_memory_provider() or ""
        if name:
            provider = load_memory_provider(name)
            if provider is not None and provider.is_available():
                manager.add_provider(provider)
                manager.initialize_all(session_id="seed-script", platform="cli")
                print(f"Provider '{name}': {MemstoreSeeder(manager).seed(corpus).summary()}")
                try:
                    manager.shutdown_all()
                except Exception:
                    pass
        else:
            print("Provider: none active")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
