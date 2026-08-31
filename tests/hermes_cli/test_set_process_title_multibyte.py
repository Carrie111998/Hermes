"""Regression for #98620: a UnicodeDecodeError from setproctitle must not
abort startup.

setproctitle 1.3.x on Darwin (PS_USE_CLOBBER_ARGV) truncates the argv buffer
one byte short of the title. When the last argv entry ends in a multibyte
character the cut lands mid-sequence, and setproctitle's module-level
``getproctitle()`` decode raises ``UnicodeDecodeError`` at import time — which
used to escape ``_set_process_title``'s ``except ImportError`` guard and kill
``main()`` before the agent started. The title setter is purely cosmetic and
must fall through to the ctypes fallback instead.
"""

import builtins
import sys
import types
from unittest.mock import MagicMock, patch

from hermes_cli import main as hmain


def _unicode_decode_error() -> UnicodeDecodeError:
    return UnicodeDecodeError("utf-8", b"\x80", 0, 1, "unexpected end of data")


def test_import_setproctitle_unicodedecodeerror_does_not_abort():
    """The crash the issue reports: `import setproctitle` itself raises."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "setproctitle":
            raise _unicode_decode_error()
        return real_import(name, *args, **kwargs)

    with patch("ctypes.CDLL", MagicMock()), patch.object(builtins, "__import__", fake_import):
        # Must return None, not propagate UnicodeDecodeError.
        assert hmain._set_process_title() is None


def test_setproctitle_call_unicodedecodeerror_does_not_abort():
    """Same guard when the import succeeds but the call raises."""
    fake_mod = types.SimpleNamespace(
        setproctitle=MagicMock(side_effect=_unicode_decode_error())
    )
    with patch("ctypes.CDLL", MagicMock()), patch.dict(sys.modules, {"setproctitle": fake_mod}):
        assert hmain._set_process_title() is None
    fake_mod.setproctitle.assert_called_once_with("hermes")


def test_setproctitle_success_path_still_used():
    """When setproctitle works, it is used and the ctypes fallback is skipped."""
    fake_mod = types.SimpleNamespace(setproctitle=MagicMock())
    cdll = MagicMock()
    with patch("ctypes.CDLL", cdll), patch.dict(sys.modules, {"setproctitle": fake_mod}):
        assert hmain._set_process_title() is None
    fake_mod.setproctitle.assert_called_once_with("hermes")
    cdll.assert_not_called()
