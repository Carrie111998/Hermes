from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.canary import trusted_signer_stage0 as stage0


def _directory_state(
    mode: int,
    *,
    uid: int = 0,
    gid: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_uid=uid,
        st_gid=gid,
    )


@pytest.mark.parametrize("mode", (0o755, 0o775, 0o1777))
def test_host_runtime_lock_accepts_pinned_root_owned_parent_modes(
    mode: int,
) -> None:
    assert stage0._lock_parent_is_trusted(_directory_state(mode))


@pytest.mark.parametrize(
    "state",
    (
        _directory_state(0o777),
        _directory_state(0o1775),
        _directory_state(0o1777, uid=1),
        _directory_state(0o1777, gid=1),
        SimpleNamespace(
            st_mode=stat.S_IFREG | 0o1777,
            st_uid=0,
            st_gid=0,
        ),
        SimpleNamespace(
            st_mode=stat.S_IFLNK | 0o1777,
            st_uid=0,
            st_gid=0,
        ),
    ),
)
def test_host_runtime_lock_rejects_untrusted_parent_contract(
    state: SimpleNamespace,
) -> None:
    assert not stage0._lock_parent_is_trusted(state)


def _local_snapshot(path: Path) -> tuple[bytes, tuple[int, ...]]:
    value = path.stat()
    return path.read_bytes(), (
        value.st_dev,
        value.st_ino,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        stat.S_IMODE(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def test_predecessor_sudoers_replacement_is_atomic_and_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "managed-sudoers"
    temporary = tmp_path / ".managed-sudoers.stage0-staged"
    destination.write_bytes(b"managed predecessor\n")
    destination.chmod(0o440)
    successor = tmp_path / ("a" * 40)
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(stage0, "_sudoers_snapshot", _local_snapshot)
    monkeypatch.setattr(
        stage0,
        "_validate_predecessor_sudoers",
        lambda raw, *, successor_release: successor_release,
    )
    monkeypatch.setattr(stage0, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(stage0.os, "fchown", lambda *_args: None)
    monkeypatch.setattr(
        stage0,
        "HOST_CURRENT_LINK",
        tmp_path / "current-absent",
    )
    monkeypatch.setattr(
        stage0,
        "HOST_ACTIVATION_SEAL",
        tmp_path / "activation-absent",
    )

    stage0._replace_predecessor_sudoers(
        b"managed successor\n",
        successor_release=successor,
        destination=destination,
        temporary=temporary,
        runner=lambda argv: commands.append(tuple(argv)) or b"",
        after_open=None,
    )

    assert destination.read_bytes() == b"managed successor\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o440
    assert not temporary.exists()
    assert commands == [
        ("/usr/sbin/visudo", "-cf", str(temporary)),
    ]


def test_predecessor_sudoers_replacement_rejects_destination_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "managed-sudoers"
    temporary = tmp_path / ".managed-sudoers.stage0-staged"
    destination.write_bytes(b"managed predecessor\n")
    destination.chmod(0o440)

    monkeypatch.setattr(stage0, "_sudoers_snapshot", _local_snapshot)
    monkeypatch.setattr(
        stage0,
        "_validate_predecessor_sudoers",
        lambda raw, *, successor_release: successor_release,
    )
    monkeypatch.setattr(stage0, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(stage0.os, "fchown", lambda *_args: None)
    monkeypatch.setattr(
        stage0,
        "HOST_CURRENT_LINK",
        tmp_path / "current-absent",
    )
    monkeypatch.setattr(
        stage0,
        "HOST_ACTIVATION_SEAL",
        tmp_path / "activation-absent",
    )

    def race() -> None:
        destination.chmod(0o640)
        destination.write_bytes(b"raced destination\n")
        destination.chmod(0o440)

    with pytest.raises(
        stage0.TrustedSignerStage0Error,
        match="trusted_signer_stage0_sudoers_conflict",
    ):
        stage0._replace_predecessor_sudoers(
            b"managed successor\n",
            successor_release=tmp_path / ("a" * 40),
            destination=destination,
            temporary=temporary,
            runner=lambda _argv: b"",
            after_open=race,
        )

    assert destination.read_bytes() == b"raced destination\n"
    assert temporary.read_bytes() == b"managed successor\n"


def test_predecessor_sudoers_must_match_one_immutable_managed_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor_revision = "b" * 40
    successor_revision = "c" * 40
    release_base = tmp_path / "releases"
    predecessor = release_base / predecessor_revision
    template = (
        predecessor
        / "ops/muncho/owner-gate/"
        "muncho-host-observation-attestor.sudoers.in"
    )
    template.parent.mkdir(parents=True)
    template.write_bytes(
        b"Cmnd_Alias TEST = "
        b"/opt/muncho-trusted-observation/releases/@RELEASE_SHA@/"
        b"venv/bin/python -I -B\n"
    )
    template.chmod(0o444)
    for relative in (
        "venv/bin/python",
        "bin/muncho-host-trusted-signer-provision",
        "bin/muncho-host-observation-attestor",
    ):
        path = predecessor / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"executable\n")
        path.chmod(0o555)
    predecessor.chmod(0o555)
    rendered = template.read_bytes().replace(
        b"@RELEASE_SHA@",
        predecessor_revision.encode("ascii"),
    )
    real_lstat = Path.lstat

    def root_lstat(path: Path) -> SimpleNamespace:
        value = real_lstat(path)
        return SimpleNamespace(
            st_mode=value.st_mode,
            st_uid=0,
            st_gid=0,
        )

    def root_reader(path: Path, **kwargs: object) -> bytes:
        assert kwargs["expected_uid"] == 0
        assert kwargs.get("expected_gid", 0) == 0
        return path.read_bytes()

    monkeypatch.setattr(stage0, "HOST_RELEASE_BASE", release_base)
    monkeypatch.setattr(Path, "lstat", root_lstat)
    monkeypatch.setattr(stage0.stage0, "_read_regular", root_reader)
    monkeypatch.setattr(stage0, "_render_sudoers", lambda _release: rendered)

    assert stage0._validate_predecessor_sudoers(
        rendered,
        successor_release=release_base / successor_revision,
    ) == predecessor

    ambiguous = rendered + rendered.replace(
        predecessor_revision.encode("ascii"),
        ("d" * 40).encode("ascii"),
    )
    with pytest.raises(
        stage0.TrustedSignerStage0Error,
        match="trusted_signer_stage0_sudoers_conflict",
    ):
        stage0._validate_predecessor_sudoers(
            ambiguous,
            successor_release=release_base / successor_revision,
        )
