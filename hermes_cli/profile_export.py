"""Credential-safe profile export helpers.

This module owns the profile-export security boundary: path classification,
SQLite snapshotting and verification, staged-file scrubbing, symlink rejection,
and atomic archive publication. Profile lookup and CLI orchestration remain in
``hermes_cli.profiles``.
"""

import os
import re
from pathlib import Path
from typing import Iterator, Optional

# Directories/files to exclude when exporting the default (~/.hermes) profile.
# The default profile contains infrastructure (repo checkout, worktrees, DBs,
# caches, binaries) that named profiles don't have.  We exclude those so the
# export is a portable, reasonable-size archive of actual profile data.
_DEFAULT_EXPORT_EXCLUDE_ROOT = frozenset({
    # Infrastructure
    "hermes-agent",         # repo checkout (multi-GB)
    ".worktrees",           # git worktrees
    "profiles",             # other profiles — never recursive-export
    "bin",                  # installed binaries (tirith, etc.)
    "node_modules",         # npm packages
    # Databases & runtime state
    "state.db", "state.db-shm", "state.db-wal",
    "hermes_state.db",
    "response_store.db", "response_store.db-shm", "response_store.db-wal",
    "gateway.pid", "gateway_state.json", "processes.json",
    "auth.json",            # API keys, OAuth tokens, credential pools
    ".env",                 # API keys (dotenv)
    "auth.lock", "active_profile", ".update_check",
    "errors.log",
    ".hermes_history",
    # Caches (regenerated on use)
    "image_cache", "audio_cache", "document_cache",
    "browser_screenshots", "checkpoints",
    "sandboxes",
    "logs",                 # gateway logs
})

# Allow-list for ``export_profile("default")``: when HERMES_HOME equals the
# cwd (Docker/custom deployments), the default profile home is the working
# directory and contains arbitrary user files that should NOT be bundled
# into the export. The set below identifies the *known Hermes profile
# artifacts* at the root of HERMES_HOME; everything else is excluded.
# Sensitive runtime infrastructure (``state.db``, ``logs/``, ``auth.*``,
# other profiles) is intentionally *not* in this list so the export stays
# a portable, credential-free snapshot of the user-facing surface
# (#58394). Add new artifacts here when introduced in ``hermes_constants``.
_DEFAULT_EXPORT_INCLUDE_ROOT = frozenset({
    # Configuration / persona
    "config.yaml", "SOUL.md", "MEMORY.md", "USER.md", "todo.json",
    "system_prompt.md", "AGENTS.md", "CLAUDE.md", ".cursorrules",
    # Desktop appearance/interface overlay (written by the desktop app's
    # profile export; applied by its import; see desktop.json handling).
    "desktop.json",
    # Secret-free dotenv templates document the variables a profile expects.
    ".env.example", ".env.sample", ".env.template", ".env.dist",
    # User-facing skill, cron, and session artifacts
    "skills", "cron", "scripts", "sessions",
    # Plugin / memory surfaces (per-profile overrides live here)
    "plugins", "memories", "knowledge", "preferences",
})


# Transient entries excluded from every profile export. SQLite databases are
# identified by their header, not their filename, so extensionless and custom-
# suffix databases receive the same snapshot and sidecar policy.
_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_SIDECAR_ENDINGS = ("-shm", "-wal", "-journal")
_EXPORT_TRANSIENT_SUFFIXES = (
    ".sock",
    ".tmp",
)


def _is_transient_export_name(name: str) -> bool:
    """Return True for cache or transient files unsafe to copy live."""
    lowered = name.lower()
    return name == "__pycache__" or lowered.endswith(_EXPORT_TRANSIENT_SUFFIXES)


