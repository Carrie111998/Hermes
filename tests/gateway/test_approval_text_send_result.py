from types import SimpleNamespace

from gateway.run import _approval_send_succeeded


def test_plaintext_approval_send_result_success_is_delivery():
    ok, error = _approval_send_succeeded(SimpleNamespace(success=True, error=None))

    assert ok is True
    assert error is None


def test_plaintext_approval_send_result_failure_is_notify_failure():
    ok, error = _approval_send_succeeded(
        SimpleNamespace(success=False, error="Signal send failed")
    )

    assert ok is False
    assert error == "Signal send failed"


def test_plaintext_approval_send_result_none_preserves_legacy_success():
    ok, error = _approval_send_succeeded(None)

    assert ok is True
    assert error is None
