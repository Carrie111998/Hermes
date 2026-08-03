import ast
import warnings
from pathlib import Path


def _collect_shadowed_defs(body, *, scope, rel_path, duplicates):
    seen: dict[str, list[int]] = {}
    for node in body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name == "_":
            continue
        seen.setdefault(node.name, []).append(node.lineno)

    for name, lines in sorted(seen.items()):
        if len(lines) > 1:
            duplicates.append((rel_path, scope or "<module>", name, lines))

    for node in body:
        if isinstance(node, ast.ClassDef):
            child_scope = f"{scope}.{node.name}" if scope else node.name
            _collect_shadowed_defs(
                node.body,
                scope=child_scope,
                rel_path=rel_path,
                duplicates=duplicates,
            )


def test_no_shadowed_duplicate_definitions_in_tests():
    tests_root = Path(__file__).resolve().parent
    duplicates: list[tuple[str, str, str, list[int]]] = []

    for path in sorted(tests_root.rglob("*.py")):
        rel_path = path.relative_to(tests_root.parent).as_posix()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _collect_shadowed_defs(
            tree.body,
            scope="",
            rel_path=rel_path,
            duplicates=duplicates,
        )

    assert not duplicates, "Shadowed duplicate definitions found:\n" + "\n".join(
        f"- {rel_path} [{scope}] `{name}` at lines {', '.join(map(str, lines))}"
        for rel_path, scope, name, lines in duplicates
    )
