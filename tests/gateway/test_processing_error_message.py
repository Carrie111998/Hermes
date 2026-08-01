from types import SimpleNamespace

from gateway.platforms.base import _processing_error_content


def test_processing_error_content_uses_opt_in_localized_template_without_leaking_detail():
    config = SimpleNamespace(
        extra={
            "processing_error_message": (
                "Не смог завершить задачу на этапе обработки ({error_type}). "
                "Повторите сообщение или используйте /reset."
            )
        }
    )
    content = _processing_error_content(config, RuntimeError("private filesystem detail"))
    assert "Не смог завершить" in content
    assert "RuntimeError" in content
    assert "private filesystem detail" not in content


def test_processing_error_content_preserves_default_when_not_configured():
    config = SimpleNamespace(extra={})
    content = _processing_error_content(config, ValueError("bad input"))
    assert "Sorry, I encountered an error (ValueError)." in content
    assert "bad input" not in content
    assert "gateway logs" in content
    assert "/reset" in content


def test_processing_error_content_survives_invalid_template():
    config = SimpleNamespace(extra={"processing_error_message": "broken {missing}"})
    content = _processing_error_content(config, KeyError("x"))
    assert "Sorry, I encountered an error" in content
