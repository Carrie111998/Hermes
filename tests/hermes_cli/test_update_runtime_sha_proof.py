"""The SHA proof must come from the replacement itself (#99450 R2-4).

``_probe_relaunched_runtime_sha`` read ``<profile>/gateway_state.json`` for
every runtime kind. That is wrong three ways:

* ``serve`` and ``dashboard`` backends never write that file, so their
  "verification" could only ever answer ``None`` — a permanently unverifiable
  runtime whose relaunch obligation never discharges;
* the file is keyed by profile, not by process, so a DIFFERENT gateway (the
  one that was never stopped, or a stale record of the one that was) could
  answer for the replacement;
* it was read once, immediately after the relaunch was issued, before any
  replacement could have booted far enough to stamp anything.

The probe is now runtime-kind-correct — the gateway's own state stamp for a
gateway, the replacement's own spawn-ledger registration for a serve or a
dashboard — identifies the replacement by PID, and polls a bounded settle
window. There is still no checkout fallback: an unstamped runtime stays
unverified.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

import hermes_cli.process_identity as pi
import hermes_cli.update_cmd as update_cmd

NEW_SHA = "b" * 40
OLD_SHA = "a" * 40
#: ``ledger_entries()`` live-verifies every row, so a registration standing in
#: for a replacement has to name a process that actually exists. This one does.
LIVE_PID = os.getpid()


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    """Profile homes + a real machine spawn ledger under ``tmp_path``."""
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)

    profiles = tmp_path / "profiles"
    profiles.mkdir()

    def _profile_dir(profile="default"):
        home = profiles / profile
        home.mkdir(parents=True, exist_ok=True)
        return home

    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", _profile_dir)
    # Every PID this suite names is a stand-in; liveness is not what is
    # under test, identity-of-the-stamp is.
    monkeypatch.setattr(update_cmd, "_runtime_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        "hermes_cli.main._runtime_pid_alive", lambda pid: True, raising=False
    )
    return _profile_dir, root / pi.LEDGER_FILENAME


def _stamp_gateway(home: Path, pid: int, sha: str):
    (home / "gateway_state.json").write_text(
        json.dumps({"pid": pid, "code_sha": sha}), encoding="utf-8"
    )


def _stamp_ledger(path: Path, pid: int, sha: str, *, purpose="serve", profile="default"):
    entries = []
    if path.is_file():
        entries = json.loads(path.read_text(encoding="utf-8"))
    entries.append(
        {
            "pid": pid,
            "create_time": None,
            "purpose": purpose,
            "install": pi.install_id(),
            "spawner_pid": None,
            "spawner_create": None,
            "registered_at": time.time(),
            "argv": "hermes serve",
            "profile": profile,
            "code_sha": sha,
        }
    )
    path.write_text(json.dumps(entries), encoding="utf-8")


def _record(kind, *, pid=4242, profile="default"):
    return {"kind": kind, "profile": profile, "pid": pid}


def _probe(record, new_pid=None, *, timeout=5.0):
    return update_cmd._probe_relaunched_runtime_sha(
        record, new_pid, timeout=timeout, poll_interval=0.02
    )


# ---------------------------------------------------------------------------
# serve / dashboard — the kinds that never stamp gateway_state.json
# ---------------------------------------------------------------------------


class TestServeAndDashboardProveTheirOwnSha:
    @pytest.mark.parametrize("kind", ["serve", "dashboard"])
    def test_the_replacement_ledger_registration_is_the_proof(self, homes, kind):
        _profile_dir, ledger = homes
        _stamp_ledger(ledger, LIVE_PID, NEW_SHA, purpose=kind)

        assert _probe(_record(kind), LIVE_PID) == NEW_SHA

    @pytest.mark.parametrize("kind", ["serve", "dashboard"])
    def test_a_gateway_state_file_never_answers_for_them(self, homes, kind):
        """The old behaviour: a co-located gateway's stamp was accepted as
        the serve backend's proof."""
        profile_dir, _ledger = homes
        _stamp_gateway(profile_dir("default"), 9999, NEW_SHA)

        assert _probe(_record(kind), 5151, timeout=0.3) is None

    def test_a_stale_pre_update_registration_is_not_proof(self, homes):
        """The old PID's own row must not verify its replacement."""
        _profile_dir, ledger = homes
        _stamp_ledger(ledger, LIVE_PID, OLD_SHA)

        assert _probe(_record("serve", pid=LIVE_PID), 5151, timeout=0.3) is None

    def test_a_delayed_registration_is_waited_for(self, homes):
        """The replacement registers when it finishes booting, not when the
        respawn call returns."""
        _profile_dir, ledger = homes
        timer = threading.Timer(0.4, _stamp_ledger, args=(ledger, LIVE_PID, NEW_SHA))
        timer.start()
        try:
            assert _probe(_record("serve"), LIVE_PID, timeout=10.0) == NEW_SHA
        finally:
            timer.cancel()

    def test_an_unstamped_replacement_stays_unverified(self, homes):
        _profile_dir, ledger = homes
        _stamp_ledger(ledger, LIVE_PID, "")  # older backend, no self-stamp

        assert _probe(_record("serve"), LIVE_PID, timeout=0.3) is None

    def test_a_supervised_serve_matches_on_kind_and_profile(self, homes):
        """A unit-relaunched serve has no known new PID — the fresh
        registration on its profile is still its own."""
        _profile_dir, ledger = homes
        _stamp_ledger(ledger, LIVE_PID, NEW_SHA, purpose="serve", profile="edge")

        assert _probe(_record("serve", pid=4242, profile="edge"), None) == NEW_SHA

    def test_another_profiles_serve_does_not_answer(self, homes):
        _profile_dir, ledger = homes
        _stamp_ledger(ledger, LIVE_PID, NEW_SHA, purpose="serve", profile="other")

        assert (
            _probe(_record("serve", pid=4242, profile="edge"), None, timeout=0.3)
            is None
        )


