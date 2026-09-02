"""Regression tests for ACP session cwd sanitization.

The Rabbit R1 node sends a POSIX-style cwd (``/home/yt``) that does not exist
on the host actually running Hermes (e.g. Windows). Pinning it used to make
the terminal tool spawn its probe shells in a bad directory — MSYS bash wedges
during init and every command hangs forever. ``_translate_acp_cwd`` must fall
back to a real directory instead of trusting the client value blindly.
"""

import os
from unittest import mock

from acp_adapter.session import _translate_acp_cwd


def test_nonexistent_posix_cwd_falls_back_to_home():
    # Rabbit R1 sends /home/yt on a Windows host -> must not be pinned.
    home = os.path.expanduser("~")
    with mock.patch("hermes_constants.translate_cwd_for_wsl_backend", side_effect=lambda c: c):
        with mock.patch("acp_adapter.session.os.path.isdir", side_effect=lambda p: p == home):
            result = _translate_acp_cwd("/home/yt")
    assert result == home
    assert os.path.isdir(result)


def test_existing_cwd_passes_through():
    cwd = os.getcwd()
    assert _translate_acp_cwd(cwd) == cwd


def test_falls_back_to_process_cwd_when_home_missing():
    # Neither the client cwd nor home exist -> process cwd must win.
    with mock.patch("hermes_constants.translate_cwd_for_wsl_backend", side_effect=lambda c: c):
        with mock.patch("acp_adapter.session.os.path.isdir", side_effect=lambda p: False):
            assert _translate_acp_cwd("/home/yt") == os.getcwd()
