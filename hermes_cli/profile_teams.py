"""Durable registry for Teams composed of existing Hermes profiles.

A Team is metadata only: it does not create a synthetic profile or own chat
history.  The registry is shared by Desktop/backend processes, so every
read-modify-write mutation is protected by an OS process lock and committed
with an atomic JSON replacement.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence

from utils import atomic_json_write

REGISTRY_VERSION = 1
REGISTRY_FILENAME = "profile_teams.json"
_LOCK_FILENAME = ".profile_teams.lock"
_TEAM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_NAME_LENGTH = 128


class ProfileTeamError(ValueError):
    """Base class for invalid registry operations or data."""


class ProfileTeamRegistryCorruptError(ProfileTeamError):
    """The on-disk registry is malformed or uses an unsupported version."""


# A process lock does not portably guarantee useful same-process thread
# serialization on every OS.  Keep a narrow per-path thread guard as well.
_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.RLock())


@contextmanager
def _process_lock(path: Path) -> Iterator[None]:
    """Take a blocking, cross-platform exclusive lock on one byte."""

    path.parent.mkdir(parents=True, exist_ok=True)
    # msvcrt.locking requires an existing byte and locks from the current file
    # position.  POSIX flock ignores content, so the same setup works there.
    handle = path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _default_registry_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root()) / REGISTRY_FILENAME


def _default_known_profiles() -> set[str]:
    from hermes_cli.profiles import list_profiles

    return {str(profile.name).strip().lower() for profile in list_profiles()}


def _profile_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileTeamError(f"{field} must be a non-empty profile name")
    name = value.strip().lower()
    # Reuse the canonical profile validator so registry membership follows the
    # exact same identifier rules as profile creation.
    from hermes_cli.profiles import validate_profile_name

    try:
        validate_profile_name(name)
    except ValueError as exc:
        raise ProfileTeamError(f"invalid {field}: {exc}") from exc
    return name


def _team_id(value: object) -> str:
    if not isinstance(value, str) or not _TEAM_ID_RE.fullmatch(value):
        raise ProfileTeamError("team id must match [a-z0-9][a-z0-9_-]{0,63}")
    return value


def _team_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileTeamError("team name must be non-empty")
    name = value.strip()
    if len(name) > _MAX_NAME_LENGTH:
        raise ProfileTeamError(f"team name must be at most {_MAX_NAME_LENGTH} characters")
    return name


def _members(value: object) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ProfileTeamError("members must be a list of profile names")
    members = [_profile_id(item, "member") for item in value]
    if len(members) < 2:
        raise ProfileTeamError("a team requires at least two members")
    if len(set(members)) != len(members):
        raise ProfileTeamError("team members must be unique")
    return members


def _normalize_team(
    value: Mapping[str, object],
    *,
    known_profiles: set[str] | None,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ProfileTeamError("team must be an object")
    team_id = _team_id(value.get("id"))
    name = _team_name(value.get("name"))
    lead = _profile_id(value.get("lead"), "lead")
    members = _members(value.get("members"))
    if lead not in members:
        raise ProfileTeamError("team lead must be included in members")
    if known_profiles is not None:
        missing = sorted(set(members) - known_profiles)
        if missing:
            raise ProfileTeamError(f"unknown member profile(s): {', '.join(missing)}")
    return {"id": team_id, "name": name, "lead": lead, "members": members}


class ProfileTeamRegistry:
    """Versioned, process-safe Team registry.

    ``known_profiles`` is injectable for tests and embedding.  It may be an
    iterable or a callable returning the current iterable; mutations resolve it
    while holding the registry lock so profile validation is current at commit.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        known_profiles: Iterable[str] | Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else _default_registry_path()
        self.lock_path = self.path.with_name(_LOCK_FILENAME)
        self._known_profiles = known_profiles

    def _known(self) -> set[str]:
        source = self._known_profiles
        values = source() if callable(source) else source
        if values is None:
            return _default_known_profiles()
        return {_profile_id(value, "known profile") for value in values}

    def _read_unlocked(self) -> dict[str, object]:
        if not self.path.exists():
            return {"version": REGISTRY_VERSION, "teams": []}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProfileTeamRegistryCorruptError(f"could not read Team registry: {exc}") from exc
        if not isinstance(raw, dict):
            raise ProfileTeamRegistryCorruptError("Team registry root must be an object")
        if raw.get("version") != REGISTRY_VERSION:
            raise ProfileTeamRegistryCorruptError(
                f"unsupported Team registry version {raw.get('version')!r}; expected {REGISTRY_VERSION}"
            )
        teams = raw.get("teams")
        if not isinstance(teams, list):
            raise ProfileTeamRegistryCorruptError("Team registry teams must be a list")
        normalized: list[dict[str, object]] = []
        ids: set[str] = set()
        try:
            for team in teams:
                item = _normalize_team(team, known_profiles=None)
                if item["id"] in ids:
                    raise ProfileTeamError(f"duplicate team id {item['id']!r}")
                ids.add(str(item["id"]))
                normalized.append(item)
        except ProfileTeamError as exc:
            raise ProfileTeamRegistryCorruptError(f"invalid Team registry: {exc}") from exc
        return {"version": REGISTRY_VERSION, "teams": normalized}

    def _write_unlocked(self, data: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(self.path, dict(data), mode=0o600)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with _thread_lock(self.lock_path):
            with _process_lock(self.lock_path):
                yield

    def list(self) -> list[dict[str, object]]:
        with self._locked():
            data = self._read_unlocked()
        return [dict(team, members=list(team["members"])) for team in data["teams"]]  # type: ignore[index]

    def get(self, team_id: str) -> dict[str, object] | None:
        wanted = _team_id(team_id)
        for team in self.list():
            if team["id"] == wanted:
                return team
        return None

    def create(self, *, team_id: str, name: str, lead: str, members: Sequence[str]) -> dict[str, object]:
        with self._locked():
            data = self._read_unlocked()
            team = _normalize_team(
                {"id": team_id, "name": name, "lead": lead, "members": members},
                known_profiles=self._known(),
            )
            teams = data["teams"]
            assert isinstance(teams, list)
            if any(existing["id"] == team["id"] for existing in teams):
                raise ProfileTeamError(f"team id {team['id']!r} already exists")
            teams.append(team)
            self._write_unlocked(data)
        return dict(team, members=list(team["members"]))

    def update(self, team_id: str, *, name: str, lead: str, members: Sequence[str]) -> dict[str, object]:
        wanted = _team_id(team_id)
        with self._locked():
            data = self._read_unlocked()
            replacement = _normalize_team(
                {"id": wanted, "name": name, "lead": lead, "members": members},
                known_profiles=self._known(),
            )
            teams = data["teams"]
            assert isinstance(teams, list)
            for index, existing in enumerate(teams):
                if existing["id"] == wanted:
                    teams[index] = replacement
                    self._write_unlocked(data)
                    return dict(replacement, members=list(replacement["members"]))
        raise ProfileTeamError(f"unknown team id {wanted!r}")

    def delete(self, team_id: str) -> bool:
        wanted = _team_id(team_id)
        with self._locked():
            data = self._read_unlocked()
            teams = data["teams"]
            assert isinstance(teams, list)
            remaining = [team for team in teams if team["id"] != wanted]
            if len(remaining) == len(teams):
                return False
            data["teams"] = remaining
            self._write_unlocked(data)
            return True
