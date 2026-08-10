"""Tests for ``tools.path_security`` filesystem-root detection helpers.

Regression: Hermes executed ``rd /s /q`` against the ``C:\\`` drive root
after a user-approved scoped folder deletion (issue #82842). The quote
escapes collapsed under nested bash → PowerShell → cmd and a bare
``\\`` ended up as one of the destructive command's argument tokens.

Root-target commands like ``rd /s /q C:\\``, ``rd /s /q \\``, or
``rm -rf /`` must be rejected **independent of approval state** — no
yolo flag, no session allowlist, no smart-approval verdict can let a
destructive command resolve to a filesystem root.

These helpers give the approval / detection layer a portable "is this
a filesystem root?" and "extract path-like argument tokens" pair, so
the hardline floor can guard every destructive command (cmd, PowerShell,
bash, zsh, WSL) under one rule instead of relying on per-shell regexes.
"""

import pytest

from tools.path_security import (
    extract_filesystem_targets,
    is_filesystem_root,
)


# ---------------------------------------------------------------------------
# is_filesystem_root
# ---------------------------------------------------------------------------

class TestIsFilesystemRootPosix:
    """POSIX root paths resolve to ``/``."""

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "//",
            "///",
            "/.",
            "/./",
            "/..",
            "/../",
            "/./..",
            "/../../..",
            "/.//",
            "/../..",
            "  /  ",  # surrounding whitespace tolerated
        ],
    )
    def test_posix_roots_are_filesystem_roots(self, path):
        assert is_filesystem_root(path)

    @pytest.mark.parametrize(
        "path",
        [
            "/tmp",
            "/home",
            "/etc",
            "/usr",
            "/.ssh",
            "/.config",
            "/...",  # a dir literally named "...", not root
            "/var/log",
            "/usr/local/bin",
        ],
    )
    def test_non_root_posix_paths_are_not_filesystem_roots(self, path):
        assert not is_filesystem_root(path)


class TestIsFilesystemRootWindows:
    """Windows drive roots (drive-letter or bare ``\\``) are filesystem roots."""

    @pytest.mark.parametrize(
        "path",
        [
            "C:\\",
            "c:\\",
            "C:\\",
            "C:",
            "c:",
            "H:\\",
            "Z:\\",
            "\\",
            "\\\\",  # two literal backslashes ⇒ current drive root
            "\\\\?",  # UNC device namespace opener — treated as root-ish
            "C:\\.",
            "C:\\..",
            "C:\\/",
            "  C:\\  ",
        ],
    )
    def test_windows_roots_are_filesystem_roots(self, path):
        assert is_filesystem_root(path), f"{path!r} should be filesystem root"

    @pytest.mark.parametrize(
        "path",
        [
            "C:\\Users",
            "C:\\Users\\tester",
            "C:\\tmp\\hermes-victim",
            "C:\\Program Files",
            "D:\\data",
            "C:\\Users\\tester\\Documents",
        ],
    )
    def test_windows_subdirs_are_not_filesystem_roots(self, path):
        assert not is_filesystem_root(path)


class TestIsFilesystemRootUnc:
    """UNC roots (``\\\\server\\share``) are filesystem roots."""

    @pytest.mark.parametrize(
        "path",
        [
            "\\\\server\\share",
            "\\\\server\\share\\",
            "\\\\NAS01\\backups",
            "\\\\?\\C:\\",
        ],
    )
    def test_unc_roots_are_filesystem_roots(self, path):
        assert is_filesystem_root(path), f"{path!r} should be filesystem root"

    def test_unc_deeper_path_is_not_a_filesystem_root(self):
        assert not is_filesystem_root("\\\\server\\share\\folder")


class TestIsFilesystemRootEdgeCases:
    """Defensive: empty / None / non-path inputs must not crash and must
    classify empty string as "not a root" (no path, no risk)."""

    @pytest.mark.parametrize("path", ["", " ", None, "  ", "relative/path", "no-leading-slash"])
    def test_non_absolute_paths_are_not_filesystem_roots(self, path):
        assert not is_filesystem_root(path)


# ---------------------------------------------------------------------------
# extract_filesystem_targets
# ---------------------------------------------------------------------------

