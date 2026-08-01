from __future__ import annotations

import os
from pathlib import Path

import pytest

from session_bridge.sidebar_placement import (
    SidebarPlacementError,
    _windows_identity,
    filesystem_path_identity,
    ordinary_windows_path_identity,
    placement_paths_equivalent,
    resolve_sidebar_placement,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("C:/Users/diego/.hermes", "c:\\users\\diego\\.hermes"),
        ("c:\\USERS\\diego\\.hermes", "c:\\users\\diego\\.hermes"),
        ("\\\\server\\share\\workspace", "\\\\server\\share\\workspace"),
        ("\\\\host.example\\public-share\\workspace", "\\\\host.example\\public-share\\workspace"),
        ("\\\\192.0.2.1\\public\\workspace", "\\\\192.0.2.1\\public\\workspace"),
        (
            "\\\\2001-db8--1.ipv6-literal.net\\public\\workspace",
            "\\\\2001-db8--1.ipv6-literal.net\\public\\workspace",
        ),
        (
            "\\\\2001-0db8-0000-0000-0000-0000-0000-0001.ipv6-literal.net\\public\\workspace",
            "\\\\2001-0db8-0000-0000-0000-0000-0000-0001.ipv6-literal.net\\public\\workspace",
        ),
        (
            "\\\\--ffff-192.0.2.1.ipv6-literal.net\\public\\workspace",
            "\\\\--ffff-192.0.2.1.ipv6-literal.net\\public\\workspace",
        ),
        ("\\\\" + "n" * 15 + "\\public\\workspace", "\\\\" + "n" * 15 + "\\public\\workspace"),
        ("\\\\" + "1" * 15 + "\\public\\workspace", "\\\\" + "1" * 15 + "\\public\\workspace"),
        ("\\\\bad_server\\public\\workspace", "\\\\bad_server\\public\\workspace"),
        ("\\\\srv[1]\\public\\workspace", "\\\\srv[1]\\public\\workspace"),
        ("\\\\srv..name\\public\\workspace", "\\\\srv..name\\public\\workspace"),
        ("\\\\host.123\\public\\workspace", "\\\\host.123\\public\\workspace"),
        ("\\\\3com.example\\public\\workspace", "\\\\3com.example\\public\\workspace"),
        (
            "\\\\servidor東京\\shareüber\\workspace",
            "\\\\servidor東京\\shareüber\\workspace",
        ),
        ("\\\\server\\CON\\workspace", "\\\\server\\con\\workspace"),
        ("\\\\server\\share.\\workspace", "\\\\server\\share.\\workspace"),
        ("\\\\server\\share \\workspace", "\\\\server\\share \\workspace"),
        ("\\\\server\\" + "s" * 80 + "\\workspace", "\\\\server\\" + "s" * 80 + "\\workspace"),
        ("\\\\server\\share\\tail東京", "\\\\server\\share\\tail東京"),
        ("\\\\server\\share\\" + "p" * 255, "\\\\server\\share\\" + "p" * 255),
        ("C:/workspace/東京/über", "c:\\workspace\\東京\\über"),
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/srv/session-inbox", "/srv/session-inbox"),
        ("/srv/Session-Inbox", "/srv/Session-Inbox"),
        ("/", "/"),
    ],
)
def test_filesystem_path_identity_uses_canonical_case_sensitive_posix_spelling(
    value: str,
    expected: str,
) -> None:
    assert filesystem_path_identity(value, platform="posix") == expected


@pytest.mark.parametrize(
    "value",
    [
        "relative/path",
        "C:/Users/diego/.hermes",
        "\\\\server\\share\\inbox",
        "/srv/./session-inbox",
        "/srv/../session-inbox",
        "/srv//session-inbox",
        "/srv/session-inbox/",
        "/srv/session\x00inbox",
    ],
)
def test_filesystem_path_identity_rejects_noncanonical_or_windows_posix_spellings(
    value: str,
) -> None:
    assert filesystem_path_identity(value, platform="posix") is None


def test_placement_paths_equivalent_is_case_sensitive_on_posix() -> None:
    assert not placement_paths_equivalent(
        "/srv/Session-Inbox",
        "/srv/session-inbox",
        platform="posix",
    )


