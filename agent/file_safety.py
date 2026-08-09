"""Shared file safety rules used by both tools and ACP shims."""

from __future__ import annotations

import math
import os
import shutil
import stat
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import tarfile


def _hermes_home_path() -> Path:
    """Resolve the active HERMES_HOME (profile-aware) without circular imports."""
    try:
        from hermes_constants import get_hermes_home  # local import to avoid cycles
        return get_hermes_home()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def _hermes_root_path() -> Path:
    """Resolve the Hermes root dir (always the parent of any profile, never per-profile)."""
    try:
        from hermes_constants import get_default_hermes_root  # local import to avoid cycles
        return get_default_hermes_root()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def build_write_denied_paths(home: str) -> set[str]:
    """Return exact sensitive paths that must never be written."""
    hermes_home = _hermes_home_path()
    hermes_root = _hermes_root_path()
    return {
        os.path.realpath(p)
        for p in [
            os.path.join(home, ".ssh", "authorized_keys"),
            os.path.join(home, ".ssh", "id_rsa"),
            os.path.join(home, ".ssh", "id_ed25519"),
            os.path.join(home, ".ssh", "config"),
            # Active profile .env (or top-level .env when not in profile mode).
            str(hermes_home / ".env"),
            # Top-level .env, even when running under a profile — overwriting it
            # leaks credentials across every profile that inherits from root (#15981).
            str(hermes_root / ".env"),
            # Active profile Anthropic PKCE credential store.
            str(hermes_home / ".anthropic_oauth.json"),
            # Top-level Anthropic PKCE credential store remains sensitive even
            # when a profile is active; default/non-profile sessions still read it.
            str(hermes_root / ".anthropic_oauth.json"),
            # Bitwarden Secrets Manager encrypted disk cache.
            str(hermes_home / "cache" / "bws_cache.enc.json"),
            str(hermes_root / "cache" / "bws_cache.enc.json"),
            os.path.join(home, ".netrc"),
            os.path.join(home, ".pgpass"),
            os.path.join(home, ".npmrc"),
            os.path.join(home, ".pypirc"),
            os.path.join(home, ".git-credentials"),
            "/etc/sudoers",
            "/etc/passwd",
            "/etc/shadow",
        ]
    }


def build_write_denied_prefixes(home: str) -> list[str]:
    """Return sensitive directory prefixes that must never be written."""
    return [
        os.path.realpath(p) + os.sep
        for p in [
            os.path.join(home, ".ssh"),
            os.path.join(home, ".aws"),
            os.path.join(home, ".gnupg"),
            os.path.join(home, ".kube"),
            "/etc/sudoers.d",
            "/etc/systemd",
            os.path.join(home, ".docker"),
            os.path.join(home, ".azure"),
            os.path.join(home, ".config", "gh"),
            os.path.join(home, ".config", "gcloud"),
        ]
    ]


def get_safe_write_roots() -> set[str]:
    """Return resolved HERMES_WRITE_SAFE_ROOT paths. Supports multiple directories
    separated by ``os.pathsep`` (``:`` on Unix, ``;`` on Windows).
    E.g., ``/opt/data:/var/www/html`` on Unix, ``C:\\data;D:\\www`` on Windows."""
    env = os.getenv("HERMES_WRITE_SAFE_ROOT", "")
    if not env:
        return set()
    roots: set[str] = set()
    for path in env.split(os.pathsep):
        if path:
            try:
                resolved = os.path.realpath(os.path.expanduser(path))
                roots.add(resolved)
            except (OSError, ValueError):
                continue
    return roots


def _classify_write_denial(path: str) -> Optional[str]:
    """Return ``'credential'``, ``'safe_root'``, or ``None`` if writes are allowed."""
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(str(path)))

    if resolved in build_write_denied_paths(home):
        return "credential"
    for prefix in build_write_denied_prefixes(home):
        if resolved.startswith(prefix):
            return "credential"

    mcp_tokens_dir_name = "mcp-tokens"

    hermes_dirs = []
    for base in (_hermes_home_path(), _hermes_root_path()):
        try:
            real = os.path.realpath(base)
            if real not in hermes_dirs:
                hermes_dirs.append(real)
        except Exception:
            continue

    for base_real in hermes_dirs:
        # Session transcripts are application-owned state.  Letting the agent's
        # generic file tools rewrite state.db or legacy JSON snapshots can
        # falsify conversation history and invalidate resume/compression state.
        try:
            if resolved == os.path.realpath(os.path.join(base_real, "state.db")):
                return True
            sessions_real = os.path.realpath(os.path.join(base_real, "sessions"))
            if resolved == sessions_real or resolved.startswith(sessions_real + os.sep):
                return True
        except Exception:
            pass
        try:
            mcp_real = os.path.realpath(os.path.join(base_real, mcp_tokens_dir_name))
            if resolved == mcp_real or resolved.startswith(mcp_real + os.sep):
                return "credential"
        except Exception:
            pass
        try:
            pairing_real = os.path.realpath(os.path.join(base_real, "pairing"))
            if resolved == pairing_real or resolved.startswith(pairing_real + os.sep):
                return "credential"
        except Exception:
            pass

    safe_roots = get_safe_write_roots()
    if safe_roots:
        allowed = False
        for safe_root in safe_roots:
            if resolved == safe_root or resolved.startswith(safe_root + os.sep):
                allowed = True
                break
        if not allowed:
            return "safe_root"

    return None


def is_write_denied(path: str) -> bool:
    """Return True if path is blocked by the write denylist or safe root."""
    return _classify_write_denial(path) is not None


def get_write_denied_error(path: str, *, verb: str = "Write") -> Optional[str]:
    """Return a user/model-facing error when writes to ``path`` are blocked."""
    denial = _classify_write_denial(path)
    if denial is None:
        return None
    if denial == "safe_root":
        roots_display = os.pathsep.join(sorted(get_safe_write_roots()))
        return (
            f"{verb} denied: '{path}' is outside HERMES_WRITE_SAFE_ROOT "
            f"({roots_display}). Unset the variable or add this path's directory prefix."
        )
    return f"{verb} denied: '{path}' is a protected system/credential file."


# Common secret-bearing project-local environment file basenames.
# These are blocked because .env files routinely contain API keys,
# database passwords, and other credentials.
_BLOCKED_PROJECT_ENV_BASENAMES: set[str] = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    ".env.staging",
    ".envrc",
}


