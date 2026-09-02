"""Kanban skill revision pins (#101341).

``task.skills`` stays backward compatible with bare name strings. Structured
entries may pin an expected package digest and/or frontmatter version:

    {"name": "release-policy", "expected_digest": "sha256:abcd...", "source_policy": "assignee"}

Preflight resolves through the same runtime loader the worker uses, hashes the
full skill package via ``tools.skills_guard.content_hash``, and reports
mismatches without mutating profile skill trees.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

VALID_SOURCE_POLICIES = frozenset({"assignee", "shared", "task"})
_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{8,64}$")
_CLI_DIGEST_SPLIT = re.compile(r"^(.+?)@(sha256:[0-9a-fA-F]{8,64})$", re.IGNORECASE)
_CLI_VERSION_SPLIT = re.compile(r"^(.+?)@version:(.+)$", re.IGNORECASE)


def parse_skill_cli_token(raw: str) -> str | dict[str, Any]:
    """Parse a ``--skill`` CLI token into a stored skill entry.

    Forms:
      * ``name``
      * ``name@sha256:<hex>``
      * ``name@version:<semver>``
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("skill name cannot be empty")
    digest_match = _CLI_DIGEST_SPLIT.match(text)
    if digest_match:
        return {
            "name": digest_match.group(1).strip(),
            "expected_digest": digest_match.group(2).lower(),
            "source_policy": "assignee",
        }
    version_match = _CLI_VERSION_SPLIT.match(text)
    if version_match:
        return {
            "name": version_match.group(1).strip(),
            "expected_version": version_match.group(2).strip(),
            "source_policy": "assignee",
        }
    return text


def normalize_skill_entry(raw: Any) -> str | dict[str, Any]:
    """Normalize one create-time skill value to a storable entry."""
    if raw is None:
        raise ValueError("skill entry cannot be empty")
    if isinstance(raw, str):
        return parse_skill_cli_token(raw)
    if isinstance(raw, dict):
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"structured skill entry missing name: {raw!r}")
        if "," in name:
            raise ValueError(
                f"skill name cannot contain comma: {name!r} "
                f"(pass a list of separate names instead of a comma-joined string)"
            )
        entry: dict[str, Any] = {"name": name}
        digest = raw.get("expected_digest") or raw.get("digest")
        if digest is not None and str(digest).strip():
            digest_s = str(digest).strip().lower()
            if not _DIGEST_RE.match(digest_s):
                raise ValueError(
                    f"expected_digest must look like sha256:<hex>, got {digest!r}"
                )
            entry["expected_digest"] = digest_s
        version = raw.get("expected_version") or raw.get("version")
        if version is not None and str(version).strip():
            entry["expected_version"] = str(version).strip()
        policy = raw.get("source_policy")
        if policy is not None and str(policy).strip():
            policy_s = str(policy).strip().lower()
            if policy_s not in VALID_SOURCE_POLICIES:
                raise ValueError(
                    f"source_policy must be one of {sorted(VALID_SOURCE_POLICIES)}, "
                    f"got {policy!r}"
                )
            entry["source_policy"] = policy_s
        elif "expected_digest" in entry or "expected_version" in entry:
            entry["source_policy"] = "assignee"
        if len(entry) == 1:
            return name
        return entry
    raise ValueError(
        f"skill entry must be a string or object, got {type(raw).__name__}"
    )


def normalize_skills_list(skills: Optional[Iterable[Any]]) -> Optional[list[Any]]:
    """Validate/dedupe a skills iterable. Preserves first-seen name order."""
    if skills is None:
        return None
    cleaned: list[Any] = []
    seen: set[str] = set()
    for raw in skills:
        if raw is None or raw is False:
            continue
        entry = normalize_skill_entry(raw)
        name = skill_ref_name(entry)
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(entry)
    return cleaned


def skill_ref_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("name") or "").strip()
    return str(entry or "").strip()


def skill_names_for_cli(skills: Optional[Iterable[Any]]) -> list[str]:
    """Bare names for ``hermes --skills`` argv pairs."""
    if not skills:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for entry in skills:
        name = skill_ref_name(entry)
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def skill_entry_has_pin(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("expected_digest") or entry.get("expected_version"))


def pinned_skill_entries(skills: Optional[Iterable[Any]]) -> list[dict[str, Any]]:
    if not skills:
        return []
    return [e for e in skills if skill_entry_has_pin(e)]


def skill_pins_env_payload(skills: Optional[Iterable[Any]]) -> str:
    """JSON for ``HERMES_KANBAN_SKILL_PINS`` (pinned entries only)."""
    pins = pinned_skill_entries(skills)
    return json.dumps(pins, ensure_ascii=False) if pins else ""


