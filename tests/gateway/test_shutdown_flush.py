"""Tests for gateway/shutdown_flush.py — pending message durability (#72680)."""

import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.shutdown_flush import (
    _serialise_value,
    flush_pending_to_file,
    recover_pending_to_db,
)


def _make_flush_dir(tmp_path: Path) -> Path:
    """Create a temp flush dir and monkeypatch _get_flush_dir to use it."""
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    return flush_dir


def test_flush_writes_string_pending_to_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    pending = {"agent:main:telegram:supergroup:123": "hello world"}
    count = flush_pending_to_file(pending, reason="shutdown")
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["session_key"] == "agent:main:telegram:supergroup:123"
    assert payload["reason"] == "shutdown"
    assert payload["data"]["text"] == "hello world"
    assert ":" not in files[0].name
    assert "telegram" not in files[0].name


def test_flush_writes_message_event_to_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    event = MagicMock()
    event.text = "user message"
    event.session_id = "20260728_120000_abc"
    event.platform = "telegram"
    event.sender_id = "456"
    event.sender_name = "Alice"
    event.reply_to = None
    event.media = None
    event.raw_event = None

    count = flush_pending_to_file({"session_key_1": event}, reason="adapter_shutdown")
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["data"]["text"] == "user message"
    assert payload["data"]["session_id"] == "20260728_120000_abc"


def test_recover_inserts_via_append_message_and_deletes_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    ts = int(time.time())
    # Write a flush file with session_id
    payload = {
        "session_key": "agent:main:telegram:supergroup:123",
        "reason": "shutdown",
        "ts": ts,
        "data": {
            "text": "lost message",
            "session_id": "20260728_120000_abc",
        },
    }
    flush_file = flush_dir / "test_session_123.json"
    flush_file.write_text(json.dumps(payload), encoding="utf-8")

    mock_db = MagicMock()
    count = recover_pending_to_db(mock_db)

    assert count == 1
    mock_db.append_message.assert_called_once_with(
        session_id="20260728_120000_abc",
        role="user",
        content="lost message",
        timestamp=ts,
    )
    assert not flush_file.exists()


def test_serialise_object_with_text():
    obj = MagicMock()
    obj.text = "msg"
    obj.session_id = "sid"
    obj.platform = None
    obj.sender_id = None
    obj.sender_name = None
    obj.reply_to = None
    obj.media = None
    obj.raw_event = None
    result = _serialise_value(obj)
    assert result is not None
    assert result["text"] == "msg"
    assert result["session_id"] == "sid"


def test_get_flush_dir_uses_get_hermes_home(tmp_path, monkeypatch):
    """Flush dir must use get_hermes_home(), not hardcoded Path.home()."""
    import gateway.shutdown_flush as mod

    captured = {}

    def fake_get_hermes_home():
        from pathlib import Path
        captured["called"] = True
        return tmp_path

    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", fake_get_hermes_home
    )
    result = mod._get_flush_dir()
    assert captured.get("called") is True
    assert result == tmp_path / "pending_messages"


# ---------------------------------------------------------------------------
# At-rest permissions for the recovery directory (#72680 follow-up)
#
# ``pending_messages/`` holds *verbatim undelivered user messages*, and the
# agent-history payloads written here are documented as operator-salvaged by
# hand ("so an operator can salvage the conversation after repairing
# state.db"). So the directory is read by a human, not only by the gateway.
#
# Two mechanisms set its mode and they mask each other under a plain
# "is it 0700?" assertion: the ``mode=`` on ``mkdir`` and the ``_secure_dir``
# reconciliation. Each class below isolates one of them.
# ---------------------------------------------------------------------------

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX mode bits; Windows at-rest protection is ACL-based (#77527)",
)

GROUP_OTHER_BITS = (
    stat.S_IRGRP
    | stat.S_IWGRP
    | stat.S_IXGRP
    | stat.S_IROTH
    | stat.S_IWOTH
    | stat.S_IXOTH
)


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def permissive_umask():
    """Force a world-readable-by-default umask for the duration of a test."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A real HERMES_HOME with ``pending_messages`` deliberately absent."""
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Never let the ambient environment silently disable the hardening.
    monkeypatch.delenv("HERMES_CONTAINER", raising=False)
    monkeypatch.delenv("HERMES_SKIP_CHMOD", raising=False)
    monkeypatch.delenv("HERMES_HOME_MODE", raising=False)
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    return home


