"""Tests for utils.atomic_json_write — crash-safe JSON file writes."""

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from utils import atomic_json_write


class TestAtomicJsonWrite:
    """Core atomic write behavior."""







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


    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only mode assertion")
    def test_create_mode_is_set_before_replacement(self, tmp_path, monkeypatch):
        """A reader must never observe the temp file's restrictive mode."""
        import utils

        target = tmp_path / "runtime.json"
        events = []
        real_fchmod = os.fchmod
        real_replace = utils.atomic_replace

        def record_fchmod(fd, mode):
            events.append(("fchmod", mode))
            return real_fchmod(fd, mode)

        def record_replace(tmp_path_arg, target_arg):
            events.append(("replace", stat.S_IMODE(Path(tmp_path_arg).stat().st_mode)))
            return real_replace(tmp_path_arg, target_arg)

        monkeypatch.setattr(utils.os, "fchmod", record_fchmod)
        monkeypatch.setattr(utils, "atomic_replace", record_replace)

        atomic_json_write(target, {"ok": True}, create_mode=0o644)

        assert events == [("fchmod", 0o644), ("replace", 0o644)]
        assert stat.S_IMODE(target.stat().st_mode) == 0o644

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only mode assertion")
    def test_create_mode_does_not_change_existing_target_mode(self, tmp_path):
        target = tmp_path / "existing.json"
        target.write_text('{"old": true}', encoding="utf-8")
        target.chmod(0o640)

        atomic_json_write(target, {"new": True}, create_mode=0o644)

        assert stat.S_IMODE(target.stat().st_mode) == 0o640

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