def _frontmatter_version(skill_dir: Optional[Path], loaded: dict[str, Any]) -> Optional[str]:
    raw = loaded.get("raw_content") or loaded.get("content") or ""
    if skill_dir is not None:
        skill_md = skill_dir / "SKILL.md"
        if skill_md.is_file():
            try:
                raw = skill_md.read_text(encoding="utf-8-sig", errors="replace")
            except Exception:
                pass
    if not raw:
        return None
    try:
        from agent.skill_utils import parse_frontmatter

        fm, _ = parse_frontmatter(str(raw))
    except Exception:
        return None
    version = fm.get("version")
    if version is None:
        return None
    text = str(version).strip()
    return text or None


def resolve_skill_identity(
    name: str,
    hermes_home: str | Path,
    *,
    task_id: str | None = None,
) -> Optional[dict[str, Any]]:
    """Resolve ``name`` under ``hermes_home`` and return identity metadata.

    Uses the same ``_load_skill_payload`` / ``skill_view`` path as preload so
    disabled, ambiguous, external, and plugin-qualified skills behave the same.
    """
    name = (name or "").strip()
    if not name:
        return None
    home = str(hermes_home)
    previous = os.environ.get("HERMES_HOME")
    try:
        os.environ["HERMES_HOME"] = home
        # skill_utils caches disabled names / dirs against the process env.
        try:
            from agent import skill_utils

            for attr in (
                "_disabled_skill_names_cache",
                "_skills_dir_cache",
                "_external_dirs_cache",
            ):
                if hasattr(skill_utils, attr):
                    setattr(skill_utils, attr, None)
        except Exception:
            pass
        try:
            from tools import skills_tool

            if hasattr(skills_tool, "_skills_dir_cache"):
                skills_tool._skills_dir_cache = None
        except Exception:
            pass

        from agent.skill_commands import _load_skill_payload
        from agent.skill_utils import get_disabled_skill_names

        disabled = set()
        try:
            disabled = get_disabled_skill_names()
        except Exception:
            disabled = set()

        loaded = _load_skill_payload(name, task_id=task_id)
        if not loaded:
            return None
        payload, skill_dir, display_name = loaded
        if display_name in disabled or name in disabled:
            return None

        digest = None
        if skill_dir is not None and Path(skill_dir).is_dir():
            from tools.skills_guard import content_hash

            digest = content_hash(Path(skill_dir))
        elif skill_dir is None:
            # Legacy flat .md skill — hash the single file bytes + name.
            src = payload.get("_source_path") or payload.get("path")
            if src:
                src_path = Path(str(src))
                if not src_path.is_absolute():
                    src_path = Path(home) / "skills" / src_path
                if src_path.is_file():
                    import hashlib

                    data = src_path.read_bytes()
                    digest = "sha256:" + hashlib.sha256(
                        f"{src_path.name}\0".encode() + data
                    ).hexdigest()[:16]

        version = _frontmatter_version(
            Path(skill_dir) if skill_dir else None, payload
        )
        return {
            "name": display_name,
            "requested_name": name,
            "skill_dir": str(skill_dir) if skill_dir else None,
            "digest": digest,
            "version": version,
            "source": str(skill_dir) if skill_dir else payload.get("path"),
        }
    finally:
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous


def _digests_match(expected: str, actual: Optional[str]) -> bool:
    if not actual:
        return False
    exp = expected.strip().lower()
    act = actual.strip().lower()
    if not exp.startswith("sha256:") or not act.startswith("sha256:"):
        return exp == act
    # content_hash truncates to 16 hex chars; accept longer expected digests
    # when they share the same prefix the runtime emits.
    exp_hex = exp[len("sha256:") :]
    act_hex = act[len("sha256:") :]
    n = min(len(exp_hex), len(act_hex))
    return n >= 8 and exp_hex[:n] == act_hex[:n]


def resolve_pin_home(
    *,
    source_policy: str,
    assignee_home: str | Path,
    shared_home: str | Path | None = None,
) -> Path:
    policy = (source_policy or "assignee").strip().lower()
    if policy in ("assignee", "task"):
        # task-scoped immutable snapshots land later (#33245). Until then the
        # digest itself is the identity and we resolve under the assignee so
        # profile isolation is preserved (no implicit default-profile copy).
        return Path(assignee_home)
    if policy == "shared":
        if shared_home is not None:
            return Path(shared_home)
        try:
            from hermes_constants import get_hermes_home

            return Path(get_hermes_home())
        except Exception:
            return Path(assignee_home)
    return Path(assignee_home)