@pytest.mark.parametrize(
    "component",
    [
        "bad*name",
        "bad?name",
        'bad"name',
        "bad<name",
        "bad>name",
        "bad|name",
        "bad\x00name",
        "bad\x07name",
        "bad\x1fname",
    ],
)
def test_ordinary_windows_path_identity_rejects_win32_invalid_components(
    component: str,
) -> None:
    assert ordinary_windows_path_identity(f"C:/workspace/{component}") is None


@pytest.mark.parametrize(
    "server",
    [
        "[2001:db8::1]",
        "fe80::1%12",
        "2001:db8::1.ipv6-literal.net",
        "2001-db8--1%12.ipv6-literal.net",
        "[2001-db8--1].ipv6-literal.net",
        "not-an-address.ipv6-literal.net",
        "999.999.999.999",
        "1" * 16,
        "bad\x00server",
        "_" * 16,
        "💥" * 4,
    ],
)
def test_ordinary_windows_path_identity_rejects_invalid_unc_server(
    server: str,
) -> None:
    assert ordinary_windows_path_identity(f"\\\\{server}\\share\\workspace") is None


@pytest.mark.parametrize(
    "share",
    [
        "bad*share",
        "bad?share",
        'bad"share',
        "bad<share",
        "bad>share",
        "bad|share",
        "bad[share",
        "bad]share",
        "bad+share",
        "bad=share",
        "bad;share",
        "bad,share",
        "bad\x00share",
        "bad\x07share",
        "bad\x1fshare",
    ],
)
def test_ordinary_windows_path_identity_rejects_invalid_unc_share(
    share: str,
) -> None:
    assert ordinary_windows_path_identity(f"\\\\server\\{share}\\workspace") is None


def test_ordinary_windows_path_identity_rejects_unc_share_over_80_characters() -> None:
    assert ordinary_windows_path_identity("\\\\server\\" + "s" * 81 + "\\workspace") is None


@pytest.mark.parametrize(
    "tail",
    [
        'bad"tail',
        "bad[tail",
        "bad]tail",
        "bad+tail",
        "bad=tail",
        "bad;tail",
        "bad,tail",
        "bad:tail",
        "bad|tail",
        "bad<tail",
        "bad>tail",
        "bad*tail",
        "bad?tail",
        "bad\x00tail",
        "bad\x1ftail",
    ],
)
def test_ordinary_windows_path_identity_rejects_invalid_unc_tail_component(
    tail: str,
) -> None:
    assert ordinary_windows_path_identity(f"\\\\server\\share\\{tail}") is None


def test_ordinary_windows_path_identity_rejects_unc_tail_over_255_characters() -> None:
    assert ordinary_windows_path_identity("\\\\server\\share\\" + "p" * 256) is None


def test_ordinary_windows_path_identity_enforces_utf8_fqdn_limits() -> None:
    valid_unicode_label = "é" * 31 + "a"
    invalid_unicode_label = "é" * 32
    valid_total = ".".join(("a" * 63, "a" * 63, "a" * 63, "a" * 61))
    invalid_total = ".".join(
        ("a" * 63, "a" * 63, "a" * 63, "a" * 62)
    )

    assert len(valid_unicode_label.encode("utf-8")) == 63
    assert len(valid_total.encode("utf-8")) == 253
    assert sum(len(label.encode("utf-8")) + 1 for label in valid_total.split(".")) + 1 == 255
    assert ordinary_windows_path_identity(
        f"\\\\{valid_unicode_label}\\share\\workspace"
    ) == f"\\\\{valid_unicode_label}\\share\\workspace"
    assert ordinary_windows_path_identity(f"\\\\{valid_total}\\share\\workspace") == (
        f"\\\\{valid_total}\\share\\workspace"
    )
    assert ordinary_windows_path_identity(
        f"\\\\{invalid_unicode_label}\\share\\workspace"
    ) is None
    assert ordinary_windows_path_identity(
        f"\\\\{invalid_total}\\share\\workspace"
    ) is None


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

    placement = resolve_sidebar_placement(str(inbox), str(inbox), 1, str(source))

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
    "path_builder",
    (
        lambda path: "\\\\?\\C:\\invalid",
        lambda path: "\\\\.\\C:\\invalid",
        lambda path: str(path) + "\\.",
        lambda path: str(path) + "\\",
        lambda path: str(path) + ".",
        lambda path: str(path) + " ",
    ),
    ids=("device_verbatim", "device_dot", "dot_segment", "trailing_separator", "trailing_dot", "trailing_space"),
)
def test_resolve_sidebar_placement_rejects_raw_noncanonical_configured_inbox(
    tmp_path: Path,
    path_builder,
) -> None:
    inbox = tmp_path / ".hermes"
    source = tmp_path / "source"
    inbox.mkdir()
    source.mkdir()

    with pytest.raises(SidebarPlacementError, match="^inbox_unavailable$"):
        resolve_sidebar_placement(
            configured_inbox_cwd=path_builder(inbox),
            hermes_home=inbox,
            placement_generation=1,
            source_cwd=str(source),
        )


