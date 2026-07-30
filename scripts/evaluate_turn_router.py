#!/usr/bin/env python3
"""Write an observe-only native turn-router evaluation report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.turn_router_eval import evaluate_turn_router, load_eval_corpus


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--classifier-corpus", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    report = evaluate_turn_router(
        load_eval_corpus(args.corpus),
        classifier_corpus=(
            load_eval_corpus(args.classifier_corpus)
            if args.classifier_corpus is not None
            else None
        ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
