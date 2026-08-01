#!/usr/bin/env python3
"""Secret-free CLI for promoting one exact dormant rotation stager."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from scripts.canary import production_release_candidate_promoter as promoter


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision")
    parser.add_argument("terminal_receipt_sha256")
    arguments = parser.parse_args(argv)
    try:
        result = promoter.promote_rotation_stager_candidate(
            revision=arguments.revision,
            expected_builder_terminal_receipt_sha256=(
                arguments.terminal_receipt_sha256
            ),
        )
    except (OSError, promoter.ProductionReleaseCandidatePromoterError):
        print(
            '{"error_code":"rotation_stager_promotion_failed","ok":false}',
            file=sys.stderr,
        )
        return 2
    print(_canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
