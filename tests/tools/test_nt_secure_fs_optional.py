import builtins
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest


def test_optional_backend_fails_closed_when_native_module_is_absent():
    module_path = (
        Path(__file__).resolve().parents[2] / "tools" / "nt_secure_fs_optional.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_nt_secure_fs_optional_without_backend",
        module_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    original_import = builtins.__import__

    def without_native_backend(name, *args, **kwargs):
        if name == "tools.nt_secure_fs":
            exc = ModuleNotFoundError("simulated optional backend absence")
            exc.name = name
            raise exc
        return original_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=without_native_backend):
        spec.loader.exec_module(module)

    assert module.is_available() is False
    with pytest.raises(
        OSError, match="secure Windows skill filesystem backend is not installed"
    ):
        module.open_directory("C:/skills", writable=False)
