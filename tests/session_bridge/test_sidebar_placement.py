from __future__ import annotations

import os
from pathlib import Path

import pytest

from session_bridge.sidebar_placement import (
    SidebarPlacementError,
    _windows_identity,
    ordinary_windows_path_identity,
    resolve_sidebar_placement,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("C:/Users/diego/.hermes", "c:\\users\\diego\\.hermes"),
        ("c:\\USERS\\diego\\.hermes", "c:\\users\\diego\\.hermes"),
        ("\\\\server\\share\\workspace", "\\\\server\\share\\workspace"),
    ],
)
def test_ordinary_windows_path_identity_normalizes_ordinary_filesystem_paths(
    value: str,
    expected: str,
) -> None:
    assert ordinary_windows_path_identity(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "relative/path",
        "\\Users\\diego\\.hermes",
        "C:Users\\diego\\.hermes",
        "C:/Users/diego/../diego/.hermes",
        "C:/Users/diego/.hermes/.",
        "\\\\?\\C:\\Users\\diego\\.hermes",
        "\\\\.\\pipe\\session-inbox",
        "\\\\server\\pipe\\session-inbox",
        "\\\\server\\mailslot\\session-inbox",
        "\\\\server\\IPC$\\session-inbox",
        "C:/con",
        "C:/workspace/trailing.",
        "C:/workspace/trailing ",
        "C:/workspace/name:stream",
    ],
)
def test_ordinary_windows_path_identity_rejects_nonfilesystem_spellings(
    value: str,
) -> None:
    assert ordinary_windows_path_identity(value) is None


def test_resolve_sidebar_placement_keeps_source_out_of_identity(tmp_path: Path) -> None:
    inbox = tmp_path / ".hermes"
    source = tmp_path / "repo" / ".claude" / "worktrees" / "task"
    inbox.mkdir()
    source.mkdir(parents=True)

    placement = resolve_sidebar_placement(
        configured_inbox_cwd=str(inbox),
        hermes_home=inbox,
        placement_generation=1,
        source_cwd=str(source),
    )

    assert placement.inbox_cwd == str(inbox.resolve())
    assert placement.local_host == "local"
    assert placement.placement_generation == 1
    assert placement.runtime_workspace_roots == (
        str(inbox.resolve()),
        str(source.resolve()),
    )


def test_resolve_sidebar_placement_accepts_stated_positional_signature(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / ".hermes"
    source = tmp_path / "source"
    inbox.mkdir()
    source.mkdir()

    placement = resolve_sidebar_placement(str(inbox), inbox, 1, str(source))

    assert placement.runtime_workspace_roots == (
        str(inbox.resolve()),
        str(source.resolve()),
    )


def test_resolve_sidebar_placement_deduplicates_identical_roots(tmp_path: Path) -> None:
    inbox = tmp_path / ".hermes"
    inbox.mkdir()

    placement = resolve_sidebar_placement(
        configured_inbox_cwd=str(inbox),
        hermes_home=inbox,
        placement_generation=1,
        source_cwd=str(inbox),
    )

    assert placement.runtime_workspace_roots == (str(inbox.resolve()),)


def test_resolve_sidebar_placement_rejects_invalid_hermes_home_type(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / ".hermes"
    source = tmp_path / "source"
    inbox.mkdir()
    source.mkdir()

    with pytest.raises(SidebarPlacementError, match="^inbox_unavailable$"):
        resolve_sidebar_placement(
            str(inbox), None, 1, str(source)  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "configured_inbox",
    (
        lambda tmp_path, inbox: ".hermes",
        lambda tmp_path, inbox: str(tmp_path / "missing"),
        lambda tmp_path, inbox: str(tmp_path / "file"),
        lambda tmp_path, inbox: str(inbox) + "\\.",
        lambda tmp_path, inbox: str(tmp_path / "different"),
    ),
)
def test_resolve_sidebar_placement_rejects_unsafe_inbox_paths(
    tmp_path: Path,
    configured_inbox: object,
) -> None:
    inbox = tmp_path / ".hermes"
    source = tmp_path / "source"
    inbox.mkdir()
    source.mkdir()
    (tmp_path / "file").write_text("not a directory", encoding="utf-8")
    (tmp_path / "different").mkdir()

    with pytest.raises(SidebarPlacementError, match="^inbox_unavailable$"):
        resolve_sidebar_placement(
            configured_inbox_cwd=configured_inbox(tmp_path, inbox),  # type: ignore[operator]
            hermes_home=inbox,
            placement_generation=1,
            source_cwd=str(source),
        )


@pytest.mark.parametrize("source_cwd", ("relative", None))
def test_resolve_sidebar_placement_rejects_missing_or_relative_source_identity(
    tmp_path: Path,
    source_cwd: str | None,
) -> None:
    inbox = tmp_path / ".hermes"
    inbox.mkdir()

    with pytest.raises(SidebarPlacementError, match="^source_identity_mismatch$"):
        resolve_sidebar_placement(
            configured_inbox_cwd=str(inbox),
            hermes_home=inbox,
            placement_generation=1,
            source_cwd=source_cwd,
        )


def test_resolve_sidebar_placement_rejects_missing_source_identity(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / ".hermes"
    inbox.mkdir()

    with pytest.raises(SidebarPlacementError, match="^source_identity_mismatch$"):
        resolve_sidebar_placement(
            configured_inbox_cwd=str(inbox),
            hermes_home=inbox,
            placement_generation=1,
            source_cwd=str(tmp_path / "missing"),
        )


@pytest.mark.parametrize("placement_generation", (True, False, 0, 2))
def test_resolve_sidebar_placement_rejects_any_generation_except_integer_one(
    tmp_path: Path,
    placement_generation: object,
) -> None:
    inbox = tmp_path / ".hermes"
    source = tmp_path / "source"
    inbox.mkdir()
    source.mkdir()

    with pytest.raises(SidebarPlacementError, match="^source_identity_mismatch$"):
        resolve_sidebar_placement(
            configured_inbox_cwd=str(inbox),
            hermes_home=inbox,
            placement_generation=placement_generation,  # type: ignore[arg-type]
            source_cwd=str(source),
        )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path equivalence")
def test_resolve_sidebar_placement_normalizes_windows_equivalents_and_deduplicates(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / ".hermes"
    inbox.mkdir()
    configured_inbox = str(inbox.resolve())
    equivalent_home = configured_inbox.upper().replace("\\", "/")
    equivalent_source = configured_inbox.upper().replace("\\", "/")

    assert equivalent_home != configured_inbox
    assert _windows_identity(Path(configured_inbox)) == _windows_identity(
        Path(equivalent_home)
    )

    placement = resolve_sidebar_placement(
        configured_inbox_cwd=configured_inbox,
        hermes_home=equivalent_home,
        placement_generation=1,
        source_cwd=equivalent_source,
    )

    assert placement.inbox_cwd == configured_inbox
    assert placement.runtime_workspace_roots == (configured_inbox,)
