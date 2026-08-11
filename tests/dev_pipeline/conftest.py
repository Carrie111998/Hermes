"""Shared fixtures for dev-pipeline executor tests."""

from __future__ import annotations

import pytest


def git_command_success(*_args, **_kwargs):
    return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()


@pytest.fixture
def git_command_ok(monkeypatch):
    from hermes_cli import dev_executor as ex

    monkeypatch.setattr(ex, "git_command", git_command_success)
