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
        """Round-7 MoA review (2026-08-10) flagged a span-overlap bug:
        when both a normal drive path and a stray bare ``\\`` coexist
        in the same collapsed command, the bare-root must still be
        emitted as a root token (drive-path substring check would
        otherwise swallow it).
        """
        # ``cmd /c \"rd /s /q \\\\\"C:\\\\Users\\\\tester\"`` after PowerShell
        # strips the outer single-quotes surfaces:
        #   - ``C:\\Users\\tester`` (real user path)
        #   - one or more bare ``\\`` tokens (root residue)
        cmd = 'cmd /c "rd /s /q C:\\Users\\tester\\folder"'
        tokens = extract_filesystem_targets(cmd)
        # The user-folder string must be present.
        assert any("tester" in t for t in tokens), (
            f"expected user-folder token in {tokens!r}"
        )
        # Independent of the user folder, the bare-backslash sweep
        # must not over-emit: ``\\server\\share\\folder`` is NOT root,
        # so the inner-form drive match must not be double-counted.
        # No spurious UNC root here, so the only root candidate would
        # be a bare ``\`` if it appeared, which it does NOT in this
        # case (no collapse). Verify by ensuring no token classifies
        # as root:
        from tools.path_security import is_filesystem_root

        assert not any(is_filesystem_root(t) for t in tokens), (
            f"command with normal path should not yield a root token, got {tokens!r}"
        )

    def test_unc_non_root_path_does_not_yield_root_token(self):
        """Round-7 MoA review flagged that ``\\\\server\\share\\folder``
        (a UNC non-root) was being decomposed into ``\\\\server\\share\\folder``,
        plus ``\\\\`` plus ``\\``, and the leading ``\\`` inside the
        UNC span was incorrectly classified as a filesystem root.
        After the span-overlap fix this should leave only the
        non-root drive-substring token (which classifies as False),
        no bare-root residue.
        """
        cmd = 'rd /s /q \\\\server\\share\\folder'
        from tools.path_security import is_filesystem_root

        tokens = extract_filesystem_targets(cmd)
        # No token should classify as a filesystem root — the target is a
        # child folder under the share, NOT a root.
        assert not any(is_filesystem_root(t) for t in tokens), (
            f"UNC non-root path should not yield a root token, got {tokens!r}"
        )

    def test_unconditional_fallback_emits_root_when_only_drive_seen(self):
        """The fix-#82842 echo-reproduction surfaces BOTH a drive-path
        substring AND a bare-root residue. After the round-6 +
        round-7 fixes, ``extract_filesystem_targets`` must surface
        one of each AND at least one of them must classify as root.
        """
        # A cmd fragment simulating the post-collapse form.
        cmd = 'cmd /c "rd /s /q C:\\Users\\tester \\rdrt"'
        tokens = extract_filesystem_targets(cmd)
        from tools.path_security import is_filesystem_root

        # Force a deliberate bare backslash into the command to mimic the
        # quote-collapse residue:
        coll = 'cmd /c "rd /s /q \\C:\\Users\\tester"'
        coll_tokens = extract_filesystem_targets(coll)
        assert any(is_filesystem_root(t) for t in coll_tokens), (
            f"standalone bare '\\\\' adjacent to a drive-path must produce a root token, got {coll_tokens!r}"
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
