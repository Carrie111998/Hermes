"""One-shot workspace confinement regressions."""

from types import SimpleNamespace

import pytest


def test_oneshot_applies_explicit_workspace(tmp_path, monkeypatch):
    from hermes_cli import main

    monkeypatch.chdir(tmp_path.parent)
    args = SimpleNamespace(in_dir=str(tmp_path), no_restore_cwd=False)

    main._apply_oneshot_working_dir(args)

    assert tmp_path.samefile(".")
    assert args.no_restore_cwd is True


def test_oneshot_rejects_missing_workspace(tmp_path, monkeypatch, capsys):
    from hermes_cli import main

    start = tmp_path / "start"
    start.mkdir()
    monkeypatch.chdir(start)
    args = SimpleNamespace(in_dir=str(tmp_path / "missing"), no_restore_cwd=False)

    with pytest.raises(SystemExit) as exc:
        main._apply_oneshot_working_dir(args)

    assert exc.value.code == 1
    assert start.samefile(".")
    assert "--in directory not found" in capsys.readouterr().out
