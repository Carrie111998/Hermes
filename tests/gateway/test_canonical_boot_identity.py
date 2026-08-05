from __future__ import annotations

import os
from pathlib import Path

import pytest

import gateway.canonical_boot_identity as identity


BOOT_ID = "11111111-1111-4111-8111-111111111111"


def _with_owner(value: os.stat_result, *, uid: int, gid: int) -> os.stat_result:
    fields = list(value)
    fields[4] = uid
    fields[5] = gid
    return os.stat_result(fields)


def test_reads_canonical_kernel_boot_id_without_systemd_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "boot_id"
    source.write_text(f"{BOOT_ID}\n", encoding="ascii")
    monkeypatch.setattr(identity, "PROC_BOOT_ID_PATH", source)

    assert identity.current_boot_id(environ={}) == BOOT_ID


@pytest.mark.parametrize(
    ("credential_uid", "credential_gid"),
    [
        (0, 0),
        (os.geteuid(), 0),
        (os.geteuid(), os.getegid()),
    ],
)
def test_reads_exact_private_systemd_boot_identity_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_uid: int,
    credential_gid: int,
) -> None:
    credentials_root = tmp_path / "run/credentials"
    credentials = credentials_root / "muncho-canonical-writer.service"
    credentials.mkdir(parents=True)
    boot_id = credentials / identity.SYSTEMD_BOOT_ID_CREDENTIAL_NAME
    boot_id.write_text(f"{BOOT_ID}\n", encoding="ascii")
    boot_id.chmod(0o400)
    credentials.chmod(0o500)
    real_stat = os.stat
    real_fstat = os.fstat
    monkeypatch.setattr(identity, "SYSTEMD_CREDENTIALS_ROOT", credentials_root)
    monkeypatch.setattr(
        identity.os,
        "stat",
        lambda *args, **kwargs: _with_owner(
            real_stat(*args, **kwargs), uid=credential_uid, gid=credential_gid
        ),
    )
    monkeypatch.setattr(
        identity.os,
        "fstat",
        lambda descriptor: _with_owner(
            real_fstat(descriptor), uid=credential_uid, gid=credential_gid
        ),
    )

    assert identity.current_boot_id(
        environ={"CREDENTIALS_DIRECTORY": str(credentials)}
    ) == BOOT_ID


@pytest.mark.parametrize(
    "value",
    [
        "11111111-1111-4111-8111-11111111111A",
        "not-a-boot-id",
        "1" * 129,
    ],
)
def test_rejects_noncanonical_or_oversized_kernel_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    source = tmp_path / "boot_id"
    source.write_text(f"{value}\n", encoding="ascii")
    monkeypatch.setattr(identity, "PROC_BOOT_ID_PATH", source)

    with pytest.raises(RuntimeError, match="boot identity"):
        identity.current_boot_id(environ={})


def test_rejects_untrusted_credentials_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        identity,
        "SYSTEMD_CREDENTIALS_ROOT",
        tmp_path / "run/credentials",
    )

    with pytest.raises(RuntimeError, match="credential path"):
        identity.current_boot_id(
            environ={"CREDENTIALS_DIRECTORY": str(tmp_path / "attacker.service")}
        )


def test_rejects_writable_systemd_credential_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials_root = tmp_path / "run/credentials"
    credentials = credentials_root / "muncho-canonical-writer.service"
    credentials.mkdir(parents=True)
    credentials.chmod(0o700)
    boot_id = credentials / identity.SYSTEMD_BOOT_ID_CREDENTIAL_NAME
    boot_id.write_text(f"{BOOT_ID}\n", encoding="ascii")
    boot_id.chmod(0o600)
    real_stat = os.stat
    real_fstat = os.fstat
    monkeypatch.setattr(identity, "SYSTEMD_CREDENTIALS_ROOT", credentials_root)
    monkeypatch.setattr(
        identity.os,
        "stat",
        lambda *args, **kwargs: _with_owner(
            real_stat(*args, **kwargs), uid=os.geteuid(), gid=os.getegid()
        ),
    )
    monkeypatch.setattr(
        identity.os,
        "fstat",
        lambda descriptor: _with_owner(
            real_fstat(descriptor), uid=os.geteuid(), gid=os.getegid()
        ),
    )

    with pytest.raises(RuntimeError, match="credential metadata"):
        identity.current_boot_id(
            environ={"CREDENTIALS_DIRECTORY": str(credentials)}
        )


def test_rejects_mismatched_systemd_credential_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials_root = tmp_path / "run/credentials"
    credentials = credentials_root / "muncho-canonical-writer.service"
    credentials.mkdir(parents=True)
    boot_id = credentials / identity.SYSTEMD_BOOT_ID_CREDENTIAL_NAME
    boot_id.write_text(f"{BOOT_ID}\n", encoding="ascii")
    boot_id.chmod(0o400)
    credentials.chmod(0o500)
    real_stat = os.stat
    real_fstat = os.fstat
    monkeypatch.setattr(identity, "SYSTEMD_CREDENTIALS_ROOT", credentials_root)
    monkeypatch.setattr(
        identity.os,
        "stat",
        lambda *args, **kwargs: _with_owner(
            real_stat(*args, **kwargs), uid=os.geteuid(), gid=os.getegid()
        ),
    )
    monkeypatch.setattr(
        identity.os,
        "fstat",
        lambda descriptor: _with_owner(real_fstat(descriptor), uid=0, gid=0),
    )

    with pytest.raises(RuntimeError, match="credential metadata"):
        identity.current_boot_id(
            environ={"CREDENTIALS_DIRECTORY": str(credentials)}
        )


def test_missing_host_boot_credential_retains_visible_procfs_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials_root = tmp_path / "run/credentials"
    credentials = credentials_root / "hermes-cloud-gateway.service"
    credentials.mkdir(parents=True)
    source = tmp_path / "boot_id"
    source.write_text(f"{BOOT_ID}\n", encoding="ascii")
    monkeypatch.setattr(identity, "SYSTEMD_CREDENTIALS_ROOT", credentials_root)
    monkeypatch.setattr(identity, "PROC_BOOT_ID_PATH", source)

    assert identity.current_boot_id(
        environ={"CREDENTIALS_DIRECTORY": str(credentials)}
    ) == BOOT_ID
