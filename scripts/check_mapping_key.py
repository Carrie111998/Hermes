#!/usr/bin/env python3
"""Check membership in a statically declared Python mapping."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _static_string(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_string(node.left) + _static_string(node.right)
    raise ValueError("mapping key is not a static string expression")


def mapping_keys(source: Path, assignment: str) -> set[str]:
    """Parse keys from one dict assignment without importing the source."""
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ValueError(f"cannot parse mapping source: {exc}") from exc

    values: list[ast.expr] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == assignment
            for target in statement.targets
        ):
            values.append(statement.value)
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == assignment
            and statement.value is not None
        ):
            values.append(statement.value)

    if len(values) != 1:
        raise ValueError(
            f"expected exactly one assignment to {assignment!r}, found {len(values)}"
        )
    mapping = values[0]
    if not isinstance(mapping, ast.Dict):
        raise ValueError(f"assignment {assignment!r} is not a dict literal")
    if any(key is None for key in mapping.keys):
        raise ValueError("mapping unpacking is not supported")
    return {_static_string(key) for key in mapping.keys if key is not None}


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            f"usage: {argv[0]} <python-source> <assignment> <key>",
            file=sys.stderr,
        )
        return 2
    try:
        keys = mapping_keys(Path(argv[1]), argv[2])
    except ValueError as exc:
        print(f"mapping check failed closed: {exc}", file=sys.stderr)
        return 2
    return 0 if argv[3] in keys else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