class TestExtractFilesystemTargets:
    """The helper parses a destructive command string and returns every
    path-like argument token it can identify, so the approval layer can
    call ``is_filesystem_root`` on each token instead of reinventing the
    shell-tokenisation rules in every regex pattern.

    The function is intentionally permissive: a return value of
    ``["C:\\Users\\tester"]`` is fine even when the real command was
    interpreted as something else by the shell, because the caller only
    uses it to fail-closed (any suspect token triggers root-refusal).
    """

    def test_single_user_path_returns_single_target(self):
        assert extract_filesystem_targets('rd /s /q "C:\\Users\\tester\\folder"') == [
            "C:\\Users\\tester\\folder",
        ]

    def test_unquoted_user_path(self):
        assert extract_filesystem_targets("rd /s /q C:\\Users\\tester") == [
            "C:\\Users\\tester",
        ]

    def test_drive_root_token_is_returned(self):
        # The exact shell-construction from issue #82842: PowerShell
        # receives ``cmd /c "rd /s /q \""C:\Users\tester\""\""""`` and
        # the PowerShell/cmd layer collapses the escape into two
        # arguments — ``\`` and ``C:\Users\tester``.  The agent-side
        # approval layer sees the inner part (after PowerShell strips the
        # outer single quotes), so we feed the helper just that fragment
        # to assert it surfaces the real Windows target AND the
        # bare-root residue (so the root-target guard catches it).
        ps_inner = 'cmd /c "rd /s /q \\\\\\"C:\\\\Users\\\\tester\\\\\""'
        tokens = extract_filesystem_targets(ps_inner)
        # The real Windows path target must be present (defence-in-depth
        # for the user folder part).
        assert any("tester" in t for t in tokens), (
            f"expected the human-readable target to surface from the collapsed command, got {tokens!r}"
        )
        # Round-6 MoA review (2026-08-10) demanded explicit token-level
        # assertion: at least ONE returned token must classify as a
        # filesystem root, so that :func:`is_filesystem_root` cannot
        # silently fail to extract the dangerous bare ``\`` residue.
        from tools.path_security import is_filesystem_root

        root_tokens = [t for t in tokens if is_filesystem_root(t)]
        assert root_tokens, (
            f"expected at least one token classifying as a filesystem root, got {tokens!r}"
        )

    def test_quote_collapse_with_drive_path_does_not_swallow_root_token(self):
        """Round-7 / Round-8 MoA review (2026-08-10) flagged a
        span-overlap bug: when both a normal drive path and a stray
        bare ``\\`` coexist in the same collapsed command, the bare
        root residue must still be emitted as a root token, not
        silently swallowed because of a substring-membership check.

        Uses the EXACT issue #82842 echo-reproduction that the
        iterative commits closed: a ``cmd /c \"...\\\"C:\\Users\"
        \"form where PowerShell/cmd layer collapses the escaping
        into a ``\\`` and a real ``C:\\Users\\tester\\`` token. The
        span-overlap + outer-quote re-tokenize must surface the bare
        ``\\`` residue as a root-classified token alongside the user
        path token.
        """
        cmd = 'cmd /c "rd /s /q \\"C:\\Users\\tester\\""'
        tokens = extract_filesystem_targets(cmd)
        from tools.path_security import is_filesystem_root

        # The drive-path substring must be present (defence in depth
        # for the user folder part).
        assert any("tester" in t for t in tokens), (
            f"expected drive-path substring in {tokens!r}"
        )
        # Regression target: at least one token classifies as a
        # filesystem root — the bare ``\\`` residue is the dangerous
        # one, NOT the user folder.
        root_tokens = [t for t in tokens if is_filesystem_root(t)]
        assert root_tokens, (
            f"expected at least one root token to surface from the "
            f"issue #82842 echo reproduction, got {tokens!r}"
        )

    def test_unc_non_root_path_does_not_yield_root_token(self):
        r"""Round-7 MoA review flagged that ``\\server\share\folder``
        (a UNC non-root) was being decomposed into ``\\server\share\folder``,
        plus ``\\server\share`` plus ``\``, and the leading ``\`` inside the
        UNC span was incorrectly classified as a filesystem root.
        After the span-overlap fix this should leave only the
        non-root drive-substring token (which classifies as False),
        no bare-root residue.
        """
        cmd = r'rd /s /q \\server\share\folder'
        from tools.path_security import is_filesystem_root

        tokens = extract_filesystem_targets(cmd)
        # No token should classify as a filesystem root — the target is a
        # child folder under the share, NOT a root.
        assert not any(is_filesystem_root(t) for t in tokens), (
            f"UNC non-root path should not yield a root token, got {tokens!r}"
        )

    def test_outer_quoted_unc_root_is_blocked(self):
        """Round-8 MoA review flagged: ``cmd /c \"rd /s /q \\\\server\\share\"``
        (UNC root wrapped in an outer ``cmd /c "..."`` quote form) was
        silently NOT classified as a root target because the primary
        tokenizer returns a single outer token, leaving the UNC root
        inside a non-target outer span.

        After the round-8 outer-quote re-tokenize fix, this exact
        issue #82842-class failure mode must surface the bare UNC root
        as a root-classified token.
        """
        cmd = r'cmd /c "rd /s /q \\server\share"'
        tokens = extract_filesystem_targets(cmd)
        from tools.path_security import is_filesystem_root

        root_tokens = [t for t in tokens if is_filesystem_root(t)]
        assert root_tokens, (
            f"outer-quoted UNC root must produce a root token from the "
            f"issue #82842 failure class, got {tokens!r}"
        )

    def test_outer_quoted_unc_non_root_does_not_block(self):
        r"""Round-7 MoA flagged that ``cmd /c \"rd /s /q \\\\server\\share\\folder\"``
        (UNC non-root wrapped in outer quotes) was being incorrectly
        classified as a root target via the new outer-quote re-tokenize
        pass.

        After the fix, the outer-quote re-tokenize only fires on
        backslash-bearing tokens; even then, the inner UNC share target
        is correctly classified as non-root because the trailing
        ``\\folder`` segment turns it into a child path of the share,
        not the share-root itself.

        Round-9 MoA review (2026-08-10) flagged that the previous
        version of this test assigned ``cmd`` but never used it — the
        assertion only checked :func:`is_filesystem_root` directly
        instead of exercising the outer-quote re-tokenize pipeline.
        The regression now runs the FULL pipeline::

          cmd -> extract_filesystem_targets -> is_filesystem_root on each
              AND detect_hardline_command on the original.
        """
        cmd = r'cmd /c "rd /s /q \\server\share\folder"'
        from tools.approval import detect_hardline_command
        from tools.path_security import is_filesystem_root

        # Outer-quote re-tokenize must surface the inner UNC token.
        tokens = extract_filesystem_targets(cmd)
        # The extracted token must classify as non-root (the inner
        # share is followed by ``\\folder``, not the share root).
        assert not any(is_filesystem_root(t) for t in tokens), (
            f"wrapped UNC non-root must not yield a root token, got {tokens!r}"
        )
        # End-to-end hardline must NOT fire.
        is_hl, desc = detect_hardline_command(cmd)
        assert not is_hl, (
            f"wrapped UNC non-root must not be hardline-blocked, got desc={desc!r}"
        )

    def test_unconditional_fallback_emits_root_when_only_drive_seen(self):
        """When the only path-like token in the command is a drive
        root (e.g. ``C:\\``) AND an unrelated naked ``\\`` appears
        elsewhere, both survive the unconditional fallback sweep —
        no longer swallowed by the round-7 substring-skip bug.
        """
        cmd = r'cmd /c "rd /s /q C:\\ doc \\stuff"'
        tokens = extract_filesystem_targets(cmd)
        from tools.path_security import is_filesystem_root

        root_tokens = [t for t in tokens if is_filesystem_root(t)]
        assert root_tokens, (
            f"drive root adjacent to bare-backslash artifacts must "
            f"surface a root token, got {tokens!r}"
        )

    def test_posix_root_command(self):
        assert extract_filesystem_targets("rm -rf /") == ["/"]

    def test_posix_chained_root(self):
        tokens = extract_filesystem_targets("rm -rf /  etc")
        assert "/" in tokens

    def test_no_path_returns_empty(self):
        assert extract_filesystem_targets("echo hello world") == []

    def test_no_false_positive_on_flags(self):
        # /S and /Q are flags, not paths.
        assert extract_filesystem_targets("cmd /c del /s /q C:\\Users\\tester") == [
            "C:\\Users\\tester",
        ]

    def test_empty_command(self):
        assert extract_filesystem_targets("") == []
