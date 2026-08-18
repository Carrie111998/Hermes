"""Shared file safety rules used by both tools and ACP shims."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _hermes_home_path() -> Path:
    """Resolve the active HERMES_HOME (profile-aware) without circular imports."""
    try:
        from hermes_constants import get_hermes_home  # local import to avoid cycles
        return get_hermes_home()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def build_credential_denied_paths(home: str) -> set[str]:
    """Return exact paths whose *contents* are credentials.

    This is the credential-bearing subset of the write denylist, split out so
    the read-side deny can reuse it without also denying reads of files that
    are write-protected for non-credential reasons (shell rc files,
    /etc/passwd, ...).  Denying *reads* of those would break ordinary work for
    no credential benefit.
    """
    hermes_home = _hermes_home_path()
    return {
        os.path.realpath(p)
        for p in [
            os.path.join(home, ".ssh", "authorized_keys"),
            os.path.join(home, ".ssh", "id_rsa"),
            os.path.join(home, ".ssh", "id_ed25519"),
            os.path.join(home, ".ssh", "config"),
            str(hermes_home / ".env"),
            os.path.join(home, ".netrc"),
            os.path.join(home, ".pgpass"),
            os.path.join(home, ".npmrc"),
            os.path.join(home, ".pypirc"),
        ]
    }


def build_credential_denied_prefixes(home: str) -> list[str]:
    """Return directory prefixes that are credential stores."""
    return [
        os.path.realpath(p) + os.sep
        for p in [
            os.path.join(home, ".ssh"),
            os.path.join(home, ".aws"),
            os.path.join(home, ".gnupg"),
            os.path.join(home, ".kube"),
            os.path.join(home, ".docker"),
            os.path.join(home, ".azure"),
            os.path.join(home, ".config", "gh"),
        ]
    ]


def build_write_denied_paths(home: str) -> set[str]:
    """Return exact sensitive paths that must never be written.

    Composed of the credential subset plus paths that are write-protected for
    non-credential reasons.  Membership is unchanged from before the split.
    """
    return build_credential_denied_paths(home) | {
        os.path.realpath(p)
        for p in [
            os.path.join(home, ".bashrc"),
            os.path.join(home, ".zshrc"),
            os.path.join(home, ".profile"),
            os.path.join(home, ".bash_profile"),
            os.path.join(home, ".zprofile"),
            "/etc/sudoers",
            "/etc/passwd",
            "/etc/shadow",
        ]
    }


def build_write_denied_prefixes(home: str) -> list[str]:
    """Return sensitive directory prefixes that must never be written."""
    return build_credential_denied_prefixes(home) + [
        os.path.realpath(p) + os.sep
        for p in [
            "/etc/sudoers.d",
            "/etc/systemd",
        ]
    ]


def get_safe_write_root() -> Optional[str]:
    """Return the resolved HERMES_WRITE_SAFE_ROOT path, or None if unset."""
    root = os.getenv("HERMES_WRITE_SAFE_ROOT", "")
    if not root:
        return None
    try:
        return os.path.realpath(os.path.expanduser(root))
    except Exception:
        return None


def is_write_denied(path: str) -> bool:
    """Return True if path is blocked by the write denylist or safe root."""
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(str(path)))

    if resolved in build_write_denied_paths(home):
        return True
    for prefix in build_write_denied_prefixes(home):
        if resolved.startswith(prefix):
            return True

    safe_root = get_safe_write_root()
    if safe_root and not (resolved == safe_root or resolved.startswith(safe_root + os.sep)):
        return True

    return False


# ---------------------------------------------------------------------------
# Credential-file read deny  (Phase 9 / Packet C, layer 1 -- this is a boundary)
# ---------------------------------------------------------------------------
#
# Deliberately pure path logic.  It must NOT depend on the user's redaction
# preference (HERMES_REDACT_SECRETS / security.redact_secrets), on the
# `code_file` argument to redact_sensitive_text, on config, or on import order.
# Redaction is a logging preference; this is a security boundary.
#
# Policy is ADDITIVE ONLY.  HERMES_CREDENTIAL_READ_DENY_EXTRA may add paths.
# There is intentionally no env var or config key that can remove or disable
# this check -- adding one would recreate exactly the _REDACT_ENABLED problem
# this exists to fix.  Do not add a kill switch.

#: Dot-separated basename components that mark a file as a documentation
#: template rather than a live credential store.
CREDENTIAL_TEMPLATE_TOKENS = frozenset({
    "example", "examples",
    "sample", "samples",
    "template", "templates", "tpl", "tmpl",
    "dist", "default", "defaults", "schema",
})


def is_credential_template_basename(basename: str) -> bool:
    """Return True for .env-family files that are templates, not real secrets.

    Matches a token in ANY dot-separated component, not just the last one, so
    both ``.env.example.local`` and ``.env.local.example`` are recognised.
    """
    low = basename.lower()
    if not (low.startswith(".env") or low.startswith("env.")):
        return False
    return any(part in CREDENTIAL_TEMPLATE_TOKENS for part in low.split("."))


def is_credential_basename(basename: str) -> bool:
    """Return True if *basename* names a file whose contents are credentials.

    Template files are explicitly exempt: reading ``.env.example`` to learn
    which keys a project needs is the single most common legitimate ``.env``
    workflow, and blocking it would make the agent materially worse at setup
    tasks for no security gain.
    """
    low = basename.lower()
    if is_credential_template_basename(low):
        return False
    if low == ".env" or low.startswith(".env."):
        return True
    if low == ".envrc":  # direnv; routinely holds `export AWS_SECRET_ACCESS_KEY=`
        return True
    if low.startswith(".credentials"):
        return True
    if low == "credentials" or low.startswith("credentials."):
        return True
    return False


def _extra_credential_denied_paths() -> list[str]:
    """Additional protected paths from HERMES_CREDENTIAL_READ_DENY_EXTRA.

    Colon-separated. Additive only -- this can widen the deny set, never
    narrow it.
    """
    raw = os.getenv("HERMES_CREDENTIAL_READ_DENY_EXTRA", "")
    out = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(os.path.realpath(os.path.expanduser(item)))
        except OSError:
            continue
    return out


def is_credential_read_denied(path: str) -> bool:
    """Return True if *path* names a known credential-bearing file.

    The basename rule is applied to BOTH the literal path and its realpath so
    that a symlink is caught in either direction:
      * ``notes.txt -> .env``  caught via realpath
      * ``.env -> notes.txt``  caught via the literal basename

    Hard links are NOT covered -- realpath resolves symlinks, not hard links,
    and there is no path-level way to detect them. That residual is covered
    only by the layer-2 tripwire, and is pinned by an explicit test.
    """
    raw = os.path.normpath(os.path.expanduser(str(path)))
    try:
        resolved = os.path.realpath(raw)
    except OSError:
        resolved = raw
    home = os.path.realpath(os.path.expanduser("~"))

    if resolved in build_credential_denied_paths(home):
        return True
    for prefix in build_credential_denied_prefixes(home):
        if resolved.startswith(prefix):
            return True
    if is_credential_basename(os.path.basename(raw)):
        return True
    if is_credential_basename(os.path.basename(resolved)):
        return True
    for extra in _extra_credential_denied_paths():
        if resolved == extra or resolved.startswith(extra + os.sep):
            return True
    return False


def get_credential_read_error(path: str) -> Optional[str]:
    """Return a refusal message when a read targets a credential file."""
    if not is_credential_read_denied(path):
        return None
    return (
        f"Access denied: {path} is a known credential-bearing file and its "
        "contents cannot be returned. This is a security boundary, not a "
        "redaction preference, and it cannot be disabled. To rotate a "
        "credential, use scripts/rotate_credential.sh, which reads the "
        "replacement through a hidden TTY prompt so the value never enters "
        "agent context."
    )


def get_read_block_error(path: str) -> Optional[str]:
    """Return an error message when a read targets internal Hermes cache files."""
    resolved = Path(path).expanduser().resolve()
    hermes_home = _hermes_home_path().resolve()
    blocked_dirs = [
        hermes_home / "skills" / ".hub" / "index-cache",
        hermes_home / "skills" / ".hub",
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
    return None