@pytest.fixture
def neutralized_secure_dir(monkeypatch):
    """Make the post-creation reconciliation step a recording no-op.

    ``_get_flush_dir`` hardens twice over: ``mode=`` on ``mkdir``, then
    ``hermes_cli.config._secure_dir`` to reconcile policy. Either mechanism
    alone satisfies a plain "is it 0700?" assertion, so the two mask each
    other and such a test cannot tell which one did the work — deleting
    ``mode=`` keeps it green.

    Stubbing the reconciler out isolates the creation mode as the *only*
    thing that can produce the observed bits. ``_secure_dir`` is patched on
    ``hermes_cli.config`` (not on the caller) because the production code
    imports it lazily inside the function, so the attribute is resolved at
    call time.
    """
    import hermes_cli.config as hermes_config

    calls = []

    def _recording_noop(path):
        calls.append(str(path))

    monkeypatch.setattr(hermes_config, "_secure_dir", _recording_noop)
    return calls


@pytest.fixture
def managed_nixos_home(hermes_home, monkeypatch):
    """Simulate a managed NixOS install that shares state via the hermes group.

    Reproduces the two conditions ``nix/nixosModules.nix`` actually creates:

    * ``systemd.tmpfiles`` pins ``stateDir/.hermes`` to ``2770`` — setgid,
      group-rwx — so the gateway and interactive ``hostUsers`` share it;
    * the service runs with ``UMask = "0007"``, commented "files created by
      the gateway should be group-writable so interactive users in the hermes
      group can read/write them".

    ``pending_messages`` is *not* in those tmpfiles rules, so it is created
    lazily at runtime under exactly this umask — which is why the creation
    mode, not just the reconciliation, has to honour the carve-out. Setting
    ``HERMES_MANAGED`` without the umask and the 2770 parent would pass for
    the wrong reason.
    """
    monkeypatch.setenv("HERMES_MANAGED", "nixos")
    os.chmod(hermes_home, 0o2770)
    previous = os.umask(0o007)
    try:
        yield hermes_home
    finally:
        os.umask(previous)


@posix_only
class TestFlushDirUnmanagedPermissions:
    """Unmanaged installs keep the recovery dir owner-only."""

    def test_fresh_dir_has_no_group_or_other_access(
        self, hermes_home, permissive_umask
    ):
        """The real path must not leave undelivered messages world-readable."""
        from gateway.shutdown_flush import _get_flush_dir

        flush_dir = _get_flush_dir()

        assert flush_dir == hermes_home / "pending_messages"
        mode = _mode(flush_dir)
        assert not (mode & GROUP_OTHER_BITS), (
            f"recovery dir {flush_dir} is group/other-accessible: {oct(mode)}"
        )

    def test_mode_is_not_umask_derived(self, hermes_home, permissive_umask):
        """Owner keeps full access; the mode comes from code, not the umask."""
        from gateway.shutdown_flush import _get_flush_dir

        assert _mode(_get_flush_dir()) == 0o700

    def test_creation_mode_alone_is_owner_only(
        self, hermes_home, permissive_umask, neutralized_secure_dir
    ):
        """Teeth for ``mode=`` on mkdir, with the reconciler stubbed out.

        The mkdir->chmod window is what ``mode=`` closes; the race itself is
        not assertable, but "correct with no chmod at all" is the equivalent
        deterministic property.
        """
        from gateway.shutdown_flush import _get_flush_dir

        flush_dir = _get_flush_dir()

        mode = _mode(flush_dir)
        assert mode == 0o700, (
            "creation mode must be owner-only on its own — the mkdir mode is "
            f"what closes the TOCTOU window, got {oct(mode)}"
        )
        assert neutralized_secure_dir == [str(flush_dir)], (
            "unmanaged installs must still reconcile via the shared "
            "hermes_cli.config._secure_dir policy"
        )

    def test_flush_payload_is_owner_only(self, hermes_home, permissive_umask):
        """The payload file itself carries the verbatim message text."""
        from gateway.shutdown_flush import _get_flush_dir, flush_pending_to_file

        assert flush_pending_to_file(
            {"agent:main:telegram:dm:1": "verbatim user text"},
            reason="shutdown",
        ) == 1

        payloads = list(_get_flush_dir().glob("pending-*.json"))
        assert len(payloads) == 1
        mode = _mode(payloads[0])
        assert not (mode & GROUP_OTHER_BITS), (
            f"recovery payload {payloads[0]} is group/other-readable: {oct(mode)}"
        )

    def test_home_mode_hatch_is_honoured(
        self, hermes_home, permissive_umask, monkeypatch
    ):
        """Delegating to ``_secure_dir`` picks up the documented hatch.

        Every other HERMES_HOME subdirectory ``ensure_hermes_home`` creates
        honours ``HERMES_HOME_MODE``; this one now does too rather than
        hard-coding a mode the operator overrode.
        """
        from gateway.shutdown_flush import _get_flush_dir

        monkeypatch.setenv("HERMES_HOME_MODE", "0701")

        assert _mode(_get_flush_dir()) == 0o701


