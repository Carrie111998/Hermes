#!/usr/bin/env python3
"""Standalone memstore seeding + dreams driver.

A thin, dependency-light wrapper around ``agent.memstore_seeding`` for use
outside the ``hermes`` CLI — e.g. in CI, a setup script, or a cron job that
keeps a fresh agent profile primed.

Examples
--------
    # Dry-run: see what would be seeded from the bundled examples.
    python scripts/seed_memstore.py \
        --persona examples/memstore-seed/persona.md \
        --transcript examples/memstore-seed/transcript.json \
        --dry-run

    # Export the consolidated corpus to JSONL without touching any provider.
    python scripts/seed_memstore.py \
        --persona examples/memstore-seed/persona.md \
        --export /tmp/seed.jsonl

    # Seed the active provider (reads memory.provider from config.yaml).
    python scripts/seed_memstore.py --persona about-me.md

The script writes through the same provider-agnostic ``MemoryManager`` path
the ``hermes memory seed`` command uses, so whatever provider is active in
``config.yaml`` receives the facts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from a checkout without installation.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent.memstore_seeding import (  # noqa: E402
    DreamConsolidator,
    MemstoreSeeder,
    build_corpus_from_sources,
    seed_and_dream,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seed_memstore",
        description="Seed and consolidate agent memstores from persona docs and transcripts.",
    )
    p.add_argument("--persona", action="append", default=[], metavar="FILE",
                   help="Markdown persona/'about' doc (repeatable)")
    p.add_argument("--transcript", action="append", default=[], metavar="FILE",
                   help="Conversation transcript JSON/JSONL (repeatable)")
    p.add_argument("--no-dream", action="store_true",
                   help="Skip the consolidation pass")
    p.add_argument("--dry-run", action="store_true",
                   help="Report without writing to any provider")
    p.add_argument("--export", metavar="FILE",
                   help="Write the (consolidated) corpus to JSONL instead of seeding")
    p.add_argument("--min-trust", type=float, default=0.25,
                   help="Prune facts below this trust during dreams (default: 0.25)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.persona and not args.transcript:
        print("error: provide at least one --persona or --transcript source", file=sys.stderr)
        return 2

    corpus = build_corpus_from_sources(
        persona_paths=args.persona, transcript_paths=args.transcript
    )
    if not corpus:
        print("No memory-worthy facts found in the provided sources.")
        return 0

    consolidator = None if args.no_dream else DreamConsolidator(min_trust=args.min_trust)
    if not args.no_dream:
        corpus, dream_report = consolidator.consolidate(corpus)
        print(f"Dreams: {dream_report.summary()}")

    print(f"Corpus: {len(corpus)} fact(s)")

    if args.export:
        Path(args.export).write_text(corpus.to_jsonl(), encoding="utf-8")
        print(f"Wrote corpus to {args.export}")
        return 0

    # Build the active provider sink (lazy import — only needed when writing).
    from agent.memory_manager import MemoryManager
    from plugins.memory import _get_active_memory_provider, load_memory_provider

    manager = MemoryManager()
    provider_name = _get_active_memory_provider() or ""
    if provider_name and not args.dry_run:
        provider = load_memory_provider(provider_name)
        if provider is not None and provider.is_available():
            manager.add_provider(provider)
            manager.initialize_all(session_id="seed-script", platform="cli")

    report = MemstoreSeeder(manager).seed(corpus, dry_run=args.dry_run)
    print(f"Target provider: {provider_name or '(built-in only)'}")
    print(report.summary())
    if report.errors:
        print(f"{len(report.errors)} error(s); first: {report.errors[0]}", file=sys.stderr)
    try:
        manager.shutdown_all()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