# ---------------------------------------------------------------------------
# gateway — the kind that does stamp, but must stamp as the REPLACEMENT
# ---------------------------------------------------------------------------


class TestGatewayProofIdentifiesTheReplacement:
    def test_the_replacements_stamp_is_the_proof(self, homes):
        profile_dir, _ledger = homes
        _stamp_gateway(profile_dir("default"), 5151, NEW_SHA)

        assert _probe(_record("gateway"), 5151) == NEW_SHA

    def test_the_pre_update_stamp_is_not_proof(self, homes):
        """Same profile, same file — but it is still the OLD process's row."""
        profile_dir, _ledger = homes
        _stamp_gateway(profile_dir("default"), 4242, OLD_SHA)

        assert _probe(_record("gateway", pid=4242), 5151, timeout=0.3) is None

    def test_a_different_gateway_does_not_answer(self, homes):
        """A unit relaunch has no known new PID, so the stamp must at least
        not be the old process's."""
        profile_dir, _ledger = homes
        _stamp_gateway(profile_dir("default"), 4242, OLD_SHA)

        assert _probe(_record("gateway", pid=4242), None, timeout=0.3) is None

    def test_a_delayed_stamp_is_waited_for(self, homes):
        profile_dir, _ledger = homes
        home = profile_dir("default")
        timer = threading.Timer(0.4, _stamp_gateway, args=(home, 5151, NEW_SHA))
        timer.start()
        try:
            assert _probe(_record("gateway"), 5151, timeout=10.0) == NEW_SHA
        finally:
            timer.cancel()


# ---------------------------------------------------------------------------
# No checkout fallback, ever
# ---------------------------------------------------------------------------


def test_nothing_stamped_means_unverified_not_the_checkout_sha(homes, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.build_info.get_code_identity",
        lambda refresh=False: {"sha": NEW_SHA, "version": "1.0"},
    )
    assert _probe(_record("serve"), 5151, timeout=0.3) is None
    assert _probe(_record("gateway"), 5151, timeout=0.3) is None


def test_an_unknown_runtime_kind_is_unverified(homes):
    assert _probe(_record("mystery"), 5151, timeout=0.3) is None