def get_read_block_error(path: str) -> Optional[str]:
    """Return an error message when a read targets a denied Hermes path.

    Three categories are blocked:

      * Internal Hermes cache files under ``HERMES_HOME/skills/.hub`` —
        readable metadata that an attacker could use as a prompt-injection
        carrier.
      * Credential / secret stores under HERMES_HOME and the global Hermes
        root: ``auth.json``, ``auth.lock``, ``.anthropic_oauth.json``,
        ``.env``, ``webhook_subscriptions.json``, ``auth/google_oauth.json``,
        and anything under ``mcp-tokens/``. These hold plaintext provider keys,
        OAuth tokens, and HMAC secrets that the agent never needs to read
        directly — provider tools / gateway adapters consume them through
        internal channels.
      * Project-local environment files anywhere on disk: ``.env``,
        ``.env.local``, ``.env.development``, ``.env.production``,
        ``.env.test``, ``.env.staging``, ``.envrc``. These routinely hold
        API keys, database passwords, and other credentials for the user's
        own projects. The agent helping debug a project shouldn't normally
        need to read these — ``.env.example`` is the documented-shape
        substitute.

    **This is NOT a security boundary.** The terminal tool runs as the
    same OS user with shell access; the agent can still ``cat auth.json``
    or ``cat ~/.hermes/.env`` and exfiltrate the file. The read-deny exists
    as defense-in-depth that:

      * Returns a clear error to models that respect tool denials, which
        empirically prompts most modern models to stop rather than reach
        for the shell.
      * Surfaces a visible audit trail when something tries to read
        credentials — easier to spot in logs than a generic ``cat``.

    Treat any user-visible framing around this as "may help" rather than
    "stops attackers." A determined model or malicious instruction can
    always shell out.

    Callers that resolve relative paths against a non-process cwd
    (e.g. ``TERMINAL_CWD`` in ``tools/file_tools.py``) MUST pre-resolve
    and pass the absolute path string.  This function's own ``resolve()``
    is anchored at the Python process cwd, so a relative input like
    ``"auth.json"`` would otherwise miss the denylist when the task's
    terminal cwd differs from the process cwd.
    """
    resolved = Path(path).expanduser().resolve()

    # Resolve BOTH the active HERMES_HOME (profile-aware) AND the global
    # Hermes root so credential stores at <root>/auth.json etc. are also
    # blocked when running under a profile (HERMES_HOME points at
    # <root>/profiles/<name> in profile mode). Same shape as the write
    # deny widening (#15981, #14157).
    hermes_dirs: list[Path] = []
    for base in (_hermes_home_path(), _hermes_root_path()):
        try:
            real = base.resolve()
            if real not in hermes_dirs:
                hermes_dirs.append(real)
        except Exception:
            continue

    # Skills .hub: prompt-injection carriers.
    for hd in hermes_dirs:
        blocked_dirs = [
            hd / "skills" / ".hub" / "index-cache",
            hd / "skills" / ".hub",
        ]
        for blocked in blocked_dirs:
            try:
                resolved.relative_to(blocked)
            except ValueError:
                continue
            return (
                f"Access denied: {path} is an internal Hermes cache file "
                "and cannot be read directly to prevent prompt injection. "
                "Use the skills_list or skill_view tools instead."
            )

    # Credential / secret stores. Exact-file matches under either
    # HERMES_HOME or <root>.
    credential_file_names = (
        "auth.json",
        "auth.lock",
        ".anthropic_oauth.json",
        ".env",
        "webhook_subscriptions.json",
        os.path.join("auth", "google_oauth.json"),
        # Bitwarden Secrets Manager disk cache: stores plaintext secret values
        # to avoid re-fetching across back-to-back CLI invocations. The file
        # was introduced by #31968 but not added to this guard.
        os.path.join("cache", "bws_cache.json"),
    )
    for hd in hermes_dirs:
        for name in credential_file_names:
            try:
                blocked = (hd / name).resolve()
            except Exception:
                continue
            if resolved == blocked:
                return (
                    f"Access denied: {path} is a Hermes credential store "
                    "and cannot be read directly. Provider tools consume "
                    "these credentials through internal channels. "
                    "(Defense-in-depth — not a security boundary; the "
                    "terminal tool can still bypass.)"
                )

    # mcp-tokens/: directory prefix match — anything inside is OAuth
    # token material.
    for hd in hermes_dirs:
        try:
            mcp_tokens = (hd / "mcp-tokens").resolve()
        except Exception:
            continue
        if resolved == mcp_tokens:
            return (
                f"Access denied: {path} is the Hermes MCP token directory "
                "and cannot be read directly. (Defense-in-depth — not a "
                "security boundary; the terminal tool can still bypass.)"
            )
        try:
            resolved.relative_to(mcp_tokens)
        except ValueError:
            continue
        return (
            f"Access denied: {path} is a Hermes MCP token file "
            "and cannot be read directly. (Defense-in-depth — not a "
            "security boundary; the terminal tool can still bypass.)"
        )

    # Block common secret-bearing project-local .env files anywhere on disk.
    # The agent helping a user with their project rarely needs to read raw
    # .env contents — .env.example is the documented-shape substitute. The
    # terminal tool can still ``cat .env``; this is defense-in-depth, not a
    # boundary (see module docstring).
    if resolved.name.lower() in _BLOCKED_PROJECT_ENV_BASENAMES:
        return (
            f"Access denied: {path} is a secret-bearing environment file "
            "and cannot be read to prevent credential leakage. "
            "If you need to check the file structure, read .env.example instead. "
            "(Defense-in-depth — not a security boundary; the terminal tool can still bypass.)"
        )

    return None