def check_skill_pins(
    skills: Optional[Iterable[Any]],
    *,
    assignee_home: str | Path,
    shared_home: str | Path | None = None,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return mismatch/unresolved records for pinned skill entries.

    Name-only entries are ignored (legacy behavior). Empty list means pass.
    """
    failures: list[dict[str, Any]] = []
    for entry in pinned_skill_entries(skills):
        name = skill_ref_name(entry)
        policy = str(entry.get("source_policy") or "assignee")
        home = resolve_pin_home(
            source_policy=policy,
            assignee_home=assignee_home,
            shared_home=shared_home,
        )
        identity = resolve_skill_identity(name, home, task_id=task_id)
        record = {
            "name": name,
            "source_policy": policy,
            "requested_digest": entry.get("expected_digest"),
            "requested_version": entry.get("expected_version"),
            "actual_digest": identity.get("digest") if identity else None,
            "actual_version": identity.get("version") if identity else None,
            "actual_source": identity.get("source") if identity else None,
            "resolved_home": str(home),
        }
        if identity is None:
            record["reason"] = "unresolved"
            failures.append(record)
            continue
        expected_digest = entry.get("expected_digest")
        if expected_digest and not _digests_match(expected_digest, identity.get("digest")):
            record["reason"] = "digest_mismatch"
            failures.append(record)
            continue
        expected_version = entry.get("expected_version")
        if expected_version:
            actual_version = identity.get("version")
            if actual_version is None or str(actual_version) != str(expected_version):
                record["reason"] = "version_mismatch"
            failures.append(record)
            continue
    return failures


def pin_skills_with_home_digests(
    skills: Optional[Iterable[Any]],
    hermes_home: str | Path,
) -> Optional[list[Any]]:
    """Attach ``expected_digest`` from ``hermes_home`` resolution for each entry."""
    normalized = normalize_skills_list(skills)
    if not normalized:
        return normalized
    out: list[Any] = []
    for entry in normalized:
        name = skill_ref_name(entry)
        identity = resolve_skill_identity(name, hermes_home)
        if identity is None or not identity.get("digest"):
            raise ValueError(
                f"cannot pin digest for unresolved skill {name!r} under {hermes_home}"
            )
        if isinstance(entry, dict):
            pinned = dict(entry)
        else:
            pinned = {"name": name, "source_policy": "assignee"}
        pinned["expected_digest"] = identity["digest"]
        if identity.get("version") and "expected_version" not in pinned:
            pinned["expected_version"] = identity["version"]
        pinned.setdefault("source_policy", "assignee")
        out.append(pinned)
    return out


def format_skill_pin_failures(failures: list[dict[str, Any]]) -> str:
    parts = []
    for f in failures:
        reason = f.get("reason") or "mismatch"
        name = f.get("name")
        if reason == "digest_mismatch":
            parts.append(
                f"{name}: digest {f.get('actual_digest')!r} != "
                f"expected {f.get('requested_digest')!r}"
            )
        elif reason == "version_mismatch":
            parts.append(
                f"{name}: version {f.get('actual_version')!r} != "
                f"expected {f.get('requested_version')!r}"
            )
        else:
            parts.append(f"{name}: unresolved under {f.get('resolved_home')}")
    return "; ".join(parts)


def enforce_env_skill_pins(*, task_id: str | None = None) -> None:
    """Worker-side re-check of ``HERMES_KANBAN_SKILL_PINS`` before model use.

    On mismatch, block the kanban task as ``capability`` (no retry burn) and
    raise ``SystemExit(0)`` so the dispatcher does not count a failure.
    No-op when the env var is unset/empty. Outside a kanban worker, raises
    ``ValueError`` instead of exiting.
    """
    raw = (os.environ.get("HERMES_KANBAN_SKILL_PINS") or "").strip()
    if not raw:
        return
    try:
        pins = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"HERMES_KANBAN_SKILL_PINS is not valid JSON: {exc}") from exc
    if not pins:
        return
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    tid = task_id or os.environ.get("HERMES_KANBAN_TASK")
    failures = check_skill_pins(
        pins,
        assignee_home=home,
        shared_home=home,
        task_id=tid,
    )
    if not failures:
        return
    reason = format_skill_pin_failures(failures)
    blocked = False
    if tid:
        try:
            from hermes_cli import kanban_db as kb

            with kb.connect_closing() as conn:
                with kb.write_txn(conn):
                    kb._append_event(
                        conn,
                        tid,
                        "skill_pin_check",
                        {
                            "ok": False,
                            "phase": "worker_startup",
                            "failures": failures,
                        },
                    )
                kb.block_task(
                    conn,
                    tid,
                    reason=f"skill pin mismatch: {reason}",
                    kind="capability",
                )
            blocked = True
        except Exception:
            blocked = False
    if blocked:
        raise SystemExit(0)
    raise ValueError(f"skill pin mismatch: {reason}")