@pytest.mark.parametrize(
    "path_builder",
    (
        lambda path: "\\\\?\\C:\\invalid",
        lambda path: "\\\\.\\C:\\invalid",
        lambda path: str(path) + "\\.",
        lambda path: str(path) + "\\",
        lambda path: str(path) + ".",
        lambda path: str(path) + " ",
    ),
    ids=("device_verbatim", "device_dot", "dot_segment", "trailing_separator", "trailing_dot", "trailing_space"),
)
def test_resolve_sidebar_placement_rejects_raw_noncanonical_hermes_home(
    tmp_path: Path,
    path_builder,
) -> None:
    inbox = tmp_path / ".hermes"
    source = tmp_path / "source"
    inbox.mkdir()
    source.mkdir()

    with pytest.raises(SidebarPlacementError, match="^inbox_unavailable$"):
        resolve_sidebar_placement(
            configured_inbox_cwd=str(inbox),
            hermes_home=path_builder(inbox),
            placement_generation=1,
            source_cwd=str(source),
        )


@pytest.mark.parametrize(
    "path_builder",
    (
        lambda path: "\\\\?\\C:\\invalid",
        lambda path: "\\\\.\\C:\\invalid",
        lambda path: str(path) + "\\.",
        lambda path: str(path) + "\\",
        lambda path: str(path) + ".",
        lambda path: str(path) + " ",
    ),
    ids=("device_verbatim", "device_dot", "dot_segment", "trailing_separator", "trailing_dot", "trailing_space"),
)
def test_resolve_sidebar_placement_rejects_raw_noncanonical_source(
    tmp_path: Path,
    path_builder,
) -> None:
    inbox = tmp_path / ".hermes"
    source = tmp_path / "source"
    inbox.mkdir()
    source.mkdir()

    with pytest.raises(SidebarPlacementError, match="^source_identity_mismatch$"):
        resolve_sidebar_placement(
            configured_inbox_cwd=str(inbox),
            hermes_home=inbox,
            placement_generation=1,
            source_cwd=path_builder(source),
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


@pytest.mark.parametrize("source_cwd", ("relative",))
def test_resolve_sidebar_placement_rejects_relative_source_identity(
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


def test_resolve_sidebar_placement_supports_inbox_only_authority(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / ".hermes"
    inbox.mkdir()

    placement = resolve_sidebar_placement(
        str(inbox),
        inbox,
        1,
        None,
    )

    assert placement.inbox_cwd == str(inbox.resolve())
    assert placement.runtime_workspace_roots == (str(inbox.resolve()),)


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
    equivalent_inbox = configured_inbox.upper().replace("\\", "/")
    equivalent_home = configured_inbox.upper().replace("\\", "/")
    equivalent_source = configured_inbox.upper().replace("\\", "/")

    assert equivalent_inbox != configured_inbox
    assert _windows_identity(Path(configured_inbox)) == _windows_identity(
        Path(equivalent_home)
    )

    placement = resolve_sidebar_placement(
        configured_inbox_cwd=equivalent_inbox,
        hermes_home=equivalent_home,
        placement_generation=1,
        source_cwd=equivalent_source,
    )

    assert placement.inbox_cwd == configured_inbox
    assert placement.runtime_workspace_roots == (configured_inbox,)