def safe_extract_tar(
    tar: "tarfile.TarFile",
    dest: "Path | str",
    *,
    refuse_top_level: "frozenset[str] | None" = None,
) -> None:
    """Extract ``tar`` into ``dest`` with ``data``-filter semantics everywhere.

    ``tarfile``'s ``filter="data"`` landed in 3.11.4 while this project supports
    ``>=3.11``, so 3.11.0–3.11.3 have no filter. The obvious stopgap — validate
    the members yourself, then call an unfiltered ``extractall`` — is what this
    replaces, because **a lexical pre-flight check cannot be equivalent to the
    filter.** Three rounds of review found three separate bypasses of exactly
    such a check:

    1. a clean *name* on a symlink whose *target* escapes, with a later member
       written through it;
    2. hardlink targets, which ``tarfile`` resolves against the extraction root
       rather than the link's parent, so symlink math under-resolves them;
    3. and the one that settles it — an earlier symlink member changing what a
       later member's path *means*. Given ``a -> .``, a directory ``a/b``, and
       ``a/b/link -> ../../outside``, the link's lexical depth is 2 but its real
       depth is 1, so the target normalizes as contained while landing outside.
       Reproduced: the check accepted the archive and ``a/b/link/pwned`` was
       written beside the destination.

    Containment depends on what earlier members created, which a check over
    member metadata cannot know. So on interpreters without the filter this
    does not call ``extractall`` at all: it writes each member itself, creating
    only directories and regular files. Nothing that can redirect a later path
    is ever created, which makes the traversal question moot instead of
    answering it correctly. This mirrors ``hermes_cli.psutil_android``, which
    ``main`` reached independently for the psutil sdist.

    Paths are still validated, under both POSIX and Windows rules — ``os.path``
    on Windows also splits on ``\\`` and honours drive letters, so
    ``..\\outside`` and ``C:\\outside`` are escapes there while looking like
    one opaque component to ``PurePosixPath``.

    **How link members are handled, and why the two kinds differ.** Symlinks
    are refused outright: honouring one safely means resolving through links
    already created, which is the complexity that produced the bypasses above.
    Hardlinks are *materialized as copies* instead — ``tarfile.add()`` stores
    the second occurrence of an inode as a ``LNKTYPE`` member, so an ordinary
    snapshot of a skills tree containing hardlinks has them, and refusing those
    would make a backup unrestorable on exactly the interpreters this serves.
    A copy carries the same content and, unlike a link, cannot be used to reach
    anything else.

    So this is stricter than ``data`` for symlinks (``data`` allows a contained
    one) and equivalent for everything else. The divergence applies to
    3.11.0–3.11.3 only; on 3.11.4+ the real filter runs and a contained symlink
    is restored as a symlink.
    """
    import tarfile

    # The stdlib filter has the same hardlink hole, and unlike the fallback it
    # is the path that actually runs on every supported interpreter, so the
    # hazards are cleared out of the destination before handing over.
    _validate_members(tar, refuse_top_level or frozenset())

    try:
        tar.extractall(dest, filter="data")  # type: ignore[call-arg]
        return
    except TypeError:
        # Python 3.11.0-3.11.3 — no filter kwarg. Extract by hand rather than
        # falling back to an unfiltered extractall.
        pass

    root = Path(dest)
    # Deferred until every regular file is written:
    #   * symlinks — created last so they cannot interfere with extraction at
    #     all. This is defence in depth, not the guarantee: the guarantee is
    #     that ``_walk_dirs`` refuses to traverse *any* symlink, one this
    #     extraction just created included, so nothing is ever written through
    #     a link regardless of ordering. Verified by making creation inline and
    #     confirming every escape case still fails.
    #   * mtimes — a directory's mtime is bumped by writing its children, so it
    #     has to be stamped afterwards. ``data`` preserves mtimes and this used
    #     not to, which made a restored snapshot's timestamps depend on the
    #     interpreter (``build_skill_nodes()`` falls back to SKILL.md's mtime).
    deferred_links: list[tuple[tuple[str, ...], str]] = []
    dir_times: list[tuple[tuple[str, ...], float]] = []
    written: set[tuple[str, ...]] = set()

    # Directory modes are deliberately NOT restored, because ``filter="data"``
    # does not restore them either: a 0700 directory comes out 0755 on the
    # filtered path. Preserving them here would make the permissions of a
    # restored snapshot depend on the interpreter, which is the divergence this
    # whole function exists to remove. (That the stdlib widens a private
    # directory at all is a real question, but it is the stdlib's answer on
    # every supported version, not something to fix asymmetrically in the
    # fallback.)
    for member in tar.getmembers():
        parts = _safe_member_parts(member.name)

        if member.isdir():
            _close(_walk_dirs(root, parts, create=True))
            dir_times.append((parts, member.mtime))
            continue

        if member.issym():
            # Validated now, created later. The target is read by the kernel
            # relative to the link's own directory, so it is resolved that way;
            # containment is re-checked against the real filesystem once every
            # link exists, which is what actually settles it.
            target = member.linkname
            if _is_absolute_path(target):
                raise tarfile.TarError(
                    f"refusing to extract symlink {member.name!r} -> {target!r}: "
                    f"absolute target"
                )
            deferred_links.append((parts, target))
            continue

        if member.islnk():
            # The source must be something this archive already wrote. Resolving
            # `linkname` against the extraction root and copying whatever is
            # there let a crafted snapshot name a *preserved* file instead —
            # `.hub/secret.txt` — and pull it into the restored tree (the
            # stdlib path goes further and creates a real hardlink, so later
            # writes through the restored name mutate state rollback
            # deliberately excludes). Verified: `stolen.txt` came back holding
            # the hub's content on both paths.
            #
            # This cannot reject a legitimate snapshot: `tarfile.add()` only
            # emits LNKTYPE for an inode it has *already* archived, so the
            # target is always an earlier member.
            # tarfile.add() stores the second occurrence of a hardlinked inode
            # as a LNKTYPE member, so an ordinary snapshot of a skills tree
            # containing hardlinks has them — refusing outright would make the
            # backup unrestorable on exactly the interpreters this path serves.
            # Materialize it as a copy instead of creating a link: a copy has
            # the same content and cannot be used to reach anything else.
            # Hardlink targets are root-relative (tarfile joins linkname onto
            # the extraction root), so they validate the same way names do.
            src_parts = _safe_member_parts(member.linkname)
            if src_parts not in written:
                raise tarfile.TarError(
                    f"refusing hardlink {member.name!r} -> {member.linkname!r}: "
                    f"target is not an earlier member of this archive"
                )
            _copy_within(root, src_parts, parts, member)
            written.add(parts)
            continue

        if not member.isfile():
            raise tarfile.TarError(
                f"refusing to extract non-regular member {member.name!r} "
                f"(type {member.type!r}) without the 'data' filter"
            )

        extracted = tar.extractfile(member)
        if extracted is None:
            raise tarfile.TarError(f"cannot read archive member {member.name!r}")
        with extracted:
            _write_file(
                root, parts, extracted, _data_filter_mode(member.mode), member.mtime
            )
        written.add(parts)

    _create_symlinks(root, deferred_links)

    # Deepest first: stamping a parent before its children would be undone by
    # writing them.
    for parts, mtime in sorted(dir_times, key=lambda item: len(item[0]), reverse=True):
        fd = _walk_dirs(root, parts, create=False)
        try:
            os.utime(fd, (mtime, mtime))
        except (OSError, OverflowError, ValueError):
            pass
        finally:
            _close(fd)


# ---------------------------------------------------------------------------
# Path walking that refuses to traverse a symlink that is *already on disk*.
#
# Validating archive members is not enough: the destination can carry the
# redirect. curator_backup deliberately preserves ``skills/.hub`` across a
# rollback, so an existing ``skills/.hub/link -> /outside`` makes a perfectly
# ordinary member ``.hub/link/victim.txt`` — no link member in the archive at
# all — land outside the skills tree. Reproduced before this was added.
#
# Each component is therefore opened with ``O_NOFOLLOW`` relative to its parent
# directory descriptor, so an existing symlink anywhere along the path raises
# instead of being followed.
# ---------------------------------------------------------------------------

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_HAVE_DIR_FD = (
    hasattr(os, "supports_dir_fd")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
)


def _close(fd: int) -> None:
    os.close(fd)


