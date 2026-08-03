"""Tests for utils.atomic_json_write — crash-safe JSON file writes."""

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from utils import atomic_json_write

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission bits; Windows honours only the read-only bit",
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestAtomicJsonWrite:
    """Core atomic write behavior."""

    @posix_only
    def test_mode_overrides_a_pre_existing_permissive_mode(self, tmp_path):
        """``mode=`` must win over the mode an existing target already has.

        This is the *only* case where the argument changes the result, and it
        was untested — which made ``mode=`` deletable everywhere without a
        single failure. Measured behaviour of the four combinations:

        ==========================  ==========  ==========
        target                      no ``mode=``  ``mode=0o600``
        ==========================  ==========  ==========
        fresh file                  0o600       0o600
        existing 0o644              0o644       0o600
        ==========================  ==========  ==========

        On a fresh file the argument is a no-op: ``tempfile.mkstemp`` already
        creates at 0o600 and passing ``mode`` forces ``original_mode = None`` so
        ``_restore_file_mode`` returns early. Every caller that only ever writes
        a brand-new file therefore looks correct with or without it. What the
        argument actually buys is *overwrite*, where the default behaviour is to
        **preserve** the existing mode.

        That path is reachable in production. ``/save`` names its snapshot
        ``hermes_conversation_%Y%m%d_%H%M%S.json`` — second resolution, so two
        saves in the same second hit the same file — and a snapshot left at
        0o644 by a pre-hardening Hermes would otherwise keep 0o644 while
        receiving a fresh full transcript. Asserting it on the helper covers
        every caller that passes a mode, not just this one.
        """
        target = tmp_path / "snapshot.json"
        target.write_text(json.dumps({"old": True}), encoding="utf-8")
        os.chmod(target, 0o644)

        atomic_json_write(target, {"new": True}, mode=0o600)

        assert _mode(target) == 0o600, (
            f"mode= did not override the pre-existing 0o644 ({oct(_mode(target))}); "
            "atomic_json_write preserves the target's mode by default, so "
            "without this the argument has no observable effect on any path"
        )
        assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}

    @posix_only
    def test_existing_mode_is_preserved_when_no_mode_requested(self, tmp_path):
        """The contrast case that makes the test above meaningful.

        Default behaviour is deliberate preservation: ``_preserve_file_mode`` /
        ``_restore_file_mode`` reinstate the target's original bits after the
        replace, so callers that have no opinion don't silently re-tighten a
        file the user widened. Pinning it here is what proves the sibling test
        is observing ``mode=`` and not just ``mkstemp``'s 0o600.
        """
        target = tmp_path / "shared.json"
        target.write_text(json.dumps({"old": True}), encoding="utf-8")
        os.chmod(target, 0o644)

        atomic_json_write(target, {"new": True})

        assert _mode(target) == 0o644, (
            "an existing file's mode must survive a write that requests none"
        )

    @posix_only
    def test_mode_is_applied_verbatim_not_merely_left_at_mkstemp_default(
        self, tmp_path
    ):
        """``mode=`` must set exactly the mode asked for, via a real chmod.

        Needed because ``mode=0o600`` — the value every caller in the tree
        happens to use — is indistinguishable from doing nothing at all. Four
        mechanisms converge on it: ``mkstemp`` creates the temp file at 0o600,
        passing ``mode`` sets ``original_mode = None`` so the preserve/restore
        path is skipped, then ``fchmod`` and the post-replace ``os.chmod`` each
        set it again. Disabling *both* chmod calls still yields 0o600, so the
        sibling tests above cannot distinguish "chmod ran" from "mkstemp's
        default survived".

        0o640 is a mode ``mkstemp`` cannot produce, so only an actual chmod can
        get there. That pins the contract as "the file ends up at ``mode``"
        rather than the much weaker "the file ends up no wider than 0o600",
        which is what the other assertions really prove.
        """
        target = tmp_path / "group_readable.json"

        atomic_json_write(target, {"shared": True}, mode=0o640)

        assert _mode(target) == 0o640, (
            f"requested 0o640 but got {oct(_mode(target))}; mode= is not being "
            "applied by chmod — the observed bits are just mkstemp's 0o600 "
            "default surviving the replace"
        )







    def test_cleans_up_temp_file_on_baseexception(self, tmp_path):
        class SimulatedAbort(BaseException):
            pass

        target = tmp_path / "data.json"
        original = {"preserved": True}
        target.write_text(json.dumps(original), encoding="utf-8")

        with patch("utils.json.dump", side_effect=SimulatedAbort):
            with pytest.raises(SimulatedAbort):
                atomic_json_write(target, {"new": True})

        tmp_files = [f for f in tmp_path.iterdir() if ".tmp" in f.name]
        assert len(tmp_files) == 0
        assert json.loads(target.read_text(encoding="utf-8")) == original




    def test_mode_does_not_crash_without_fchmod(self, tmp_path):
        """Regression: os.fchmod is Unix-only and absent on Windows. Passing a
        mode must not raise AttributeError when fchmod is unavailable.

        Simulates the Windows os module by removing fchmod from the namespace.
        Previously this crashed in `hermes memory setup` while saving the
        Hindsight config with mode=0o600 (GitHub: Windows setup traceback).
        """
        import utils

        target = tmp_path / "secret.json"
        no_fchmod = {k: getattr(os, k) for k in dir(os) if k != "fchmod"}
        fake_os = type("FakeOs", (), no_fchmod)
        assert not hasattr(fake_os, "fchmod")

        with patch.object(utils, "os", fake_os):
            atomic_json_write(target, {"api_key": "secret"}, mode=0o600)

        assert json.loads(target.read_text(encoding="utf-8")) == {"api_key": "secret"}


    def test_concurrent_writes_dont_corrupt(self, tmp_path):
        """Multiple rapid writes should each produce valid JSON."""
        import threading

        target = tmp_path / "concurrent.json"
        errors = []

        def writer(n):
            try:
                atomic_json_write(target, {"writer": n, "data": list(range(100))})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # File should contain valid JSON from one of the writers
        result = json.loads(target.read_text())
        assert "writer" in result
        assert len(result["data"]) == 100