@posix_only
class TestFlushDirManagedPermissions:
    """Managed (NixOS) installs must not have group access revoked."""

    def test_fresh_dir_keeps_group_access(self, managed_nixos_home):
        """The case at issue: dir absent, created lazily under UMask=0007.

        The gateway service and an interactive hermes-group CLI share one
        ``$HERMES_HOME`` at two uids, so a 0700 dir created by whichever ran
        first locks the other out of the recovery directory with EACCES.
        """
        from gateway.shutdown_flush import _get_flush_dir

        flush_dir = _get_flush_dir()

        assert flush_dir.is_dir()
        mode = _mode(flush_dir)
        assert mode & stat.S_IRWXG == stat.S_IRWXG, (
            "managed installs deliberately share this dir with the hermes "
            f"group (UMask=0007, parent 2770); got {oct(mode)}"
        )

    def test_fresh_dir_creation_does_not_hardcode_a_mode(
        self, managed_nixos_home, neutralized_secure_dir
    ):
        """Teeth for the managed branch at the *creation* site.

        With the reconciler stubbed out, the only thing that can produce the
        observed bits is the ``mkdir`` call — so an unconditional
        ``mode=0o700`` fails here even though ``_secure_dir`` would have
        stood down anyway.
        """
        from gateway.shutdown_flush import _get_flush_dir

        mode = _mode(_get_flush_dir())

        assert mode & stat.S_IRWXG == stat.S_IRWXG, (
            "creation must not hardcode a mode in managed mode: the inherited "
            f"setgid + umask should land group-rwx, got {oct(mode)}"
        )

    def test_reconciler_is_not_invoked_in_managed_mode(
        self, managed_nixos_home, neutralized_secure_dir
    ):
        """Managed mode returns before reconciliation, not merely into a no-op."""
        from gateway.shutdown_flush import _get_flush_dir

        _get_flush_dir()

        assert neutralized_secure_dir == [], (
            "managed mode should short-circuit before the reconciler"
        )

    def test_preexisting_dir_mode_is_left_alone(self, managed_nixos_home):
        """A dir the activation script already placed keeps its mode."""
        from gateway.shutdown_flush import _get_flush_dir

        preexisting = managed_nixos_home / "pending_messages"
        preexisting.mkdir()
        os.chmod(preexisting, 0o750)

        assert _mode(_get_flush_dir()) == 0o750

    def test_flush_still_writes_under_managed_mode(self, managed_nixos_home):
        """The carve-out must not cost us the flush itself."""
        from gateway.shutdown_flush import _get_flush_dir, flush_pending_to_file

        assert flush_pending_to_file(
            {"agent:main:telegram:dm:1": "verbatim user text"},
            reason="shutdown",
        ) == 1
        assert len(list(_get_flush_dir().glob("pending-*.json"))) == 1


@posix_only
class TestFlushDirIsResilient:
    """Permission reconciliation must never cost us the messages."""

    def test_flush_survives_a_failing_reconciler(
        self, hermes_home, permissive_umask, monkeypatch
    ):
        """``chmod`` by a non-owner is EPERM on a shared-state install.

        Unguarded, that raised out of ``_get_flush_dir()``, and because every
        caller wraps this module in ``except Exception: pass`` the shutdown
        flush degraded to a silent no-op — losing the very messages it exists
        to save.
        """
        import hermes_cli.config as hermes_config

        def _boom(path):
            raise PermissionError(13, "Operation not permitted")

        monkeypatch.setattr(hermes_config, "_secure_dir", _boom)

        from gateway.shutdown_flush import flush_pending_to_file

        assert flush_pending_to_file(
            {"agent:main:telegram:dm:1": "verbatim user text"},
            reason="shutdown",
        ) == 1
        payloads = list((hermes_home / "pending_messages").glob("pending-*.json"))
        assert len(payloads) == 1, "a chmod EPERM must not lose the flush"

