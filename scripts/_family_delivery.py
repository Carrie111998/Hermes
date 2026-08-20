"""Identity -> Telegram delivery-target resolution for no-agent scripts.

Mirrors ``_google_identities.py``'s pattern (a small, explicit, per-identity
registry/resolver) but for platform delivery targets rather than Google
credentials. Reused by ``scripts/hermes-oauth-expiry-check.py`` so that job
never has to guess, hardcode, or default a reminder to the wrong person's
chat — the same failure mode as this system's real 2026-08-12 cross-person
data-disclosure incident.

Where the data comes from
--------------------------
Every identity's Telegram chat id is recorded in a ``telegram_chat_id``
field in that person's vault Profile.md YAML frontmatter (the ``---``
delimited block at the very top of the file), e.g.:

    ---
    status: canonical
    purpose: ...
    telegram_chat_id: "8758899353"  # read by hermes-oauth-expiry-check.py -- do not rename/remove
    ---

confirmed live for both identities that exist today:
  * jid     -> ``Hermes/Profile/JID Profile.md``
  * zarkash -> ``Hermes/Profile/Family/Zarkash/Zarkash Profile.md``

Why frontmatter instead of the "Platform Identity" markdown table (this
module's original design): the table lives in the file's prose body,
alongside content that gets edited far more often (bios, notes, relationship
history). Frontmatter is a structurally separate block at the top of the
file that normal profile edits have no natural reason to touch, which
meaningfully reduces the chance of an unrelated edit accidentally breaking
this field. The "Platform Identity" table is left in each Profile.md as the
human-readable record for people reading the file — it is no longer what
this module parses.

JID Profile.md's own text says these platform IDs are "the human-readable
mirror" of what's enforced at the gateway level (``config.yaml``'s
``telegram.allow_from``), "not a separate source of truth. If the two ever
disagree, config.yaml's live gateway config wins; flag the mismatch." This
module honors that explicitly: after parsing an id out of frontmatter, it
cross-checks the id is also present in ``config.yaml``'s
``telegram.allow_from`` and raises rather than delivers if the two
disagree, instead of trusting the vault file alone.

Path convention for a NEW identity (generality)
-------------------------------------------------
The **primary/master-user identity's** profile file name is a fixed
convention in this vault — this system has exactly one master user, and
their file has always been named ``JID Profile.md`` — so it is not derived
from the identity string (see ``_PRIMARY_PROFILE_RELATIVE_PATH`` below).
``is_primary_identity()`` (imported from the ``hermes-oauth-expiry-check``
job) already identifies this identity structurally, so no name comparison
is needed to pick this branch.

Every **non-primary (family-member) identity** follows one mechanical rule
that needs zero code changes for a new person:

    Hermes/Profile/Family/<Capitalized identity>/<Capitalized identity> Profile.md

e.g. ``zarkash`` -> ``Family/Zarkash/Zarkash Profile.md``. A new family
member added to ``_google_identities.py`` as, say, ``"aria"`` resolves
automatically to ``Family/Aria/Aria Profile.md`` — matching the exact
folder-naming convention already used for Zarkash — the moment their vault
folder exists with that name, a populated frontmatter block, and a
``telegram_chat_id`` field in it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

# Fixed by vault convention, not derived from the identity string -- see
# module docstring. There is exactly one master-user identity in this
# system by construction (is_primary_identity()'s structural rule already
# identifies it); this is simply where THAT identity's own profile lives.
_PRIMARY_PROFILE_RELATIVE_PATH = Path("Hermes") / "Profile" / "JID Profile.md"

# A real Telegram user/chat id is an integer, optionally negative (group
# chats). Anything else parsed out of the frontmatter field is treated as
# malformed and must fail loudly, not be silently used as a chat id.


def _is_valid_chat_id(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        s = value.strip()
        return bool(s) and (s.lstrip("-").isdigit())
    return False


class DeliveryTargetResolutionError(RuntimeError):
    """Raised whenever a Telegram delivery target cannot be resolved with
    full confidence for a given identity. Callers MUST treat this as
    "cannot deliver to this identity right now" and skip -- never catch
    this and fall back to guessing, defaulting to another identity's
    target, or delivering anyway.
    """


def profile_path_for_identity(identity: str, *, is_primary: bool, vault_root: Path) -> Path:
    """Return the vault Profile.md path for ``identity``.

    ``is_primary`` must come from the SAME structural check
    (``is_primary_identity()``) already used to decide daily-vs-one-time
    reminder behavior -- keeping both decisions driven by the one
    structural signal rather than two independently-maintained rules that
    could drift apart.
    """
    if is_primary:
        return vault_root / _PRIMARY_PROFILE_RELATIVE_PATH
    name = identity.strip().capitalize()
    return vault_root / "Hermes" / "Profile" / "Family" / name / f"{name} Profile.md"


def _extract_frontmatter_block(text: str, *, profile_path: Path) -> str:
    if not text.startswith("---\n"):
        raise DeliveryTargetResolutionError(
            f"{profile_path} has no YAML frontmatter block (must start with "
            "'---') — cannot resolve a delivery target for this identity"
        )
    try:
        end_idx = text.index("\n---\n", 4)
    except ValueError as exc:
        raise DeliveryTargetResolutionError(
            f"{profile_path}'s frontmatter block is not properly closed "
            "(no terminating '---' line found) — cannot resolve a delivery "
            "target for this identity"
        ) from exc
    return text[4:end_idx]


def _parse_telegram_chat_id_from_frontmatter(text: str, *, profile_path: Path) -> str:
    frontmatter_text = _extract_frontmatter_block(text, profile_path=profile_path)
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise DeliveryTargetResolutionError(
            f"{profile_path}'s frontmatter block is not valid YAML: {exc} — "
            "cannot resolve a delivery target for this identity"
        ) from exc
    if not isinstance(data, dict) or "telegram_chat_id" not in data:
        raise DeliveryTargetResolutionError(
            f"{profile_path}'s frontmatter has no 'telegram_chat_id' field "
            "— cannot resolve a delivery target for this identity"
        )
    raw = data["telegram_chat_id"]
    if not _is_valid_chat_id(raw):
        raise DeliveryTargetResolutionError(
            f"{profile_path}'s frontmatter 'telegram_chat_id' field is not "
            f"a valid numeric id ({raw!r}) — cannot resolve a delivery "
            "target for this identity"
        )
    return str(raw).strip()


def _load_telegram_allow_from(hermes_home: Path) -> Optional[list]:
    """Best-effort read of config.yaml's telegram.allow_from, for the
    cross-check below. Returns None (skips the cross-check) rather than
    raising when config.yaml is unreadable/malformed -- the vault
    frontmatter parse is still the primary signal; this is defense-in-depth,
    not a hard dependency."""
    config_path = hermes_home / "config.yaml"
    if not config_path.is_file():
        return None
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        allow_from = ((data.get("telegram") or {}).get("allow_from")) or None
        if allow_from is None:
            return None
        return [str(v) for v in allow_from]
    except Exception:
        return None


def resolve_telegram_chat_id(
    identity: str, *, is_primary: bool, vault_root: Path, hermes_home: Path
) -> str:
    """Resolve identity's Telegram chat_id from their vault Profile.md's
    YAML frontmatter (``telegram_chat_id`` field).

    Raises :class:`DeliveryTargetResolutionError` — never returns a guessed
    or partial value — when the profile file is missing, the frontmatter
    block is missing/malformed, the ``telegram_chat_id`` field is
    missing/malformed, or (when config.yaml is readable) the resolved id
    disagrees with ``telegram.allow_from`` — per JID Profile.md's own stated
    policy that config.yaml's live gateway config wins on any disagreement
    and a mismatch must be flagged, not silently trusted from the vault
    alone.
    """
    profile_path = profile_path_for_identity(identity, is_primary=is_primary, vault_root=vault_root)
    if not profile_path.is_file():
        raise DeliveryTargetResolutionError(
            f"no Profile.md found for identity={identity!r} at {profile_path} "
            "— cannot resolve a delivery target for this identity"
        )
    try:
        text = profile_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise DeliveryTargetResolutionError(
            f"could not read {profile_path} for identity={identity!r}: {exc}"
        ) from exc

    chat_id = _parse_telegram_chat_id_from_frontmatter(text, profile_path=profile_path)

    allow_from = _load_telegram_allow_from(hermes_home)
    if allow_from is not None and chat_id not in allow_from:
        raise DeliveryTargetResolutionError(
            f"identity={identity!r}: Telegram id {chat_id!r} parsed from "
            f"{profile_path} is NOT present in config.yaml's "
            "telegram.allow_from — per this vault's own documented policy "
            "(config.yaml wins on disagreement), refusing to deliver rather "
            "than trust a possibly-stale vault entry. Flag this mismatch."
        )

    return chat_id