def _walk_dirs(root: "Path", parts: "tuple[str, ...]", *, create: bool) -> int:
    """Open ``parts`` under ``root`` as a directory fd, never following a link.

    Refuses outright where the platform cannot enforce that. An earlier version
    fell back to plain path operations here and documented the hole, on the
    grounds that it merely preserved pre-existing behaviour — but a fallback
    that silently performs no enforcement, inside the function whose whole
    purpose is to guarantee it, is not a gap worth documenting. It is one worth
    closing.

    The combination this refuses is narrow: an interpreter old enough to lack
    ``filter="data"`` (3.11.0–3.11.3) *and* a platform without ``dir_fd``
    support (Windows). On 3.11.4+ the real filter runs and this is never
    reached.

    Stated more sharply than the first version of this comment managed: on
    that combination **every** curator rollback fails before restoring its
    first entry, and ``requires-python = ">=3.11,<3.14"`` still advertises it
    as supported. That is a real gap, not a rounding error, and the honest fix
    is to floor ``requires-python`` at ``3.11.4`` so the package stops claiming
    a configuration it cannot serve. Left undone here only because this
    environment's ``uv`` cannot parse the repo's ``uv.lock`` schema, so the
    lockfile cannot be regenerated and ``uv lock --check`` would fail CI —
    tracked as a follow-up rather than shipped broken.

    The alternative — hand-rolling reparse-point checks for Windows — is
    deliberately not taken. It would be security-critical code for a platform
    this sandbox cannot execute, and every hand-rolled containment scheme in
    this module's history has been bypassed. Refusing is verifiable; a second
    unverified guard is not.
    """
    import tarfile

    if not _HAVE_DIR_FD:
        raise tarfile.TarError(
            "refusing to extract: this interpreter predates tarfile's 'data' "
            "filter (added in 3.11.4) and this platform cannot open paths "
            "without following links, so extraction cannot be made safe "
            "against a redirect already present in the destination. "
            "Upgrade to Python 3.11.4 or newer."
        )

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            if create:
                try:
                    # No explicit mode: `filter="data"` clears the archived
                    # directory mode and lets `os.mkdir`'s default 0777 meet
                    # the process umask. Hard-coding 0755 here matched that
                    # only under umask 022 — under 002 the stdlib gives 0775
                    # and this gave 0755, so a group-shared skills tree lost
                    # group write depending on the interpreter's patch level.
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                nxt = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd
                )
            except OSError as exc:
                raise tarfile.TarError(
                    f"refusing to extract through {part!r}: the destination path "
                    f"component is a symlink or not a directory ({exc.strerror})"
                ) from exc
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_file(
    root: "Path", parts: "tuple[str, ...]", source, mode: int, mtime: "float | None" = None
) -> None:
    """Write ``source`` to ``parts`` under ``root`` without following links."""
    parent = _walk_dirs(root, parts[:-1], create=True)
    name = parts[-1]
    try:
        # Unlink first, then create exclusively, rather than opening the
        # existing file with O_TRUNC. O_NOFOLLOW rejects a *symlink* at this
        # path, but a hardlink is not a link to a file — it is the file, so
        # O_NOFOLLOW says nothing about it. curator_backup preserves
        # `skills/.hub` across a rollback, so a hardlink already sitting there
        # and pointing at something outside the tree would have had that
        # outside file truncated and overwritten in place. Reproduced before
        # this changed. Replacing the name detaches it from the shared inode.
        try:
            os.unlink(name, dir_fd=parent)
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
        fd = os.open(name, flags, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(source, dst)
        try:
            os.chmod(name, mode, dir_fd=parent, follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass
        if mtime is not None:
            try:
                os.utime(name, (mtime, mtime), dir_fd=parent, follow_symlinks=False)
            except (OSError, NotImplementedError, OverflowError, ValueError):
                # OverflowError is the one that mattered: a PAX member with an
                # out-of-range mtime (1e300) raised it straight out of
                # safe_extract_tar, and because it is not a TarError,
                # rollback() skipped its extraction-failure recovery — leaving
                # the partial tree visible and the original staged. A timestamp
                # is metadata; it must never be able to abort a restore.
                pass
    finally:
        _close(parent)


def _representable_mtime(mtime: float) -> bool:
    """True if ``os.utime`` can plausibly store this timestamp.

    The bound is deliberately loose — far beyond any real file time, far below
    where the float loses integer precision. It exists to catch a crafted or
    corrupt value such as ``1e300``, not to police plausible dates.
    """
    try:
        return math.isfinite(mtime) and abs(mtime) <= 2**53
    except TypeError:
        return False


def _filter_destination_parts(name: str) -> "tuple[str, ...] | None":
    """Where ``filter="data"`` will place ``name``, or ``None`` if it refuses.

    Mirrors the filter's own normalization rather than this module's stricter
    one. The filter strips leading slashes and only then checks for an escape,
    so ``/.hub/x`` is extracted as ``.hub/x`` — while ``_safe_member_parts``
    rejects it outright as absolute.

    That mismatch was a hole. The pre-pass below used the strict rule, so a
    member spelled ``/.hub/x`` was silently skipped as "extraction will reject
    it" and then extracted anyway, straight through a preserved hardlink.
    Reproduced: outside file overwritten, ``st_nlink`` still 2.

    ``None`` means the filter will raise before writing anything, so there is
    no hazard to clear.
    """
    if "\x00" in name:
        # `PurePosixPath` accepts an embedded NUL, every filesystem call
        # rejects it — with `ValueError`, which is neither OSError nor
        # TarError, so `rollback()` skipped recovery with the tree already
        # staged. Treated as unextractable here so the refusal is a TarError.
        return None
    stripped = name.lstrip("/")
    if _is_absolute_path(stripped):
        return None
    parts = tuple(p for p in PurePosixPath(stripped).parts if p not in ("", "."))
    if ".." in parts or not parts:
        return None
    return parts


def _caseless(part: str) -> str:
    """One key for every spelling of ``part`` that names the same file.

    Casefolding alone is not filesystem identity: macOS is normalization-
    *insensitive*, so ``é`` composed (U+00E9) and ``e``+U+0301 decomposed are
    one file there. A symlink spelled one way and a member spelled the other
    resolve through each other while comparing as different strings.

    One leading ``NFC`` pass is enough, and the order is the reason: ``NFC``
    makes canonically-equivalent spellings *byte-identical*, so the casefold
    that follows is applied to the same input and cannot diverge. Folding first
    would not have that property — casefolding emits an unnormalized result for
    26 code points, U+0390 among them. Checked exhaustively over the code-point
    range: zero spellings disagree this way round.

    ``NFC`` and not ``NFKC``: Apple's filesystems decompose canonically, so
    canonical equivalence is the identity they implement. Compatibility folding
    would additionally merge names the filesystem keeps apart.
    """
    return unicodedata.normalize("NFC", part).casefold()


def _top_level_names(name: str) -> "set[str]":
    """Every first component this member could have, under either separator.

    ``tarfile`` builds its destination with ``os.path``, so on Windows a member
    spelled ``.hub\\x`` is two components while ``PurePosixPath`` sees one
    opaque name. Reading it only the POSIX way let that spelling walk straight
    past the preserved-name refusal and extract into ``.hub`` after all. Both
    readings are checked, so the refusal does not depend on which platform is
    doing the extracting.
    """
    names: set[str] = set()
    for reading in (name, name.replace("\\", "/")):
        parts = _filter_destination_parts(reading)
        if parts:
            # Casefolded because Windows and default macOS resolve `.HUB/x`
            # onto the existing `.hub`, so a case-sensitive comparison missed
            # it and the write landed in the preserved tree anyway. Folding
            # unconditionally can only over-refuse, and only for an archive
            # containing a differently-cased `.hub` — which is not a skill
            # name, and refusing it costs nothing.
            names.add(_caseless(parts[0]))
    return names


def _canonical_readings(name: str) -> "tuple[tuple[str, ...], ...]":
    """Every component tuple this member could resolve to, casefolded.

    One place, used for every comparison the validator makes. Three rounds in a
    row found the same defect — a normalization rule applied in one check and
    not in the one beside it. Both separators, because ``tarfile`` splits on
    ``\\`` where the host does; casefolded, because Windows and default macOS
    resolve ``.HUB`` onto an existing ``.hub``. Comparing anything by hand
    against ``member.name`` is how the last three bypasses got in.
    """
    readings: set[tuple[str, ...]] = set()
    for reading in (name, name.replace("\\", "/")):
        parts = _filter_destination_parts(reading)
        if parts is None:
            continue
        readings.add(tuple(_caseless(part) for part in parts))
    return tuple(readings)


def _host_identity(name: str) -> "tuple[str, ...] | None":
    """Where this member actually lands *here*, as an identity key.

    Distinct from :func:`_canonical_readings`, and the distinction matters:

    * **Refusal** rules ask "could this reach somewhere forbidden?" — so they
      consider every reading, because over-refusing costs nothing.
    * **Identity** rules ask "are these two members the same file?" — so they
      must use the host's real semantics. Judging identity across both readings
      made ``demo/a\\b`` and ``demo/a/b`` collide, and on POSIX those are two
      legitimate, different files.

    ``normcase`` is the right primitive for the case half for the same reason:
    it lowercases on Windows and is the identity on POSIX.

    Unicode normalization is deliberately **not** applied here either, for the
    third instance of the same reason: macOS treats the composed and decomposed
    spellings of a name as one file, Linux treats them as two, and folding them
    together for identity would refuse a legitimate archive containing both.
    The refusal side folds them; this side does not.
    """
    reading = name.replace("\\", "/") if os.sep == "\\" or os.altsep == "\\" else name
    parts = _filter_destination_parts(reading)
    if parts is None:
        return None
    return tuple(os.path.normcase(part) for part in parts)


def _validate_members(tar: "tarfile.TarFile", refuse_top_level: "frozenset[str]") -> None:
    """Reject an archive the destination cannot safely receive.

    Everything here is decided from the **members alone**. That boundary is the
    point. Round six established that a pass over member metadata cannot know
    where a member will land, because an earlier member changes what a later
    path means — and an earlier version of this function ignored that and tried
    to inspect the destination anyway, producing a P1 in six consecutive review
    rounds.

    What closes the class is ``refuse_top_level``. ``curator_backup`` excludes
    ``.hub`` and ``.curator_backups`` from every snapshot it writes, and
    ``rollback()`` moves *everything else* aside before extracting — so the
    destination holds only those two directories, and a legitimate archive
    never contains a member under either. Refusing such members means
    extraction only ever writes to paths that do not exist yet. An empty
    destination cannot carry a hardlink, a junction or a symlink, so there is
    nothing to detach and no evolving tree to predict.

    **No member type short-circuits.** Directories skipped these checks three
    separate times — timestamps, preserved names, then symlink ancestry — each
    time because a ``continue`` sat ahead of a check that was never
    type-specific. Every rule below applies to every member.
    """
    import tarfile

    folded_refused = {_caseless(n) for n in refuse_top_level}
    seen_regular: set[tuple[str, ...]] = set()
    symlinked: set[tuple[str, ...]] = set()
    claimed: set[tuple[str, ...]] = set()

    for member in tar.getmembers():
        readings = _canonical_readings(member.name)
        if not readings:
            raise tarfile.TarError(f"refusing to extract unsafe path: {member.name!r}")

        if folded_refused & {reading[0] for reading in readings}:
            raise tarfile.TarError(
                f"refusing to extract {member.name!r}: it names a directory that "
                f"is preserved across a restore and never part of a snapshot"
            )

        if not _representable_mtime(member.mtime):
            # `os.utime` raises OverflowError on an out-of-range PAX mtime, and
            # `data` applies directory attributes at the very end — neither is
            # a TarError, so `rollback()` skipped recovery with the tree staged.
            raise tarfile.TarError(
                f"refusing to extract {member.name!r}: timestamp {member.mtime!r} "
                f"is out of range"
            )

        if any(
            reading[: len(sym)] == sym for reading in readings for sym in symlinked
        ):
            # An earlier symlink member changes what this path means. Safe to
            # refuse: tar does not archive content underneath a symlink, so
            # `snapshot_skills()` never emits members below a symlink member.
            raise tarfile.TarError(
                f"refusing to extract {member.name!r}: it resolves through an "
                f"earlier symlink member"
            )

        if not member.isfile():
            # Whatever this member is, it is not a regular file, so any
            # regular-file provenance recorded for a name it could occupy is
            # now stale. Invalidation is a *refusal* question — "could a later
            # member have replaced my hardlink source?" — so it runs over every
            # reading, not the host identity. `os.path.normcase` is the identity
            # function on macOS while the default filesystem there is
            # case-insensitive, so `demo/A` and `demo/a` are one entry that
            # host-keyed provenance saw as two: a regular `demo/A`, a symlink
            # `demo/a -> ../.hub/x`, then a hardlink to `demo/A` had the symlink
            # standing in for the file. Folding here over-refuses that archive
            # on Linux, where the two really are separate files. That is the
            # cheap direction, and `tarfile.add()` cannot produce the shape.
            #
            # Ahead of the `isdir` return below, because a rule that sits after
            # one has been the bypass three separate times.
            seen_regular.difference_update(readings)

        if member.isdir():
            continue

        if not (member.isfile() or member.islnk() or member.issym()):
            raise tarfile.TarError(
                f"refusing to extract non-regular member {member.name!r} "
                f"(type {member.type!r})"
            )

        identity = _host_identity(member.name)
        if identity is None:
            raise tarfile.TarError(f"refusing to extract unsafe path: {member.name!r}")
        if identity in claimed:
            # A name written twice invalidates anything already concluded about
            # it — regular `a`, then a symlink also named `a`, then a hardlink
            # to `a`. `tarfile.add()` walks a tree once, so a real snapshot
            # never contains a duplicate.
            raise tarfile.TarError(
                f"refusing to extract {member.name!r}: the archive writes this "
                f"path more than once"
            )
        claimed.add(identity)

        if (member.islnk() or member.issym()) and "\x00" in member.linkname:
            # Same failure mode as a NUL in the member name, on the target
            # instead: `os.symlink` raises ValueError, which is neither OSError
            # nor TarError, so it escapes `rollback()`'s recovery.
            raise tarfile.TarError(
                f"refusing to extract {member.name!r}: its link target contains "
                f"a NUL byte"
            )

        if member.islnk():
            # The source must be an earlier *regular file* member, under
            # every reading of its name. "Earlier member" alone was not enough:
            # a symlink alias could stand in for it. Neither was the host
            # identity, which treats `demo/A` and `demo/a` as two names on a
            # filesystem that has one. `tarfile.add()` only emits LNKTYPE for a
            # regular inode it already archived, so this cannot reject a real
            # snapshot.
            src = _canonical_readings(member.linkname)
            if not src or not set(src) <= seen_regular:
                raise tarfile.TarError(
                    f"refusing hardlink {member.name!r} -> {member.linkname!r}: "
                    f"target is not an earlier regular-file member"
                )

        if member.issym():
            symlinked.update(readings)
        else:
            # Keyed by reading, not identity, so the invalidation above can
            # find it. A hardlink member lands as a copy, so it is a regular
            # file too and may itself be a later hardlink's source.
            seen_regular.update(readings)


def _copy_within(
    root: "Path", src_parts: "tuple[str, ...]", dst_parts: "tuple[str, ...]", member
) -> None:
    """Copy an already-extracted member to another path inside ``root``."""
    import tarfile

    parent = _walk_dirs(root, src_parts[:-1], create=False)
    try:
        try:
            fd = os.open(src_parts[-1], os.O_RDONLY | _O_NOFOLLOW, dir_fd=parent)
        except OSError as exc:
            raise tarfile.TarError(
                f"hardlink {member.name!r} -> {member.linkname!r}: "
                f"cannot read target ({exc.strerror})"
            ) from exc
        handle = os.fdopen(fd, "rb")
    finally:
        _close(parent)

    with handle:
        _write_file(
            root, dst_parts, handle, _data_filter_mode(member.mode), member.mtime
        )


def _is_absolute_path(value: str) -> bool:
    """True if ``value`` is absolute under POSIX *or* Windows parsing."""
    return (
        value.startswith("/")
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or bool(PureWindowsPath(value).anchor)
    )


def _create_symlinks(root: "Path", links: "list[tuple[tuple[str, ...], str]]") -> None:
    """Create validated symlinks, then prove none of them escapes.

    Two things make this safe, and it is worth being precise about which does
    the work. The guarantee is that ``_walk_dirs`` refuses to traverse any
    symlink — including one this extraction just created — so no member is ever
    written *through* a link. Creating links last is defence in depth on top of
    that: with inline creation every escape case here still fails, which was
    checked rather than assumed.

    Containment is then checked with ``realpath`` against the finished tree
    rather than lexically. That is the check that actually holds: it resolves
    through any link chain the archive just created, which no amount of string
    math over member names can do. A link that escapes is removed and the
    extraction fails, so a failed restore never leaves one behind.
    """
    import tarfile

    if not links:
        return

    root_real = os.path.realpath(root)
    created: list[str] = []
    try:
        for parts, target in links:
            parent = _walk_dirs(root, parts[:-1], create=True)
            try:
                os.symlink(target, parts[-1], dir_fd=parent)
            except OSError as exc:
                raise tarfile.TarError(
                    f"cannot create symlink {'/'.join(parts)!r} -> {target!r}: "
                    f"{exc.strerror}"
                ) from exc
            finally:
                _close(parent)
            created.append(os.path.join(root_real, *parts))

        for path in created:
            resolved = os.path.realpath(path)
            if resolved != root_real and not resolved.startswith(root_real + os.sep):
                raise tarfile.TarError(
                    f"refusing to extract symlink {os.path.relpath(path, root_real)!r}: "
                    f"it resolves outside the destination"
                )
    except BaseException:
        for path in created:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise


def _data_filter_mode(mode: int) -> int:
    """Sanitize a regular-file mode the way ``filter="data"`` does.

    Mirrors ``tarfile._get_filtered_attrs`` for regular and hard-link members:
    drop group/other write, clear *all* execute bits unless the owner had
    execute, then guarantee owner read/write. Verified against the stdlib
    across every mode in ``0o000``–``0o777``.

    Applying the archived mode verbatim instead would restore a ``0777`` member
    world-writable on 3.11.0–3.11.3 and ``0755`` everywhere else — executable
    skill content editable by any local user, on those interpreters only.
    (Directories are the opposite case and deliberately untouched: ``data``
    ignores their modes entirely, so matching it means not restoring them.)
    """
    mode &= 0o755
    if not mode & 0o100:
        mode &= ~0o111
    return mode | 0o600


def _safe_member_parts(name: str) -> tuple[str, ...]:
    """Split a stored member name, refusing anything that escapes.

    Two separate questions, and conflating them lost data.

    *Validation* is done under POSIX **and** Windows rules, because
    ``extractall`` builds its destination with ``os.path``, which on Windows
    also treats ``\\`` as a separator and honours drive letters — so a name
    that is one opaque component to ``PurePosixPath`` can be a
    multi-component escape there. An escape under either reading is refused
    everywhere: the archive decides, not the host.

    *Splitting* is done under the host's rules only. This used to rewrite every
    ``\\`` to ``/`` before splitting and the docstring called that "refusing" a
    backslash filename — it was nothing of the kind. A backslash is a legal
    POSIX filename character, so ``demo/a\\b`` was silently **relocated** to
    ``demo/a/b``; given a snapshot holding both, one entry overwrote the other
    and a rollback lost a file without reporting anything. ``filter="data"``
    keeps the two distinct on POSIX, so the fallback does now as well.
    """
    import tarfile

    if _is_absolute_path(name):
        raise tarfile.TarError(f"refusing to extract unsafe path: {name!r}")

    # Validate: refuse a traversal expressed with either separator.
    for reading in (name, name.replace("\\", "/")):
        if ".." in PurePosixPath(reading).parts:
            raise tarfile.TarError(f"refusing to extract unsafe path: {name!r}")

    # Split: on Windows ``\`` really is a separator, so honour it there and
    # only there. Nothing is written on that platform without ``dir_fd``
    # support (see ``_walk_dirs``), but the split must still be correct.
    split_on = name.replace("\\", "/") if os.sep == "\\" or os.altsep == "\\" else name
    parts = tuple(p for p in PurePosixPath(split_on).parts if p not in ("", "."))
    if not parts:
        raise tarfile.TarError(f"refusing to extract unsafe path: {name!r}")
    return parts


def raise_if_read_blocked(path: str) -> None:
    """Raise ``ValueError`` if ``path`` is a denied Hermes read (see
    :func:`get_read_block_error`), else return.

    Shared chokepoint for provider input-loading sites that read a local
    file the model/tool supplied (e.g. image-gen ``image_url`` /
    ``reference_image_urls`` paths). Centralizes the guard so every provider
    enforces the same read boundary with identical semantics instead of each
    open-coding the try/except block (#57698).

    Best-effort by design: if ``agent.file_safety`` machinery is somehow
    unavailable at the call site the guard no-ops rather than breaking local
    image loading — consistent with the defense-in-depth (not security
    boundary) framing of the denylist itself. The blocking ``ValueError`` from
    a real hit still propagates; only unexpected internal errors are swallowed.
    """
    try:
        blocked = get_read_block_error(path)
    except Exception:  # noqa: BLE001 - guard must never break local-file loading
        return
    if blocked:
        raise ValueError(blocked)


# ---------------------------------------------------------------------------
# Cross-profile write guard (#TBD)
#
# Hermes profiles are separate HERMES_HOME dirs under
# ``<root>/profiles/<name>/``. Each profile has its own skills/, plugins/,
# cron/, memories/. When an agent runs under one profile, writing into
# ANOTHER profile's directories is almost always wrong — those skills /
# plugins / cron jobs / memories affect a different session the user runs
# from a different shell.
#
# Soft guard, NOT a security boundary: the agent runs as the same OS user
# and has unrestricted terminal access, so this returns a warning the model
# can choose to honor or override with ``cross_profile=True``. Same shape
# as the dangerous-command approval flow — the agent is told the boundary
# exists, and explicit user direction is required to cross it.
#
# Reference: May 2026 incident where a hermes-security profile session
# edited skills under both ``~/.hermes/profiles/hermes-security/skills/``
# AND ``~/.hermes/skills/`` (the default profile's skills) without realizing
# the second path belonged to a different profile.
# ---------------------------------------------------------------------------

# Profile-scoped directories under HERMES_HOME / <root> / <root>/profiles/<X>/
# that should be guarded. Adding a new area here extends the guard with no
# other code change.
PROFILE_SCOPED_AREAS = ("skills", "plugins", "cron", "memories")


def _resolve_active_profile_name() -> str:
    """Return the active profile name derived from HERMES_HOME.

    ``~/.hermes``              -> ``"default"``
    ``~/.hermes/profiles/X``  -> ``"X"``

    Falls back to ``"default"`` on any resolution failure so the guard
    never raises into the tool path.
    """
    try:
        home_real = _hermes_home_path().resolve()
        root_real = _hermes_root_path().resolve()
    except (OSError, RuntimeError):
        return "default"
    profiles_dir = root_real / "profiles"
    try:
        rel = home_real.relative_to(profiles_dir)
        parts = rel.parts
        if len(parts) >= 1:
            return parts[0]
    except ValueError:
        pass
    return "default"


def classify_cross_profile_target(path: str) -> Optional[dict]:
    """Classify a write target as cross-profile if it lands in another
    profile's scoped area (skills/plugins/cron/memories).

    Returns ``None`` when the target is outside Hermes scope, or is inside
    the ACTIVE profile, or doesn't hit a profile-scoped area. Otherwise
    returns a dict with:

      * ``active_profile``: name of the profile the agent is running as
      * ``target_profile``: name of the profile the path belongs to
      * ``area``: which scoped area (``"skills"``, ``"plugins"``, etc.)
      * ``target_path``: the resolved path string

    The caller decides what to do with the result — surface a warning to
    the model, prompt the user, or (with explicit consent /
    ``cross_profile=True``) proceed anyway.
    """
    try:
        target = Path(os.path.expanduser(str(path))).resolve()
        root_real = _hermes_root_path().resolve()
    except (OSError, RuntimeError):
        return None

    target_profile: Optional[str] = None
    area: Optional[str] = None

    try:
        rel = target.relative_to(root_real)
    except ValueError:
        return None

    parts = rel.parts
    if not parts:
        return None

    if parts[0] in PROFILE_SCOPED_AREAS:
        # ``<root>/<area>/...`` → default profile.
        target_profile = "default"
        area = parts[0]
    elif (
        parts[0] == "profiles"
        and len(parts) >= 3
        and parts[2] in PROFILE_SCOPED_AREAS
    ):
        # ``<root>/profiles/<name>/<area>/...`` → named profile.
        target_profile = parts[1]
        area = parts[2]
    else:
        return None

    active_profile = _resolve_active_profile_name()
    if target_profile == active_profile:
        # In-profile write — not a cross-profile event.
        return None

    return {
        "active_profile": active_profile,
        "target_profile": target_profile,
        "area": area,
        "target_path": str(target),
    }


def get_cross_profile_warning(path: str) -> Optional[str]:
    """Return a model-facing warning string when ``path`` is cross-profile.

    Returns ``None`` when the write is in-scope (same profile) or outside
    Hermes entirely. Caller is expected to surface the warning to the
    agent as a tool-result error, NOT to silently allow the write — the
    agent must either get explicit user direction to proceed, or pass
    ``cross_profile=True`` to its write tool.

    This is defense-in-depth: the terminal tool runs as the same OS user
    and can write any of these paths without going through this guard.
    Treat the guard as a confusion-reducer, not a security boundary.
    """
    info = classify_cross_profile_target(path)
    if info is None:
        return None
    return (
        f"Cross-profile write blocked by soft guard: {info['target_path']} "
        f"belongs to Hermes profile {info['target_profile']!r}, but the "
        f"agent is running under profile {info['active_profile']!r}. "
        f"Editing another profile's {info['area']}/ will affect that "
        f"profile's future sessions, not the one you are currently in. "
        f"Confirm with the user before proceeding. To bypass this guard "
        f"after explicit user direction, retry the call with "
        f"``cross_profile=True``. (Defense-in-depth — not a security "
        f"boundary; the terminal tool can still bypass.)"
    )


# ---------------------------------------------------------------------------
# Sandbox-mirror write guard (#32049)
#
# Non-local terminal backends (Docker, Daytona, etc.) bind a sandbox-local
# directory to the container's ``$HOME``. The on-disk layout looks like
#
#   <HERMES_HOME>/profiles/<name>/sandboxes/<backend>/<task>/home/.hermes/...
#
# When the agent (running host-side) speculates that authoritative profile
# state lives at one of those sandbox-mirror paths, the write lands on the
# mirror — never read by the host process — while the host file is left
# untouched. The agent reports success, the user sees no change, and on
# disk two divergent copies accumulate. See #32049 for evidence.
#
# This guard is path-shape-only: it detects the
# ``…/sandboxes/<backend>/<task>/home/.hermes/…`` segment and warns
# regardless of which Hermes profile is active. It does NOT cover the
# inner-container case where the bind mount strips the ``sandboxes/`` prefix
# (the agent's view inside the container is plain ``/root/.hermes/...``);
# that case needs a separate dispatch-layer or host-side ``profile_state``
# tool.
# ---------------------------------------------------------------------------


def _find_sandbox_mirror_segments(parts: tuple) -> Optional[int]:
    """Return the index of the inner ``.hermes`` part in a sandbox-mirror path.

    Matches ``…/sandboxes/<backend>/<task>/home/.hermes/…`` and returns the
    index where the inner Hermes-state portion starts. Returns ``None`` for
    paths that do not contain the sandbox-mirror shape.
    """
    for i, part in enumerate(parts):
        if part != "sandboxes":
            continue
        # Need at least: sandboxes / <backend> / <task> / home / .hermes / <thing>
        if i + 5 >= len(parts):
            continue
        if parts[i + 3] == "home" and parts[i + 4] == ".hermes":
            return i + 4
    return None


def classify_sandbox_mirror_target(path: str) -> Optional[dict]:
    """Classify a write target as a sandbox-mirror of authoritative Hermes state.

    Returns ``None`` when the path does not match the sandbox-mirror shape.
    Otherwise returns a dict with:

      * ``target_path``: the resolved path string
      * ``mirror_root``: the ``…/sandboxes/<backend>/<task>/home/.hermes``
        prefix (so callers can show users which sandbox owns the mirror)
      * ``inner_path``: the portion under the mirror's ``.hermes`` (what the
        agent likely meant to address on the host)

    Detection is path-shape-only — does not require any Hermes resolver to
    succeed, so it works correctly even when called from contexts where
    HERMES_HOME resolution would be ambiguous.
    """
    try:
        target = Path(os.path.expanduser(str(path))).resolve()
    except (OSError, RuntimeError):
        return None

    parts = target.parts
    inner_idx = _find_sandbox_mirror_segments(parts)
    if inner_idx is None:
        return None

    mirror_root = str(Path(*parts[: inner_idx + 1]))
    inner_path = str(Path(*parts[inner_idx + 1 :])) if inner_idx + 1 < len(parts) else ""

    return {
        "target_path": str(target),
        "mirror_root": mirror_root,
        "inner_path": inner_path,
    }


def get_sandbox_mirror_warning(path: str) -> Optional[str]:
    """Return a model-facing warning when ``path`` lands in a sandbox mirror.

    Returns ``None`` when the path is not a sandbox-mirror target. Caller
    is expected to surface the warning to the agent as a tool-result
    error. The bypass kwarg (``cross_profile=True``) is shared with the
    cross-profile guard: both are soft "I know what I'm doing" overrides
    a user can authorise.

    Defense-in-depth, NOT a security boundary: the terminal tool runs as
    the same OS user and can write the mirror path directly. The guard
    exists to surface the misclassification before the silent-success +
    divergent-copy footgun in #32049 fires.
    """
    info = classify_sandbox_mirror_target(path)
    if info is None:
        return None
    return (
        f"Sandbox-mirror write blocked by soft guard: {info['target_path']} "
        f"sits under {info['mirror_root']!r}, which is a per-task mirror "
        f"created by a non-local terminal backend (docker/daytona/etc.). "
        f"Writes here land on a copy that the host Hermes process never "
        f"reads — the authoritative file is likely {info['inner_path']!r} "
        f"under the real HERMES_HOME. Use the host-side tool for "
        f"authoritative state (e.g. ``memory`` for memories), or address "
        f"the host path directly. To bypass this guard after explicit "
        f"user direction, retry the call with ``cross_profile=True``. "
        f"(Defense-in-depth — not a security boundary; the terminal tool "
        f"can still bypass.)"
    )


# ---------------------------------------------------------------------------
# Container-context mirror guard (inner-container case — #32049 follow-up)
#
# Brian's shape-based detector (#32213) catches paths that still carry the
# full ``…/sandboxes/<backend>/<task>/home/.hermes/…`` prefix on the host.
# But when file tools execute *inside* the container the bind-mount strips
# that prefix: the agent sees plain ``/root/.hermes/…``.  The root:root
# ownership on the divergent SOUL.md in #32049 confirms this is the primary
# failure mode.
#
# Fix: file_tools passes the active Docker mirror prefix when the terminal
# backend is docker + persistent. This catches the very first file-tool call,
# before a DockerEnvironment object necessarily exists.
# ---------------------------------------------------------------------------


def classify_container_mirror_target(
    path: str,
    mirror_prefix: str | None = None,
) -> Optional[dict]:
    """Classify a write target as a container-side sandbox mirror.

    ``mirror_prefix`` must be supplied by the caller after it has established
    that file tools are executing in a container whose home is a sandbox
    mirror. Returns ``None`` when no such context is active or the path is not
    under the mirror prefix. Otherwise returns:

      * ``target_path``: resolved path string
      * ``mirror_root``: the declared container mirror prefix
      * ``inner_path``: portion under the mirror root (what the agent
        likely meant to address in the host HERMES_HOME)
    """
    if not mirror_prefix:
        return None
    try:
        target = Path(os.path.expanduser(str(path))).resolve()
        mirror = Path(os.path.expanduser(mirror_prefix)).resolve()
        inner = target.relative_to(mirror)
    except (OSError, RuntimeError, ValueError):
        return None
    return {
        "target_path": str(target),
        "mirror_root": str(mirror),
        "inner_path": inner.as_posix(),
    }


def get_container_mirror_warning(
    path: str,
    mirror_prefix: str | None = None,
) -> Optional[str]:
    """Return a model-facing warning when *path* lands in the container's
    sandbox mirror of authoritative Hermes state.

    The caller supplies ``mirror_prefix`` only when the current file-tool
    backend is known to execute inside a Docker sandbox. Same contract as
    ``get_cross_profile_warning``: soft guard, returns ``None`` for
    non-mirror paths, caller surfaces as a tool-result error. Bypass via
    ``cross_profile=True`` after explicit user direction.
    """
    info = classify_container_mirror_target(path, mirror_prefix)
    if info is None:
        return None
    return (
        f"Sandbox-mirror write blocked by soft guard: {info['target_path']} "
        f"sits under {info['mirror_root']!r}, which is the container's "
        f"bind-mounted home — a per-task mirror that the host Hermes "
        f"process never reads. The authoritative file is "
        f"{info['inner_path']!r} under the real HERMES_HOME. Use the "
        f"host-side tool for authoritative state (e.g. ``memory`` for "
        f"memories), or address the host path directly. To bypass after "
        f"explicit user direction, retry with ``cross_profile=True``. "
        f"(Defense-in-depth — not a security boundary; the terminal tool "
        f"can still bypass.)"
    )