def _has_sqlite_header(path: Path) -> bool:
    """Return whether a regular file has SQLite's canonical file header."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _sqlite_sidecars_in_directory(directory: str, contents: list[str]) -> set[str]:
    """Return sidecars belonging to header-confirmed SQLite files."""
    available = set(contents)
    ignored: set[str] = set()
    for entry in contents:
        if not _has_sqlite_header(Path(directory) / entry):
            continue
        ignored.update(
            candidate
            for ending in _SQLITE_SIDECAR_ENDINGS
            if (candidate := f"{entry}{ending}") in available
        )
    return ignored


# ---------------------------------------------------------------------------
# Sensitive-file detection (shared by every export path)
# ---------------------------------------------------------------------------
#
# Profile archives are meant to be shared.  They must never carry API keys,
# OAuth tokens, credential-pool data, or the timestamped *backups* Hermes
# writes during normal operation:
#
#   hermes_cli/setup.py          → config.yaml.bak.<YYYYmmdd_HHMMSS>
#   hermes_cli/xai_retirement.py → config.yaml.bak-pre-migrate-xai-<ts>
#   (and other config rewrites)  → config.yaml.bak-<reason>-<ts>, .env.bak-<...>
#
# The historical exclusion lists only caught the *exact* names ``.env`` and
# ``auth.json``, so every ``config.yaml.bak*`` / ``.env.bak*`` slipped into the
# archive.  ``_is_sensitive_export_name`` is the single source of truth used by
# both the default-profile and named-profile export paths, matched at ANY
# directory depth (backups can live in subdirs too).

# Exact file basenames that always hold credentials. This mirrors the
# canonical Hermes credential guards in ``hermes_cli.web_server``,
# ``agent.file_safety``, and ``gateway.platforms.base``. ``config.yaml`` is
# intentionally not included: the active config is part of a portable profile,
# while its ``config.yaml.bak*`` copies are excluded below.
_EXPORT_SENSITIVE_BASENAMES = frozenset({
    ".env",
    ".envrc",
    ".claude.json",
    ".netrc",
    ".npmrc",
    ".pgpass",
    ".pypirc",
    "auth.json",
    "auth.lock",
    ".anthropic_oauth.json",
    "google_token.json",
    "google_oauth_pending.json",
    "google_oauth.json",
    "webhook_subscriptions.json",
    "feishu_comment_pairing.json",
    "bws_cache.json",
    "bws_cache.enc.json",
    "oauth_creds.json",
    ".git-credentials",
    # SSH private keys (extensionless) — the per-profile ``home/`` isolates
    # ssh/gh/git configs and can hold these under ``.ssh/``.
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
})

# Canonical credential-directory trees, expressed relative to a profile root.
# Root-relative matching avoids deleting unrelated user content such as
# ``plugins/example/pairing/`` while still covering both legacy ``pairing/``
# and the newer ``platforms/pairing/`` store. Backup-renamed components are
# normalized before comparison.
_EXPORT_SENSITIVE_PROFILE_DIR_PREFIXES = frozenset({
    ("mcp-tokens",),
    ("pairing",),
    ("platforms", "pairing"),
})

# The ``home/`` directory is a persistent subprocess HOME for profile-backed
# containers. It can therefore contain the same credential trees Hermes blocks
# from generic reads and media delivery. Most of ``home/`` remains portable,
# including ordinary dot-config applications; only credential-bearing prefixes
# are removed. Nested tuples cover targeted stores beneath ``.config`` without
# dropping that entire directory.
_EXPORT_SENSITIVE_PROFILE_HOME_DIR_PREFIXES = frozenset({
    (".ssh",),
    (".aws",),
    (".gnupg",),
    (".kube",),
    (".docker",),
    (".azure",),
    (".gcloud",),
    (".config", "gh"),
    (".config", "gcloud"),
    (".config", "github-copilot"),
    ("library", "keychains"),
})

# dotenv templates are conventionally secret-free, but only these exact names
# are safe. A credential backup such as ``.env.bak.example`` must not become
# exportable merely because its final suffix looks like a template.
_EXPORT_ENV_TEMPLATE_NAMES = frozenset({
    ".env.example",
    ".env.sample",
    ".env.template",
    ".env.dist",
})

# ``hermes_cli.config._backup_corrupt_config`` writes this exact base family
# when a malformed config is preserved before a later rewrite. The final
# ``.bak`` is not adjacent to ``config.yaml``, so ordinary backup-suffix peeling
# alone cannot recover the sensitive basename. Separator-delimited derivatives
# of that generated backup remain sensitive too.
_EXPORT_CORRUPT_CONFIG_BACKUP_RE = re.compile(
    r"^config\.ya?ml\.corrupt\.\d{8}-\d{6}\.bak(?:[._~-].*)?$",
    re.IGNORECASE,
)

# Backup, renamed-copy, and stale atomic-temp suffixes for canonical stores and
# any other name already classified as sensitive. Numeric and trailing-tilde
# suffixes cover names such as ``auth.json.20260101`` and ``auth.json~``. The
# base name is reclassified recursively so ``credentials.json.bak`` and
# ``oauth_creds.json.tmp.<pid>.<uuid>`` are blocked while ``notes.txt.bak``
# remains safe.
_EXPORT_BACKUP_NAME_RE = re.compile(
    r"^(?P<base>.+?)(?:"
    r"[._-](?:bak|backup|old|copy|tmp)(?:[._-].*)?"
    r"|[._-]\d{8}(?:[._-]\d{4,6})?"
    r"|~"
    r")$",
    re.IGNORECASE,
)

# Path components use a stricter backup pattern than file basenames. A bare
# backup marker or one followed by a numeric timestamp is treated as a renamed
# credential tree, while ordinary directories such as ``pairing-old-notes``
# and ``mcp-tokens_copy_of_docs`` remain portable.
_EXPORT_DIRECTORY_BACKUP_NAME_RE = re.compile(
    r"^(?P<base>.+?)(?:"
    # ``bak`` and ``backup`` are unambiguous backup markers, including
    # free-form reasons such as ``.bak-pre-migrate`` / ``.backup-before-reset``.
    r"[._-](?:bak|backup)(?:[._-][\w.-]+)?"
    # ``old``, ``copy``, and ``tmp`` are common words in ordinary directory
    # names, so only bare or numeric-run forms are normalized. This preserves
    # safe lookalikes such as ``pairing-old-notes`` and
    # ``mcp-tokens_copy_of_docs``.
    r"|[._-](?:old|copy|tmp)(?:[._-]\d[\w.-]*)?"
    r"|[._-]\d{8}(?:[._-]\d{4,6})?"
    r"|~"
    r")$",
    re.IGNORECASE,
)

# Unambiguously private key / keystore extensions. PEM files are content-checked
# separately because public CA bundles and certificates are also commonly PEM.
_EXPORT_SENSITIVE_SUFFIXES = (
    ".key", ".ppk", ".p12", ".pfx", ".keystore", ".jks",
)

# Credential-/token-looking names. The keyword must be a whole token bounded by
# start/end or a separator, so ``tokenizer.json`` and ``token_count.md`` are NOT
# matched while ``client_secret.json``, ``credentials.json``, ``access_token``,
# and ``api-key.txt`` are. Restricted to credential-container extensions (or no
# extension) so ordinary docs/skills like ``secret-santa.md`` still export.
_EXPORT_CREDENTIAL_KEYWORD_RE = re.compile(
    r"(?:^|[._-])"
    r"(?:credentials?|secrets?|api[_-]?keys?|access[_-]?tokens?"
    r"|refresh[_-]?tokens?|client[_-]?secrets?)"
    r"(?:$|[._-])",
    re.IGNORECASE,
)
_EXPORT_CREDENTIAL_CONTAINER_SUFFIXES = (
    ".json", ".txt", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".env", ".secret",
)


def _is_sensitive_export_name(name: str) -> bool:
    """Return True if *name* is a sensitive export path component.

    This covers credential files and their backup or temporary derivatives.
    Matching is case-insensitive, and callers apply it at every directory depth
    so nested OAuth stores and backup files are caught too. Credential-directory
    trees are handled separately with root-relative path rules.
    """
    lowered = name.lower()

    if lowered in _EXPORT_ENV_TEMPLATE_NAMES:
        return False

    if _EXPORT_CORRUPT_CONFIG_BACKUP_RE.fullmatch(lowered):
        return True

    if lowered in _EXPORT_SENSITIVE_BASENAMES:
        return True

    # Every non-template dotenv variant is sensitive, including misleading
    # shapes such as .env.bak.example.
    if lowered.startswith(".env."):
        return True

    backup_match = _EXPORT_BACKUP_NAME_RE.fullmatch(lowered)
    if backup_match:
        base_name = backup_match.group("base")
        if base_name in {"config.yaml", "config.yml"}:
            return True
        if _is_sensitive_export_name(base_name):
            return True

    if lowered.endswith(_EXPORT_SENSITIVE_SUFFIXES):
        return True

    if _EXPORT_CREDENTIAL_KEYWORD_RE.search(lowered):
        # Only treat as sensitive when it looks like a credential container
        # (or has no extension, e.g. ``id_rsa`` / ``credentials``).
        suffix = Path(lowered).suffix
        if not suffix or lowered.endswith(_EXPORT_CREDENTIAL_CONTAINER_SUFFIXES):
            return True

    return False


def _strip_export_backup_suffixes(name: str) -> str:
    """Return the underlying lowercase name after known backup suffixes."""
    underlying_name = name.lower()
    while backup_match := _EXPORT_BACKUP_NAME_RE.fullmatch(underlying_name):
        underlying_name = backup_match.group("base")
    return underlying_name


def _strip_export_directory_backup_suffixes(name: str) -> str:
    """Return a normalized lowercase export path component."""
    underlying_name = name.lower()
    while backup_match := _EXPORT_DIRECTORY_BACKUP_NAME_RE.fullmatch(
        underlying_name
    ):
        underlying_name = backup_match.group("base")
    return underlying_name


def _normalized_export_relative_parts(
    directory: str,
    name: str,
    profile_root: Optional[Path],
) -> tuple[str, ...]:
    """Return normalized, root-relative parts for an export entry."""
    if profile_root is None:
        return ()
    try:
        relative = (Path(directory) / name).relative_to(profile_root)
    except ValueError:
        return ()
    return tuple(
        _strip_export_directory_backup_suffixes(part)
        for part in relative.parts
    )


def _is_sensitive_profile_credential_tree_entry(
    directory: str,
    name: str,
    profile_root: Optional[Path],
) -> bool:
    """Return True for canonical credential-directory trees in a profile."""
    relative_parts = _normalized_export_relative_parts(
        directory, name, profile_root
    )
    return any(
        relative_parts[: len(prefix)] == prefix
        for prefix in _EXPORT_SENSITIVE_PROFILE_DIR_PREFIXES
    )


def _is_sensitive_profile_home_entry(
    directory: str,
    name: str,
    profile_root: Optional[Path],
) -> bool:
    """Return True for credential trees inside a profile's ``home/``.

    The check is root-relative so an unrelated directory named ``.ssh`` in a
    skill or workspace is not removed. Backup-renamed path components are
    normalized so ``home/.config/gh.bak/`` and ``home/.ssh.20260101/`` cannot
    bypass the directory policy.
    """
    relative_parts = _normalized_export_relative_parts(
        directory, name, profile_root
    )
    if len(relative_parts) < 2 or relative_parts[0] != "home":
        return False

    home_parts = relative_parts[1:]
    return any(
        home_parts[: len(prefix)] == prefix
        for prefix in _EXPORT_SENSITIVE_PROFILE_HOME_DIR_PREFIXES
    )


def _is_sensitive_export_entry(
    directory: str,
    name: str,
    profile_root: Optional[Path] = None,
) -> bool:
    """Return True when a copytree entry must be excluded from an export.

    Most decisions are basename-only. Ambiguous ``.pem`` files and their
    recognized backup variants are stream-scanned for a private-key header so
    public certificates and CA bundles remain portable while PEM-encoded
    private keys stay out of shared archives. Profile-local HOME credential
    trees are matched relative to ``profile_root`` so ordinary same-named
    project directories remain portable.
    """
    if _is_sensitive_profile_credential_tree_entry(
        directory, name, profile_root
    ):
        return True

    if _is_sensitive_profile_home_entry(directory, name, profile_root):
        return True

    path = Path(directory) / name
    if not path.is_dir() and _is_sensitive_export_name(name):
        return True

    underlying_name = _strip_export_backup_suffixes(name)
    if not underlying_name.endswith(".pem"):
        return False

    try:
        if path.is_symlink() or not path.is_file():
            return False
        marker = b"PRIVATE KEY-----"
        overlap = b""
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                data = overlap + chunk.upper()
                if marker in data:
                    return True
                overlap = data[-(len(marker) - 1):]
    except OSError:
        # copytree will surface unreadable entries itself; this helper should
        # not turn an ordinary I/O error into a silent exclusion.
        return False
    return False


def _reject_profile_export_symlinks(root: Path) -> None:
    """Fail before export can preserve or dereference a profile symlink."""
    if root.is_symlink():
        raise ValueError("Refusing profile export symlink: .")

    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            path = Path(directory) / name
            if path.is_symlink():
                relative = path.relative_to(root).as_posix()
                raise ValueError(f"Refusing profile export symlink: {relative}")


def _default_export_ignore(root_dir: Path):
    """Return an *ignore* callable for :func:`shutil.copytree`.

    Three-tier filtering:

    * **Root-level allow-list** — only entries whose name appears in
      ``_DEFAULT_EXPORT_INCLUDE_ROOT`` survive. Everything else (such as
      an unrelated ``x11-dev/`` directory in a Docker deployment where
      HERMES_HOME equals the cwd) is excluded. Blacklisting was tried
      first and proved unable to anticipate every non-Hermes file the
      user may have lying alongside HERMES_HOME (#58394).
    * **Sensitive components at any depth** — credential files, backups, and
      credential-directory trees identified by
      :func:`_is_sensitive_export_entry`.
    * **Universal exclusions at any depth** — ``__pycache__``, sockets, temp
      files, and transient SQLite sidecars; plus npm lockfiles, which may
      appear at the root.

    Surviving allow-listed profile artifacts are copied into the staged tree,
    where text files are force-redacted by :func:`_scrub_export_secrets` before
    the archive is written.
    """

    def _ignore(directory: str, contents: list) -> set:
        ignored: set = set()
        sqlite_sidecars = _sqlite_sidecars_in_directory(directory, contents)
        for entry in contents:
            # Universal exclusions (any depth)
            if entry in sqlite_sidecars or _is_transient_export_name(entry):
                ignored.add(entry)
            # npm lockfiles can appear at root
            elif entry in {"package.json", "package-lock.json"}:
                ignored.add(entry)
            # Credentials, backups, and credential trees (any depth)
            elif _is_sensitive_export_entry(directory, entry, root_dir):
                ignored.add(entry)
        # Root-level allow-list: drop everything that isn't a known Hermes
        # profile artifact.
        if Path(directory) == root_dir:
            ignored.update(
                entry for entry in contents if entry not in _DEFAULT_EXPORT_INCLUDE_ROOT
            )
        return ignored

    return _ignore


def _make_profile_archive(base: str, root_dir: str, base_dir: str) -> str:
    """Atomically create ``<base>.tar.gz`` — GNU tar format.

    Not :func:`shutil.make_archive`: that writes PAX (Python's tarfile default
    since 3.8), whose fractional-mtime records macOS Archive Utility rejects —
    double-clicking an exported profile threw "Error 94 - Bad message." GNU
    format keeps long paths working (longlink extensions) and stays integer-
    mtime, so Finder, bsdtar, and gnutar all extract it.
    """
    import tarfile
    import tempfile

    archive_path = Path(f"{base}.tar.gz")
    if not archive_path.is_symlink() and archive_path.is_dir():
        raise IsADirectoryError(f"Profile export output is a directory: {archive_path}")

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{archive_path.name}.",
            suffix=".tmp",
            dir=archive_path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            with tarfile.open(
                fileobj=handle,
                mode="w:gz",
                format=tarfile.GNU_FORMAT,
            ) as tf:
                tf.add(str(Path(root_dir) / base_dir), arcname=base_dir)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, archive_path)
        temporary_path = None
        return str(archive_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _safe_copy_export_sqlite_database(source: Path, destination: Path) -> None:
    """Create a URI-safe, transactionally consistent SQLite snapshot."""
    import sqlite3

    source_connection = None
    destination_connection = None
    snapshot_error = None
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        source_connection = sqlite3.connect(source_uri, uri=True)
        destination_connection = sqlite3.connect(destination)
        source_connection.backup(destination_connection)
    except Exception as exc:
        snapshot_error = exc
    finally:
        for connection in (destination_connection, source_connection):
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    if snapshot_error is not None:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"Could not create a consistent SQLite export snapshot: {source.name}"
        ) from snapshot_error


def _iter_sqlite_secret_text_views(data: bytes) -> Iterator[str]:
    """Yield bounded text views that can expose ASCII or UTF-16 secrets."""
    yield data.decode("utf-8", errors="surrogateescape")
    for codec in ("utf-16-le", "utf-16-be"):
        for offset in (0, 1):
            encoded = data[offset:]
            encoded = encoded[: len(encoded) - (len(encoded) % 2)]
            if encoded:
                yield encoded.decode(codec, errors="surrogatepass")


def _redact_profile_export_text(text: str) -> str:
    """Apply strict redaction for a shareable, non-navigation boundary."""
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(
        text,
        force=True,
        redact_url_credentials=True,
    )


def _profile_export_text_contains_secret(text: str) -> bool:
    return _redact_profile_export_text(text) != text


def _profile_export_bytes_contain_secret(data: bytes) -> bool:
    from agent.redact import has_sensitive_text_hint

    for text in _iter_sqlite_secret_text_views(data):
        if has_sensitive_text_hint(text) and _profile_export_text_contains_secret(text):
            return True
    return False


_EXPORT_FILE_SCAN_CHUNK_BYTES = 64 * 1024
_EXPORT_BINARY_SCAN_OVERLAP_BYTES = 64 * 1024
_EXPORT_MAX_TEXT_RECORD_CHARS = 8 * 1024 * 1024


def _is_streaming_utf8_text(path: Path) -> bool:
    """Validate UTF-8 incrementally while treating NUL-bearing files as binary."""
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_EXPORT_FILE_SCAN_CHUNK_BYTES):
                if b"\x00" in chunk:
                    return False
                decoder.decode(chunk, final=False)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _binary_export_file_contains_secret(path: Path) -> bool:
    """Scan an encoded or binary file with bounded memory and overlap."""
    overlap = b""
    with path.open("rb") as handle:
        while chunk := handle.read(_EXPORT_FILE_SCAN_CHUNK_BYTES):
            window = overlap + chunk
            if _profile_export_bytes_contain_secret(window):
                return True
            overlap = window[-_EXPORT_BINARY_SCAN_OVERLAP_BYTES:]
    return False


def _redact_export_text_record(record: str, relative: str) -> str:
    if len(record) > _EXPORT_MAX_TEXT_RECORD_CHARS:
        raise ValueError(
            "Refusing profile export because a text record is too large to "
            f"inspect safely: {relative}"
        )
    upper = record.upper()
    if "-----BEGIN" in upper and "PRIVATE KEY-----" in upper:
        raise ValueError(
            f"Refusing profile export because text contains a private key: {relative}"
        )
    return _redact_profile_export_text(record)


def _stream_scrub_utf8_export_file(path: Path, relative: str) -> None:
    """Redact a validated UTF-8 file record-by-record with bounded memory."""
    import stat
    import tempfile

    original_mode = stat.S_IMODE(path.stat().st_mode)
    temporary_path = None
    changed = False
    carry = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            suffix=".scrub",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            with path.open("r", encoding="utf-8", newline="") as source:
                while chunk := source.read(_EXPORT_FILE_SCAN_CHUNK_BYTES):
                    carry += chunk
                    records = carry.splitlines(keepends=True)
                    if records and not records[-1].endswith(("\n", "\r")):
                        incomplete = records.pop()
                    else:
                        incomplete = ""
                    if len(incomplete) > _EXPORT_MAX_TEXT_RECORD_CHARS:
                        raise ValueError(
                            "Refusing profile export because a text record is too "
                            f"large to inspect safely: {relative}"
                        )
                    # Keep one complete record beside the next chunk so a
                    # control-split witness crossing a read boundary remains
                    # contiguous, but redact the rest as one batch rather than
                    # invoking the full redactor once per short log line.
                    carry = (records.pop() if records else "") + incomplete
                    if records:
                        batch = "".join(records)
                        redacted = _redact_export_text_record(batch, relative)
                        changed = changed or redacted != batch
                        output.write(redacted)
                if carry:
                    redacted = _redact_export_text_record(carry, relative)
                    changed = changed or redacted != carry
                    output.write(redacted)
        if changed:
            temporary_path.chmod(original_mode)
            temporary_path.replace(path)
            temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _sqlite_quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_quick_check(connection, schema: str) -> None:
    import sqlite3

    cursor = connection.execute(f"PRAGMA {schema}.quick_check")
    try:
        for result in cursor:
            if result != ("ok",):
                raise sqlite3.DatabaseError(f"SQLite {schema} quick_check failed")
    finally:
        cursor.close()


def _sqlite_schema_rows(connection, schema: str) -> list[tuple]:
    return connection.execute(
        f"SELECT type, name, tbl_name, sql FROM {schema}.sqlite_schema "
        "WHERE type IN ('table', 'index', 'view', 'trigger') "
        "ORDER BY type, name"
    ).fetchall()


def _sqlite_semantic_pragmas(connection, schema: str) -> tuple:
    names = ("user_version", "application_id", "encoding", "auto_vacuum", "page_size")
    return tuple(
        connection.execute(f"PRAGMA {schema}.{name}").fetchone() for name in names
    )


def _sqlite_table_rowid_alias(connection, schema: str, identifier: str) -> Optional[str]:
    import sqlite3

    column_names = {
        str(row[1]).casefold()
        for row in connection.execute(f"PRAGMA {schema}.table_xinfo({identifier})")
    }
    for rowid_alias in ("rowid", "_rowid_", "oid"):
        if rowid_alias.casefold() in column_names:
            continue
        try:
            connection.execute(
                f"SELECT {rowid_alias} FROM {schema}.{identifier} LIMIT 0"
            )
        except sqlite3.OperationalError as exc:
            if "no such column" not in str(exc).casefold():
                raise
            return None
        return rowid_alias
    return None


def _sqlite_exact_value(value) -> tuple[str, object]:
    import struct

    if value is None:
        return ("null", b"")
    if isinstance(value, int):
        return ("integer", str(value).encode("ascii"))
    if isinstance(value, float):
        return ("real", struct.pack(">d", value))
    if isinstance(value, str):
        return ("text", value.encode("utf-8", errors="surrogatepass"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ("blob", bytes(value))
    raise TypeError(f"Unsupported SQLite value type: {type(value).__name__}")


def _sqlite_table_rows_match_exactly(
    connection,
    table_name: str,
    identifier: str,
) -> bool:
    source_rowid = _sqlite_table_rowid_alias(connection, "main", identifier)
    compact_rowid = _sqlite_table_rowid_alias(connection, "compact", identifier)
    if source_rowid != compact_rowid:
        return False

    if source_rowid is not None:
        projection = f"{source_rowid}, *"
        order_by = source_rowid
    else:
        table_info = list(connection.execute(f"PRAGMA main.table_xinfo({identifier})"))
        primary_key = [
            (int(row[5]), _sqlite_quote_identifier(str(row[1])))
            for row in table_info
            if int(row[5]) > 0
        ]
        projection = "*"
        if primary_key:
            primary_key.sort()
            order_by = ", ".join(name for _position, name in primary_key)
        else:
            # A rowid table may legally shadow all three rowid aliases without
            # declaring a primary key. Order by storage type plus SQLite's
            # canonical SQL literal for every visible column so the two
            # databases can still be compared deterministically and exactly.
            columns = [
                _sqlite_quote_identifier(str(row[1]))
                for row in table_info
                if int(row[6]) != 1
            ]
            if not columns:
                raise RuntimeError(
                    f"Cannot determine stable SQLite row order for table: {table_name}"
                )
            order_by = ", ".join(
                expression
                for column in columns
                for expression in (f"typeof({column})", f"quote({column}) COLLATE BINARY")
            )

    source_cursor = connection.execute(
        f"SELECT {projection} FROM main.{identifier} ORDER BY {order_by}"
    )
    compact_cursor = connection.execute(
        f"SELECT {projection} FROM compact.{identifier} ORDER BY {order_by}"
    )
    try:
        while True:
            source_rows = source_cursor.fetchmany(128)
            compact_rows = compact_cursor.fetchmany(128)
            if len(source_rows) != len(compact_rows):
                return False
            if not source_rows:
                return True
            for source_row, compact_row in zip(source_rows, compact_rows):
                if tuple(map(_sqlite_exact_value, source_row)) != tuple(
                    map(_sqlite_exact_value, compact_row)
                ):
                    return False
    finally:
        source_cursor.close()
        compact_cursor.close()


def _verify_compacted_sqlite_semantics(
    snapshot: Path,
    compacted: Path,
    relative: Path,
) -> None:
    """Fail closed if VACUUM INTO changed logical database semantics."""
    import sqlite3

    connection = None
    attached = False
    try:
        uri = f"{snapshot.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("ATTACH DATABASE ? AS compact", (str(compacted),))
        attached = True
        connection.execute("PRAGMA query_only = ON")
        _sqlite_quick_check(connection, "main")
        _sqlite_quick_check(connection, "compact")

        if _sqlite_schema_rows(connection, "main") != _sqlite_schema_rows(
            connection, "compact"
        ):
            raise RuntimeError("schema changed during SQLite export compaction")
        if _sqlite_semantic_pragmas(connection, "main") != _sqlite_semantic_pragmas(
            connection, "compact"
        ):
            raise RuntimeError("pragmas changed during SQLite export compaction")

        source_tables = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM main.sqlite_schema WHERE type = 'table' ORDER BY name"
            )
        ]
        compact_tables = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM compact.sqlite_schema WHERE type = 'table' ORDER BY name"
            )
        ]
        if source_tables != compact_tables:
            raise RuntimeError("tables changed during SQLite export compaction")

        for table_name in source_tables:
            identifier = _sqlite_quote_identifier(table_name)
            if not _sqlite_table_rows_match_exactly(
                connection,
                table_name,
                identifier,
            ):
                raise RuntimeError(
                    "rows changed during SQLite export compaction: "
                    f"{table_name}"
                )
    except (sqlite3.Error, OSError, UnicodeError) as exc:
        raise RuntimeError(
            f"Could not verify compacted SQLite database during profile export: {relative}"
        ) from exc
    finally:
        if connection is not None:
            if attached:
                try:
                    connection.execute("DETACH DATABASE compact")
                except sqlite3.Error:
                    pass
            connection.close()


def _compact_export_sqlite_database(
    snapshot: Path,
    compacted: Path,
    relative: Path,
) -> None:
    """Rebuild a disposable snapshot to remove deleted-page residue safely."""
    import sqlite3

    connection = None
    try:
        connection = sqlite3.connect(snapshot)
        connection.execute("VACUUM INTO ?", (str(compacted),))
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"Could not compact SQLite database during profile export: {relative}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()
    _verify_compacted_sqlite_semantics(snapshot, compacted, relative)


def _sqlite_snapshot_contains_secret(snapshot: Path, relative: Path) -> bool:
    """Inspect logical content in a compacted disposable database."""
    import sqlite3

    connection = None
    try:
        uri = f"{snapshot.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only = ON")

        _sqlite_quick_check(connection, "main")

        schema_cursor = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE type IN ('table', 'index', 'view', 'trigger') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
        try:
            while schema_rows := schema_cursor.fetchmany(128):
                for schema_row in schema_rows:
                    for value in schema_row:
                        if (
                            isinstance(value, str)
                            and _profile_export_text_contains_secret(value)
                        ):
                            return True
        finally:
            schema_cursor.close()

        table_cursor = connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        try:
            while tables := table_cursor.fetchmany(128):
                for (table_name,) in tables:
                    identifier = '"' + table_name.replace('"', '""') + '"'
                    row_cursor = connection.execute(f"SELECT * FROM {identifier}")
                    try:
                        while rows := row_cursor.fetchmany(128):
                            for row in rows:
                                for value in row:
                                    if isinstance(value, str):
                                        if _profile_export_text_contains_secret(value):
                                            return True
                                    elif isinstance(value, (bytes, bytearray, memoryview)):
                                        if _profile_export_bytes_contain_secret(
                                            bytes(value)
                                        ):
                                            return True
                                    else:
                                        continue
                    finally:
                        row_cursor.close()
        finally:
            table_cursor.close()
        return False
    except (sqlite3.Error, OSError, UnicodeError) as exc:
        raise RuntimeError(
            f"Could not safely inspect SQLite database during profile export: {relative}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _snapshot_export_sqlite_databases(source_root: Path, staged_root: Path) -> None:
    """Replace staged SQLite files with compacted, verified live snapshots.

    Export ignores transient WAL/SHM/journal sidecars because they can vanish
    during ``copytree``. Copying only the main database file is not sufficient,
    though: committed rows may still live exclusively in an active WAL. For
    every staged regular file with a SQLite header, use SQLite's backup API,
    rebuild only the disposable snapshot to remove deleted-page residue, verify
    logical semantics and rowids are unchanged, then inspect all live values.
    """
    import stat
    import tempfile

    for staged_db in staged_root.rglob("*"):
        if staged_db.is_symlink() or not staged_db.is_file():
            continue
        try:
            with staged_db.open("rb") as handle:
                if handle.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                    continue
            relative = staged_db.relative_to(staged_root)
            source_db = source_root / relative
            if source_db.is_symlink() or not source_db.is_file():
                raise RuntimeError(
                    f"SQLite source changed during profile export: {relative}"
                )
            original_mode = stat.S_IMODE(staged_db.stat().st_mode)
        except OSError as exc:
            raise RuntimeError(
                f"Could not inspect SQLite database during profile export: {staged_db}"
            ) from exc

        with tempfile.NamedTemporaryFile(
            prefix=f".{staged_db.name}.",
            suffix=".snapshot",
            dir=staged_db.parent,
            delete=False,
        ) as handle:
            snapshot = Path(handle.name)
        with tempfile.NamedTemporaryFile(
            prefix=f".{staged_db.name}.",
            suffix=".compact",
            dir=staged_db.parent,
            delete=False,
        ) as handle:
            compacted = Path(handle.name)
        try:
            try:
                _safe_copy_export_sqlite_database(source_db, snapshot)
            except RuntimeError as exc:
                raise RuntimeError(
                    "Could not safely inspect SQLite database during profile export: "
                    f"{relative}"
                ) from exc
            _compact_export_sqlite_database(snapshot, compacted, relative)
            if _sqlite_snapshot_contains_secret(compacted, relative):
                raise ValueError(
                    "Refusing profile export because SQLite database contains "
                    f"secret-shaped content: {relative}"
                )
            compacted.chmod(original_mode)
            compacted.replace(staged_db)
        finally:
            snapshot.unlink(missing_ok=True)
            compacted.unlink(missing_ok=True)


def _scrub_export_secrets(staged: Path) -> None:
    """Force-redact secret-shaped strings in a staged export tree.

    Same ``agent.redact.redact_sensitive_text(..., force=True)`` pass used by
    ``hermes sessions export --redact``. Runs on the *staged copy only* so the
    live profile is never rewritten. ``force=True`` ignores
    ``security.redact_secrets`` / ``HERMES_REDACT_SECRETS`` — share archives
    must not emit raw keys even when the user has disabled live redaction.

    Every regular file is inspected regardless of filename. Valid UTF-8 text is
    redacted in place. Binary, NUL-bearing, or encoded content is preserved only
    when its byte views contain no secret witness; otherwise export fails closed.
    Header-confirmed SQLite files were already compacted and inspected logically.
    """
    for path in staged.rglob("*"):
        if path.is_symlink():
            relative = path.relative_to(staged).as_posix()
            raise ValueError(f"Refusing profile export symlink: {relative}")
        if not path.is_file():
            continue

        relative = path.relative_to(staged).as_posix()
        try:
            with path.open("rb") as handle:
                if handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER:
                    continue
            if _is_streaming_utf8_text(path):
                _stream_scrub_utf8_export_file(path, relative)
                if _binary_export_file_contains_secret(path):
                    raise ValueError(
                        "Refusing profile export because text contains secret-shaped "
                        f"content that could not be safely redacted: {relative}"
                    )
            elif _binary_export_file_contains_secret(path):
                raise ValueError(
                    "Refusing profile export because encoded or binary file "
                    f"contains secret-shaped content: {relative}"
                )
        except ValueError:
            raise
        except OSError as exc:
            raise RuntimeError(
                f"Could not inspect or scrub profile export file: {relative}"
            ) from exc
