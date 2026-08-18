"""Render the user-systemd Kanban summary units.

Rendering is separate from enabling: this command never starts or enables the
timer. An operator may review the generated units before a later, explicitly
authorized ``systemctl --user enable --now`` action.
"""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def render_unit(
    template_name: str,
    *,
    hermes_home: Path,
    release_path: Path,
    python_path: Path,
    board: str,
) -> str:
    text = (ROOT / "systemd" / template_name).read_text(encoding="utf-8")
    values = {
        "@HERMES_HOME@": str(hermes_home.resolve()),
        "@RELEASE_PATH@": str(release_path.resolve()),
        "@PYTHON@": str(python_path.absolute()),
        "@BOARD@": board,
    }
    for token, value in values.items():
        text = text.replace(token, value)
    if "@" in text:
        raise ValueError(f"unresolved unit placeholder in {template_name}")
    return text


def write_user_units(
    hermes_home: Path,
    release_path: Path,
    python_path: Path,
    board: str,
    *,
    unit_dir: Path,
) -> list[Path]:
    if not board or any(char in board for char in "\r\n"):
        raise ValueError("board must be a non-empty single line")
    unit_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for template_name in (
        "hermes-kanban-summary.service.in",
        "hermes-kanban-summary.timer.in",
    ):
        output = unit_dir / template_name.removesuffix(".in")
        output.write_text(
            render_unit(
                template_name,
                hermes_home=hermes_home,
                release_path=release_path,
                python_path=python_path,
                board=board,
            ),
            encoding="utf-8",
        )
        outputs.append(output)
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render Hermes Kanban summary user units")
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--release-path", type=Path, required=True)
    parser.add_argument("--python", dest="python_path", type=Path, required=True)
    parser.add_argument("--board", default="default")
    parser.add_argument("--unit-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    unit_dir = args.unit_dir or (Path.home() / ".config" / "systemd" / "user")
    for path in write_user_units(
        args.hermes_home,
        args.release_path,
        args.python_path,
        args.board,
        unit_dir=unit_dir,
    ):
        print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
