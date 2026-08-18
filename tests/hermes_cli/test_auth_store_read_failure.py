"""A transient read failure on auth.json must not degrade to an empty store.

``_load_auth_store`` treated every exception as corruption and returned
``{"version": ..., "providers": {}}``. This module does read-modify-write in
roughly fifteen places, so an ``OSError`` (EMFILE under fd exhaustion, EACCES,
EIO, a stalled mount) followed by any ``_save_auth_store`` rewrote auth.json
with an empty provider set and destroyed every stored credential.

Genuine corruption still degrades, still preserves a copy, and now only claims
to have preserved one when the copy actually landed.
"""

import errno
import json
import logging
from pathlib import Path

import pytest

import hermes_cli.auth as auth


@pytest.fixture
def store_file(tmp_path):
    f = tmp_path / "auth.json"
    f.write_text(
        json.dumps({"version": 1, "providers": {"nous": {"api_key": "secret"}}}),
        encoding="utf-8",
    )
    return f


def _fail_read(exc):
    def _read(self, *args, **kwargs):
        raise exc
    return _read


@pytest.mark.parametrize(
    "exc",
    [
        OSError(errno.EMFILE, "Too many open files"),
        PermissionError(errno.EACCES, "Permission denied"),
        OSError(errno.EIO, "Input/output error"),
    ],
    ids=["emfile", "eacces", "eio"],
)
def test_read_failure_raises_and_leaves_the_store_alone(store_file, monkeypatch, exc):
    from pathlib import Path

    before = store_file.read_bytes()
    monkeypatch.setattr(Path, "read_text", _fail_read(exc))

    with pytest.raises(OSError):
        auth._load_auth_store(store_file)

    assert store_file.read_bytes() == before, "the store on disk was modified"
    assert not store_file.with_suffix(".json.corrupt").exists(), (
        "a read failure is not corruption and must not write a .corrupt sidecar"
    )


def test_unparseable_json_fails_closed_and_preserves_a_copy(store_file):
    """F10: genuine corruption must NOT degrade to an empty store — the next
    save would overwrite auth.json with an empty provider set and destroy
    every stored credential. Fail closed (raise) instead; the corrupt file is
    preserved for explicit recovery."""
    store_file.write_text("{ not json", encoding="utf-8")

    with pytest.raises(ValueError, match="fail closed"):
        auth._load_auth_store(store_file)

    corrupt = store_file.with_suffix(".json.corrupt")
    assert corrupt.exists(), "genuine corruption must still be preserved"
    assert corrupt.read_text(encoding="utf-8") == "{ not json"


def test_corrupt_copy_is_user_restricted(store_file, monkeypatch):
    """F10/P4: the .corrupt recovery copy inherits the parent directory ACL
    on Windows (copy2 copies bytes + metadata, not the DACL) — it must be
    restricted to the current user exactly like the protected original, or
    removed. A credential-bearing recovery artifact LESS protected than the
    store it preserves is a new exposure introduced by the guard itself.
    Restrict-before-write: restriction is applied to the EMPTY temp, then
    the final artifact verifies restricted."""
    import hermes_constants as hc

    store_file.write_text("{ not json", encoding="utf-8")

    state = {"restricted_temps": []}
    real_restrict = hc.restrict_credential_file

    def _restrict_and_record(path):
        ok = real_restrict(path)
        if ".tmp." in path.name:
            state["restricted_temps"].append(str(path))
        return ok

    monkeypatch.setattr(hc, "restrict_credential_file", _restrict_and_record)

    with pytest.raises(ValueError, match="fail closed"):
        auth._load_auth_store(store_file)

    corrupt = store_file.with_suffix(".json.corrupt")
    assert corrupt.exists(), "corrupt copy must be preserved"
    assert state["restricted_temps"], (
        "a recovery temp must have been restricted before the bytes "
        "were written (restrict-before-write, F10/P4)"
    )
    assert hc.credential_file_is_user_restricted(corrupt), (
        "recovery copy must verify as user-restricted"
    )


