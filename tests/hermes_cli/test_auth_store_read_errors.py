import errno
import json
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import auth


def test_load_auth_store_propagates_transient_read_errors(tmp_path):
    auth_file = tmp_path / "auth.json"
    original = {"version": 1, "providers": {"nous": {"access_token": "tok"}}}
    auth_file.write_text(json.dumps(original), encoding="utf-8")

    with mock.patch.object(
        Path,
        "read_text",
        side_effect=OSError(errno.EMFILE, "Too many open files"),
    ):
        with pytest.raises(OSError):
            auth._load_auth_store(auth_file)

    assert json.loads(auth_file.read_text(encoding="utf-8")) == original
    assert not auth_file.with_suffix(".json.corrupt").exists()


def test_load_auth_store_preserves_corrupt_json_before_empty_store(tmp_path):
    auth_file = tmp_path / "auth.json"
    raw = '{"version": 1, "providers": {'
    auth_file.write_text(raw, encoding="utf-8")

    store = auth._load_auth_store(auth_file)

    assert store == {"version": auth.AUTH_STORE_VERSION, "providers": {}}
    corrupt_file = auth_file.with_suffix(".json.corrupt")
    assert corrupt_file.read_text(encoding="utf-8") == raw