def test_corrupt_copy_restriction_precedes_credential_bytes(store_file, monkeypatch):
    """F10/P4 (exact-head re-review): the recovery artifact must be
    restricted BEFORE the corrupt store's bytes are published — the same
    write-before-ACL window F1 eliminated. Witness: at the moment
    restrict_credential_file() runs on the recovery temp, the file must
    contain ZERO bytes. copy2-then-restrict ordering fails this test."""
    import hermes_constants as hc

    store_file.write_text(
        '{"providers": {"nous": {"api_key": "secret-token"} BAD', encoding="utf-8"
    )

    state = {"size_at_restrict": None}

    def _restrict_with_witness(path):
        if path.suffix.endswith(".tmp") or ".tmp." in path.name:
            state["size_at_restrict"] = Path(path).stat().st_size
        return True

    monkeypatch.setattr(hc, "restrict_credential_file", _restrict_with_witness)
    monkeypatch.setattr(hc, "credential_file_is_user_restricted", lambda p: True)

    with pytest.raises(ValueError, match="fail closed"):
        auth._load_auth_store(store_file)

    assert state["size_at_restrict"] == 0, (
        "recovery temp must be EMPTY when the user-only ACL is applied "
        "(restrict-before-write, F10/P4); got "
        f"{state['size_at_restrict']} bytes"
    )

    corrupt = store_file.with_suffix(".json.corrupt")
    assert corrupt.exists(), "corrupt copy must be preserved"


def test_corrupt_copy_removed_when_restriction_fails(store_file, monkeypatch):
    """F10/P4: if the recovery copy cannot be restricted, it must be REMOVED
    rather than left as an unprotected credential artifact."""
    import hermes_constants as hc

    store_file.write_text("{ not json", encoding="utf-8")

    monkeypatch.setattr(hc, "restrict_credential_file", lambda p: False)
    monkeypatch.setattr(hc, "credential_file_is_user_restricted", lambda p: False)

    with pytest.raises(ValueError, match="fail closed"):
        auth._load_auth_store(store_file)

    corrupt = store_file.with_suffix(".json.corrupt")
    assert not corrupt.exists(), (
        "unrestrictable corrupt copy must be removed, not left exposed"
    )


@pytest.mark.parametrize("payload", ["", "   \n\t  "], ids=["zero-byte", "whitespace-only"])
def test_empty_store_file_fails_closed(store_file, payload):
    """F10: an existing zero-byte / whitespace-only store is corruption,
    NOT an initialized-but-empty store. ``> auth.json`` is the canonical
    truncation of an existing file; this module never writes an empty file
    (every write path persists the full JSON envelope atomically), so an
    empty file is indistinguishable from interruption, failed replacement,
    or manual truncation. Fail closed: preserve it for recovery and refuse
    to continue — a later save must never silently replace a damaged
    artifact (that is one read-modify-write away from erasing every stored
    credential)."""
    store_file.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="fail closed"):
        auth._load_auth_store(store_file)

    corrupt = store_file.with_suffix(".json.corrupt")
    assert corrupt.exists(), "damaged store must be preserved for recovery"
    assert corrupt.read_text(encoding="utf-8") == payload


def test_healthy_store_is_returned_unchanged(store_file):
    result = auth._load_auth_store(store_file)
    assert result["providers"]["nous"]["api_key"] == "secret"


def test_log_does_not_claim_a_backup_that_was_not_written(
    store_file, monkeypatch, caplog
):
    """The old message advertised the .corrupt path even when copy2 failed —
    and claimed an empty-store fallback. F10: still refuse to continue, and
    never claim a backup that was not written. P4: the recovery path is now
    restrict-before-write (no copy2); the failure point is the ACL
    restriction — when it fails, no artifact is left and the log must not
    claim one was preserved."""
    import hermes_constants as hc

    store_file.write_text("{ not json", encoding="utf-8")

    monkeypatch.setattr(hc, "restrict_credential_file", lambda p: False)
    monkeypatch.setattr(hc, "credential_file_is_user_restricted", lambda p: False)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.auth"):
        with pytest.raises(ValueError, match="fail closed"):
            auth._load_auth_store(store_file)

    assert not store_file.with_suffix(".json.corrupt").exists()
    text = caplog.text
    assert "could NOT be preserved" in text
    assert "Corrupt file preserved at" not in text


def test_corrupt_store_is_not_overwritten_by_next_save(store_file, tmp_path):
    """F10 regression: with corruption failing closed, a save cannot follow
    the empty-store fallback — the on-disk credentials survive untouched."""
    store_file.write_text("{ not json", encoding="utf-8")
    before = store_file.read_bytes()

    with pytest.raises(ValueError, match="fail closed"):
        auth._load_auth_store(store_file)

    # No save path ever runs with an empty store: the store on disk still
    # holds the original corrupt bytes (plus the preserved .corrupt copy).
    assert store_file.read_bytes() == before
