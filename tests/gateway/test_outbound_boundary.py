import asyncio
import hashlib
import json
import os
import stat
from types import SimpleNamespace
from pathlib import Path

from gateway.hooks import HookRegistry
import pytest

from gateway.outbound_boundary import (
    BoundaryLoadError,
    load_installed_outbound_hooks,
    outbound_after_send_sync,
    outbound_before_send_sync,
)


def _activate(hook_dir):
    version_tuple = {
        "hak_commit": "h", "hak_tag": "v", "hak_source_path": "source", "hak_archive_path": "archive", "hak_archive_sha256": "sha256:archive",
        "homebrew_keg_path": "keg", "homebrew_keg_sha256": "sha256:keg", "core_commit": "core",
        "core_runtime_path": "runtime", "core_patch_path": "patch", "core_patch_sha256": "sha256:patch",
    }
    from gateway.outbound_boundary import _release_tuple_digest
    tuple_digest = _release_tuple_digest(version_tuple)
    for name, contents in {
        "kit-root.txt": "/trusted/kit\n",
        "kit_root_paths.py": "# pinned kit root helper\n",
        "runtime_loader_attestation.py": "# pinned runtime attestation helper\n",
    }.items():
        (hook_dir / name).write_text(contents, encoding="utf-8")
    files = [
        {"path": name, "sha256": hashlib.sha256((hook_dir / name).read_bytes()).hexdigest()}
        for name in (
            "HOOK.yaml", "handler.py", "kit-root.txt", "kit_root_paths.py", "runtime_loader_attestation.py",
        )
    ]
    unsigned_bundle = {"schema_version": 1, "files": files}
    (hook_dir / "kit-bundle-manifest.json").write_text(json.dumps({
        **unsigned_bundle,
        "bundle_sha256": hashlib.sha256(
            json.dumps(unsigned_bundle, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }), encoding="utf-8")
    release = hook_dir / "release-tuple.json"
    release.write_text(json.dumps({
        "schema_version": "outbound-actionable-release/v1",
        "tuple_digest": tuple_digest,
        "version_tuple": version_tuple,
        "bundle_manifest_sha256": "sha256:" + hashlib.sha256(
            (hook_dir / "kit-bundle-manifest.json").read_bytes()
        ).hexdigest(),
    }), encoding="utf-8")
    os.chmod(release, 0o600)
    shared = hook_dir.parents[1] / "shared-activation.json"
    payload = {
        "schema_version": "outbound-actionable-dual-activation/v1",
        "tuple_digest": tuple_digest,
        "version_tuple": version_tuple,
        "profiles": [
            {"profile_id": "atlas", "hook_dir": str(hook_dir.absolute()), "fingerprint": {
                name: "sha256:" + hashlib.sha256((hook_dir / name).read_bytes()).hexdigest()
                for name in (
                    "HOOK.yaml", "handler.py", "kit-bundle-manifest.json", "kit-root.txt",
                    "kit_root_paths.py", "runtime_loader_attestation.py", "release-tuple.json",
                )
            } | {
                "kit_root_paths": "sha256:resolver",
                "runtime_loader_identity": "sha256:runtime-identity",
                "runtime_loader_generation": "sha256:runtime-generation",
            }, "runtime_identity": {}},
            {"profile_id": "yuange", "hook_dir": str(hook_dir.parent / "peer"), "fingerprint": {}, "runtime_identity": {}},
        ],
    }
    data = json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n"
    shared.write_bytes(data)
    os.chmod(shared, 0o600)
    (hook_dir / "release-activation.json").write_text(json.dumps({
        "schema_version": "outbound-actionable-dual-activation/v1", "tuple_digest": tuple_digest,
        "profile_id": "atlas", "activation_path": str(shared),
        "activation_sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
    }), encoding="utf-8")
    os.chmod(hook_dir / "release-activation.json", 0o600)


def _prepare_capture(hook_dir):
    from gateway.outbound_boundary import _release_tuple_digest

    keg = hook_dir.parents[2] / "keg"
    keg.mkdir(exist_ok=True)
    version_tuple = {
        "hak_commit": "h", "hak_tag": "v", "hak_source_path": "source", "hak_archive_path": "archive", "hak_archive_sha256": "sha256:archive",
        "homebrew_keg_path": str(keg), "homebrew_keg_sha256": "sha256:keg", "core_commit": "core",
        "core_runtime_path": "runtime", "core_patch_path": "patch", "core_patch_sha256": "sha256:patch",
    }
    tuple_digest = _release_tuple_digest(version_tuple)
    manifest_sha = "sha256:" + hashlib.sha256((hook_dir / "kit-bundle-manifest.json").read_bytes()).hexdigest()
    (hook_dir / "release-tuple.json").write_text(json.dumps({
        "schema_version": "outbound-actionable-release/v1", "tuple_digest": tuple_digest,
        "version_tuple": version_tuple, "bundle_manifest_sha256": manifest_sha,
    }), encoding="utf-8")
    os.chmod(hook_dir / "release-tuple.json", 0o600)
    prepared_dir = keg.parent / ".outbound-actionable-activations"
    prepared_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(prepared_dir, 0o700)
    prepared = prepared_dir / f"{tuple_digest.removeprefix('sha256:')}.prepared.json"
    trusted_root = hook_dir.parents[2] / "trusted-kit"
    trusted_scripts = trusted_root / "scripts"
    trusted_scripts.mkdir(parents=True, exist_ok=True)
    trusted_module = trusted_scripts / "trusted.py"
    trusted_module.write_text("# prepared runtime bundle\n", encoding="utf-8")
    runtime_identity = {
        "raw_root": str(trusted_root),
        "module_hashes": {"trusted.py": hashlib.sha256(trusted_module.read_bytes()).hexdigest()},
    }
    prepared.write_text(json.dumps({
        "schema_version": "outbound-actionable-dual-prepared/v1", "tuple_digest": tuple_digest,
        "version_tuple": version_tuple,
        "profiles": [
            {"profile_id": "atlas", "hook_dir": str(hook_dir.absolute()), "fingerprint": {"kit-bundle-manifest.json": manifest_sha}, "runtime_identity": runtime_identity},
            {"profile_id": "yuange", "hook_dir": str(hook_dir.parent / "peer"), "fingerprint": {"kit-bundle-manifest.json": manifest_sha}, "runtime_identity": runtime_identity},
        ],
    }), encoding="utf-8")
    os.chmod(prepared, 0o600)


def _prepared_control_path(hook_dir):
    release = json.loads((hook_dir / "release-tuple.json").read_text(encoding="utf-8"))
    tuple_digest = release["tuple_digest"].removeprefix("sha256:")
    keg = Path(release["version_tuple"]["homebrew_keg_path"])
    return keg.parent / ".outbound-actionable-activations" / f"{tuple_digest}.prepared.json"


def _rewrite_prepared_profiles(hook_dir, profiles):
    prepared = _prepared_control_path(hook_dir)
    payload = json.loads(prepared.read_text(encoding="utf-8"))
    payload["profiles"] = profiles
    prepared.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(prepared, 0o600)


def _rewrite_activation_profiles(hook_dir, profiles, *, profile_id):
    activation = hook_dir / "release-activation.json"
    activation_payload = json.loads(activation.read_text(encoding="utf-8"))
    shared = Path(activation_payload["activation_path"])
    shared_payload = json.loads(shared.read_text(encoding="utf-8"))
    shared_payload["profiles"] = profiles
    data = json.dumps(shared_payload, sort_keys=True).encode("utf-8") + b"\n"
    shared.write_bytes(data)
    os.chmod(shared, 0o600)
    activation_payload["profile_id"] = profile_id
    activation_payload["activation_sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()
    activation.write_text(json.dumps(activation_payload), encoding="utf-8")
    os.chmod(activation, 0o600)


def test_missing_boundary_decision_fails_closed():
    decision = outbound_before_send_sync(HookRegistry(), {"content": "report"})
    assert decision.decision == "deny"


def test_single_rewrite_decision_is_carried_to_transport():
    hooks = HookRegistry()
    hooks._handlers["outbound:before_send"] = [lambda _event, _ctx: {"decision": "rewrite", "content": "safe", "reason": "projection"}]
    decision = outbound_before_send_sync(hooks, {"content": "raw"})
    assert decision.transmit and decision.content == "safe"


def test_malformed_rewrite_fails_closed_without_falling_back_to_raw_content():
    hooks = HookRegistry()
    hooks._handlers["outbound:before_send"] = [lambda _event, _ctx: {"decision": "rewrite"}]

    decision = outbound_before_send_sync(hooks, {"content": "raw private result"})

    assert decision.decision == "deny"
    assert decision.reason == "boundary_rewrite_missing_content"
    assert decision.content == ""


def test_handler_failure_fails_closed_even_when_another_handler_allows():
    hooks = HookRegistry()

    def raise_error(_event, _ctx):
        raise RuntimeError("projection unavailable")

    hooks._handlers["outbound:before_send"] = [
        lambda _event, _ctx: {"decision": "allow"},
        raise_error,
    ]

    decision = outbound_before_send_sync(hooks, {"content": "report"})

    assert decision.decision == "deny"
    assert decision.reason == "boundary_unavailable"


def test_missing_manifest_keeps_boundary_optional(tmp_path):
    assert load_installed_outbound_hooks(tmp_path) is None


def test_copied_hook_without_release_activation_fails_closed(tmp_path):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text("# handler\n", encoding="utf-8")
    (hook_dir / "release-tuple.json").write_text('{"tuple_digest":"sha256:test"}', encoding="utf-8")

    with pytest.raises(BoundaryLoadError, match="not release-activated"):
        load_installed_outbound_hooks(tmp_path)


def test_generic_hook_registry_skips_unactivated_outbound_bundle(tmp_path):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("name: outbound-actionable\nevents: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text("def handle(*_args):\n    return {'decision': 'allow'}\n", encoding="utf-8")

    registry = HookRegistry(hooks_dir=hook_dir.parent)
    registry.discover_and_load()

    assert registry.loaded_hooks == []


def test_generic_hook_registry_captures_pending_runtime_identity_without_registration(tmp_path):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("name: outbound-actionable\nevents: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text(
        "from pathlib import Path\n"
        "def capture_runtime_attestation(profile):\n"
        "    (Path(profile) / 'capture').write_text('captured')\n"
        "def handle(*_args):\n"
        "    return {'decision': 'allow'}\n",
        encoding="utf-8",
    )
    files = [
        {"path": name, "sha256": hashlib.sha256((hook_dir / name).read_bytes()).hexdigest()}
        for name in ("HOOK.yaml", "handler.py")
    ]
    unsigned = {"schema_version": 1, "files": files}
    (hook_dir / "kit-bundle-manifest.json").write_text(json.dumps({
        **unsigned, "bundle_sha256": hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }), encoding="utf-8")
    _prepare_capture(hook_dir)

    registry = HookRegistry(hooks_dir=hook_dir.parent)
    registry.discover_and_load()

    assert (tmp_path / "capture").read_text(encoding="utf-8") == "captured"
    assert registry.loaded_hooks == []


def test_pending_capture_executes_the_verified_handler_bytes_after_path_replacement(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "handler.py").write_text(
        "from pathlib import Path\n"
        "def capture_runtime_attestation(profile):\n"
        "    Path(profile).joinpath('captured').write_text('verified')\n",
        encoding="utf-8",
    )
    files = [{"path": "handler.py", "sha256": hashlib.sha256((hook_dir / "handler.py").read_bytes()).hexdigest()}]
    unsigned = {"schema_version": 1, "files": files}
    (hook_dir / "kit-bundle-manifest.json").write_text(json.dumps({
        **unsigned, "bundle_sha256": hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }), encoding="utf-8")
    _prepare_capture(hook_dir)
    from gateway import outbound_boundary
    original_exec = outbound_boundary._exec_pinned_handler

    def replace_after_read(module_name, handler, source):
        (hook_dir / "handler.py").write_text("raise RuntimeError('replacement executed')\n", encoding="utf-8")
        return original_exec(module_name, handler, source)

    monkeypatch.setattr(outbound_boundary, "_exec_pinned_handler", replace_after_read)
    outbound_boundary.capture_pending_outbound_attestation(hook_dir)

    assert (tmp_path / "captured").read_text(encoding="utf-8") == "verified"


def test_pending_capture_pins_the_prepared_runtime_and_restores_environment(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "handler.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def capture_runtime_attestation(profile):\n"
        "    Path(profile).joinpath('runtime').write_text(os.environ.get('HERMES_AGENT_KIT_HOME', '') + '|' + os.environ.get('HERMES_AGENT_KIT_SCRIPTS', ''))\n",
        encoding="utf-8",
    )
    files = [{"path": "handler.py", "sha256": hashlib.sha256((hook_dir / "handler.py").read_bytes()).hexdigest()}]
    unsigned = {"schema_version": 1, "files": files}
    (hook_dir / "kit-bundle-manifest.json").write_text(json.dumps({
        **unsigned, "bundle_sha256": hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }), encoding="utf-8")
    _prepare_capture(hook_dir)
    monkeypatch.setenv("HERMES_AGENT_KIT_SCRIPTS", "/attacker/scripts")

    from gateway.outbound_boundary import capture_pending_outbound_attestation
    capture_pending_outbound_attestation(hook_dir)

    assert (tmp_path / "runtime").read_text(encoding="utf-8") == f"{hook_dir.parents[2] / 'trusted-kit'}|"
    assert os.environ["HERMES_AGENT_KIT_SCRIPTS"] == "/attacker/scripts"


def test_pending_capture_does_not_execute_when_prepared_runtime_identity_cannot_be_reverified(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "handler.py").write_text("raise RuntimeError('must remain inert')\n", encoding="utf-8")
    files = [{"path": "handler.py", "sha256": hashlib.sha256((hook_dir / "handler.py").read_bytes()).hexdigest()}]
    unsigned = {"schema_version": 1, "files": files}
    (hook_dir / "kit-bundle-manifest.json").write_text(json.dumps({
        **unsigned, "bundle_sha256": hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }), encoding="utf-8")
    _prepare_capture(hook_dir)
    from gateway import outbound_boundary
    monkeypatch.setattr(outbound_boundary, "_prepared_runtime_root", lambda _entry: None)

    outbound_boundary.capture_pending_outbound_attestation(hook_dir)


def test_activated_snapshot_rejects_corrupt_shared_control_and_handler_reread(tmp_path, monkeypatch):
    from gateway import outbound_boundary

    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text("def handle(*_args): return None\n", encoding="utf-8")
    _activate(hook_dir)
    activation = json.loads((hook_dir / "release-activation.json").read_text(encoding="utf-8"))
    activation["activation_sha256"] = "sha256:wrong"
    (hook_dir / "release-activation.json").write_text(json.dumps(activation), encoding="utf-8")
    os.chmod(hook_dir / "release-activation.json", 0o600)
    assert outbound_boundary._activated_handler_snapshot(hook_dir) is None

    _activate(hook_dir)
    original_reader = outbound_boundary._read_regular_bytes
    handler_reads = 0

    def changed_on_execution(path):
        nonlocal handler_reads
        if Path(path).name == "handler.py":
            handler_reads += 1
            if handler_reads == 2:
                return b"changed"
        return original_reader(path)

    monkeypatch.setattr(outbound_boundary, "_bundle_manifest_is_valid", lambda *_args: True)
    monkeypatch.setattr(outbound_boundary, "_read_regular_bytes", changed_on_execution)
    assert outbound_boundary._activated_handler_snapshot(hook_dir) is None

    monkeypatch.undo()
    _activate(hook_dir)
    (hook_dir / "release-tuple.json").write_text("{", encoding="utf-8")
    os.chmod(hook_dir / "release-tuple.json", 0o600)
    assert outbound_boundary._activated_handler_snapshot(hook_dir) is None


def test_prepared_runtime_root_rejects_empty_or_mismatched_bundle(tmp_path):
    from gateway import outbound_boundary
    from gateway.outbound_boundary import _prepared_runtime_root

    assert _prepared_runtime_root({}) is None
    assert _prepared_runtime_root({"runtime_identity": {"raw_root": str(tmp_path / "missing"), "module_hashes": {"seam.py": "x"}}}) is None
    assert _prepared_runtime_root({"runtime_identity": {"raw_root": str(tmp_path), "module_hashes": {}}}) is None
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "seam.py").write_text("trusted", encoding="utf-8")
    assert _prepared_runtime_root({"runtime_identity": {"raw_root": str(tmp_path), "module_hashes": {"seam.py": "wrong"}}}) is None
    assert _prepared_runtime_root({"runtime_identity": {"raw_root": str(tmp_path), "module_hashes": {"../seam.py": "wrong"}}}) is None
    original_reader = outbound_boundary._read_regular_bytes
    outbound_boundary._read_regular_bytes = lambda _path: (_ for _ in ()).throw(OSError("unreadable"))
    try:
        assert _prepared_runtime_root({"runtime_identity": {"raw_root": str(tmp_path), "module_hashes": {"seam.py": "x"}}}) is None
    finally:
        outbound_boundary._read_regular_bytes = original_reader


def test_pending_capture_does_not_execute_an_unmanifested_handler(tmp_path):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "handler.py").write_text("from pathlib import Path\nPath(__file__).parents[2].joinpath('executed').write_text('bad')\n", encoding="utf-8")
    from gateway.outbound_boundary import capture_pending_outbound_attestation
    capture_pending_outbound_attestation(hook_dir)
    assert not (tmp_path / "executed").exists()


def test_descriptor_reader_fails_closed_for_open_invalid_metadata_and_read_errors(tmp_path, monkeypatch):
    from gateway import outbound_boundary

    assert outbound_boundary._read_regular_bytes(tmp_path / "missing") is None
    target = tmp_path / "managed.py"
    target.write_text("trusted", encoding="utf-8")
    descriptor = os.open(target, os.O_RDONLY)
    monkeypatch.setattr(outbound_boundary.os, "open", lambda *_args: descriptor)
    monkeypatch.setattr(
        outbound_boundary.os, "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFDIR, st_uid=os.getuid(), st_nlink=1),
    )
    assert outbound_boundary._read_regular_bytes(target) is None
    monkeypatch.undo()

    descriptor = os.open(target, os.O_RDONLY)
    monkeypatch.setattr(outbound_boundary.os, "open", lambda *_args: descriptor)

    class BrokenReader:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            raise OSError("read failed")

    monkeypatch.setattr(outbound_boundary.os, "fdopen", lambda *_args, **_kwargs: BrokenReader())
    assert outbound_boundary._read_regular_bytes(target) is None
    monkeypatch.undo()

    private = tmp_path / "private-control.json"
    private.write_text("control", encoding="utf-8")
    os.chmod(private, 0o644)
    assert outbound_boundary._read_private_bytes(private) is None
    os.chmod(private, 0o600)
    monkeypatch.setattr(outbound_boundary.os, "fdopen", lambda *_args, **_kwargs: BrokenReader())
    assert outbound_boundary._read_private_bytes(private) is None


def test_manifest_and_active_loader_reject_malformed_or_changed_inputs(tmp_path, monkeypatch):
    from gateway import outbound_boundary

    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    manifest = hook_dir / "kit-bundle-manifest.json"
    manifest.write_bytes(b"not json")
    assert outbound_boundary._validated_bundle_manifest(hook_dir, manifest) is None
    assert outbound_boundary._validated_bundle_manifest(hook_dir, manifest, "sha256:wrong") is None
    with pytest.raises(BoundaryLoadError, match="not release-activated"):
        outbound_boundary.load_activated_outbound_handler(hook_dir, "inactive")

    (hook_dir / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text("def handle(*_args): return None\n", encoding="utf-8")
    _activate(hook_dir)
    monkeypatch.setattr(outbound_boundary, "outbound_activation_is_ready", lambda _hook: True)
    (hook_dir / "release-activation.json").write_text("{}", encoding="utf-8")
    with pytest.raises(BoundaryLoadError, match="activation changed"):
        outbound_boundary.load_activated_outbound_handler(hook_dir, "malformed")
    _activate(hook_dir)
    (hook_dir / "handler.py").write_text("def handle(*_args): return 'changed'\n", encoding="utf-8")
    with pytest.raises(BoundaryLoadError, match="activation changed"):
        outbound_boundary.load_activated_outbound_handler(hook_dir, "drifted")


def test_inert_capture_requires_a_well_formed_external_preparation_record(tmp_path):
    from gateway import outbound_boundary

    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "handler.py").write_text("# handler\n", encoding="utf-8")
    files = [{"path": "handler.py", "sha256": hashlib.sha256((hook_dir / "handler.py").read_bytes()).hexdigest()}]
    unsigned = {"schema_version": 1, "files": files}
    (hook_dir / "kit-bundle-manifest.json").write_text(json.dumps({
        **unsigned, "bundle_sha256": hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }), encoding="utf-8")
    _prepare_capture(hook_dir)
    release = hook_dir / "release-tuple.json"
    release_payload = json.loads(release.read_text(encoding="utf-8"))
    release_payload["schema_version"] = "wrong"
    release.write_text(json.dumps(release_payload), encoding="utf-8")
    os.chmod(release, 0o600)
    assert outbound_boundary._prepared_release_for_hook(hook_dir) is None

    _prepare_capture(hook_dir)
    tuple_digest = json.loads(release.read_text(encoding="utf-8"))["tuple_digest"]
    prepared = Path(release_payload["version_tuple"]["homebrew_keg_path"]).parent / ".outbound-actionable-activations" / f"{tuple_digest.removeprefix('sha256:')}.prepared.json"
    prepared.unlink()
    assert outbound_boundary._prepared_release_for_hook(hook_dir) is None

    _prepare_capture(hook_dir)
    prepared.write_text(json.dumps({"schema_version": "wrong", "profiles": []}), encoding="utf-8")
    os.chmod(prepared, 0o600)
    assert outbound_boundary._prepared_release_for_hook(hook_dir) is None
    prepared.write_text("not json", encoding="utf-8")
    os.chmod(prepared, 0o600)
    assert outbound_boundary._prepared_release_for_hook(hook_dir) is None


@pytest.mark.parametrize("peer_count", [0, 2])
def test_prepared_manifest_accepts_nonempty_unique_ordered_profiles(tmp_path, peer_count):
    from gateway import outbound_boundary

    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "kit-bundle-manifest.json").write_text("{}", encoding="utf-8")
    _prepare_capture(hook_dir)
    prepared = json.loads(_prepared_control_path(hook_dir).read_text(encoding="utf-8"))
    own = {**prepared["profiles"][0], "profile_id": "research"}
    peers = [
        {**prepared["profiles"][1], "profile_id": f"peer-{index}", "hook_dir": str(tmp_path / f"peer-{index}" / "hooks" / "outbound-actionable")}
        for index in range(peer_count)
    ]
    profiles = peers[:1] + [own] + peers[1:]
    _rewrite_prepared_profiles(hook_dir, profiles)

    result = outbound_boundary._prepared_release_for_hook(hook_dir)

    assert result is not None
    assert result[1]["profile_id"] == "research"


@pytest.mark.parametrize(
    "invalid",
    ["empty", "duplicate-profile-id", "duplicate-hook-dir", "relative-hook-dir", "missing-current-hook"],
)
def test_prepared_manifest_rejects_ambiguous_or_unbound_profiles(tmp_path, invalid):
    from gateway import outbound_boundary

    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "kit-bundle-manifest.json").write_text("{}", encoding="utf-8")
    _prepare_capture(hook_dir)
    profiles = json.loads(json.dumps(
        json.loads(_prepared_control_path(hook_dir).read_text(encoding="utf-8"))["profiles"]
    ))
    if invalid == "empty":
        profiles = []
    elif invalid == "duplicate-profile-id":
        profiles[1]["profile_id"] = profiles[0]["profile_id"]
    elif invalid == "duplicate-hook-dir":
        profiles[1]["hook_dir"] = profiles[0]["hook_dir"]
    elif invalid == "relative-hook-dir":
        profiles[1]["hook_dir"] = "relative/hooks/outbound-actionable"
    else:
        profiles[0]["hook_dir"] = str(tmp_path / "other" / "hooks" / "outbound-actionable")
    _rewrite_prepared_profiles(hook_dir, profiles)

    assert outbound_boundary._prepared_release_for_hook(hook_dir) is None


@pytest.mark.parametrize("peer_count", [0, 2])
def test_activation_manifest_accepts_nonempty_unique_ordered_profiles(tmp_path, peer_count):
    from gateway import outbound_boundary

    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text(
        "def handle(*_args):\n    return {'decision': 'allow'}\n", encoding="utf-8",
    )
    _activate(hook_dir)
    activation = json.loads((hook_dir / "release-activation.json").read_text(encoding="utf-8"))
    shared = json.loads(Path(activation["activation_path"]).read_text(encoding="utf-8"))
    own = {**shared["profiles"][0], "profile_id": "research"}
    peers = [
        {**shared["profiles"][1], "profile_id": f"peer-{index}", "hook_dir": str(tmp_path / f"peer-{index}" / "hooks" / "outbound-actionable")}
        for index in range(peer_count)
    ]
    _rewrite_activation_profiles(hook_dir, peers[:1] + [own] + peers[1:], profile_id="research")

    assert outbound_boundary.outbound_activation_is_ready(hook_dir) is True
    assert outbound_boundary._activated_handler_snapshot(hook_dir) is not None
    assert outbound_boundary.load_installed_outbound_hooks(tmp_path) is not None


@pytest.mark.parametrize(
    "invalid",
    ["empty", "duplicate-profile-id", "duplicate-hook-dir", "relative-hook-dir", "profile-hook-mismatch"],
)
def test_activation_manifest_rejects_ambiguous_or_unbound_profiles(tmp_path, invalid):
    from gateway.outbound_boundary import outbound_activation_is_ready

    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text("# handler\n", encoding="utf-8")
    _activate(hook_dir)
    activation = json.loads((hook_dir / "release-activation.json").read_text(encoding="utf-8"))
    profiles = json.loads(json.dumps(
        json.loads(Path(activation["activation_path"]).read_text(encoding="utf-8"))["profiles"]
    ))
    profile_id = "atlas"
    if invalid == "empty":
        profiles = []
    elif invalid == "duplicate-profile-id":
        profiles[1]["profile_id"] = profiles[0]["profile_id"]
    elif invalid == "duplicate-hook-dir":
        profiles[1]["hook_dir"] = profiles[0]["hook_dir"]
    elif invalid == "relative-hook-dir":
        profiles[1]["hook_dir"] = "relative/hooks/outbound-actionable"
    else:
        profile_id = "yuange"
    _rewrite_activation_profiles(hook_dir, profiles, profile_id=profile_id)

    assert outbound_activation_is_ready(hook_dir) is False


@pytest.mark.parametrize("payload", [{}, {"schema_version": 1, "files": []}, {"schema_version": 1, "files": [{"path": "../escape", "sha256": "x"}], "bundle_sha256": "bad"}])
def test_pending_capture_manifest_rejects_invalid_shapes(tmp_path, payload):
    from gateway.outbound_boundary import _bundle_manifest_is_valid
    hook_dir = tmp_path / "outbound-actionable"
    hook_dir.mkdir()
    manifest = hook_dir / "kit-bundle-manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert _bundle_manifest_is_valid(hook_dir, manifest) is False


def test_pending_capture_manifest_rejects_unsafe_or_drifted_entries(tmp_path):
    from gateway.outbound_boundary import _bundle_manifest_is_valid
    hook_dir = tmp_path / "outbound-actionable"
    hook_dir.mkdir()
    for entry in ({"path": "../escape", "sha256": "x"}, {"path": "handler.py", "sha256": "wrong"}):
        files = [entry]
        unsigned = {"schema_version": 1, "files": files}
        (hook_dir / "kit-bundle-manifest.json").write_text(json.dumps({**unsigned, "bundle_sha256": hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}), encoding="utf-8")
        (hook_dir / "handler.py").write_text("# handler\n", encoding="utf-8")
        assert _bundle_manifest_is_valid(hook_dir, hook_dir / "kit-bundle-manifest.json") is False


def test_generic_hook_registry_keeps_unactivated_bundle_inert_when_attestation_capture_fails(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("name: outbound-actionable\nevents: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text("def handle(*_args):\n    return {'decision': 'allow'}\n", encoding="utf-8")
    monkeypatch.setattr("gateway.outbound_boundary.capture_pending_outbound_attestation", lambda _hook: (_ for _ in ()).throw(RuntimeError("capture failed")))

    registry = HookRegistry(hooks_dir=hook_dir.parent)
    registry.discover_and_load()

    assert registry.loaded_hooks == []


def test_shared_activation_record_must_bind_this_profile(tmp_path):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text("# handler\n", encoding="utf-8")
    _activate(hook_dir)
    record = json.loads((hook_dir / "release-activation.json").read_text(encoding="utf-8"))
    record["profile_id"] = "missing"
    (hook_dir / "release-activation.json").write_text(json.dumps(record), encoding="utf-8")
    os.chmod(hook_dir / "release-activation.json", 0o600)

    with pytest.raises(BoundaryLoadError, match="not release-activated"):
        load_installed_outbound_hooks(tmp_path)


def test_shared_activation_record_rejects_missing_or_tampered_record(tmp_path):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text("# handler\n", encoding="utf-8")
    (hook_dir / "release-tuple.json").write_text(json.dumps({"tuple_digest": "sha256:test"}), encoding="utf-8")
    (hook_dir / "release-activation.json").write_text(json.dumps({"tuple_digest": "sha256:test"}), encoding="utf-8")
    with pytest.raises(BoundaryLoadError, match="not release-activated"):
        load_installed_outbound_hooks(tmp_path)

    _activate(hook_dir)
    (hook_dir / "handler.py").write_text("# drift\n", encoding="utf-8")
    with pytest.raises(BoundaryLoadError, match="not release-activated"):
        load_installed_outbound_hooks(tmp_path)

    _activate(hook_dir)
    shared = hook_dir.parents[1] / "shared-activation.json"
    os.chmod(shared, 0o644)
    with pytest.raises(BoundaryLoadError, match="not release-activated"):
        load_installed_outbound_hooks(tmp_path)
    os.chmod(shared, 0o600)
    shared.write_bytes(shared.read_bytes() + b" ")
    with pytest.raises(BoundaryLoadError, match="not release-activated"):
        load_installed_outbound_hooks(tmp_path)


def test_installed_manifest_without_loaded_before_send_handler_fails_closed(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text("# handler\n", encoding="utf-8")
    _activate(hook_dir)

    class EmptyRegistry:
        loaded_hooks = []

        def __init__(self, *args, **kwargs):
            pass

        def discover_and_load(self):
            return None

    monkeypatch.setattr("gateway.hooks.HookRegistry", EmptyRegistry)
    with pytest.raises(BoundaryLoadError, match="missing before-send"):
        load_installed_outbound_hooks(tmp_path)


def test_after_send_emits_transport_disposition():
    hooks = HookRegistry()
    seen = []
    handler = lambda _event, context: seen.append(context)
    hooks._handlers["outbound:after_send"] = [handler]
    hooks._named_handlers["outbound:after_send"] = {"outbound-actionable": [handler]}

    outbound_after_send_sync(hooks, {"success": True, "send_result": {"message_id": "m1"}})

    assert seen == [{"success": True, "send_result": {"message_id": "m1"}}]


def test_after_send_does_not_fan_out_to_an_unrelated_loaded_hook():
    hooks = HookRegistry()
    seen = []
    hooks._named_handlers["outbound:after_send"] = {
        "outbound-actionable": [lambda _event, context: seen.append(("trusted", context))],
        "unrelated": [lambda _event, context: seen.append(("unrelated", context))],
    }
    hooks._handlers["outbound:after_send"] = [
        *hooks._named_handlers["outbound:after_send"]["outbound-actionable"],
        *hooks._named_handlers["outbound:after_send"]["unrelated"],
    ]

    outbound_after_send_sync(hooks, {"observer_event_id": "after-send:one"})

    assert seen == [("trusted", {"observer_event_id": "after-send:one"})]


def test_named_strict_collector_requires_the_trusted_hook_and_awaits_it():
    hooks = HookRegistry()
    with pytest.raises(RuntimeError, match="not registered"):
        asyncio.run(hooks.emit_collect_strict_named("outbound:after_send", "outbound-actionable"))

    async def trusted(_event, context):
        return {"event": context["observer_event_id"]}

    hooks._named_handlers["outbound:after_send"] = {"outbound-actionable": [trusted]}
    assert asyncio.run(hooks.emit_collect_strict_named(
        "outbound:after_send", "outbound-actionable", {"observer_event_id": "after-send:trusted"},
    )) == [{"event": "after-send:trusted"}]


def test_after_send_uses_legacy_collector_when_strict_is_unavailable():
    seen = []

    class LegacyHooks:
        async def emit_collect(self, event, context):
            seen.append((event, context))
            return []

    outbound_after_send_sync(LegacyHooks(), {"success": False})
    assert seen == [("outbound:after_send", {"success": False})]


def test_boundary_uses_legacy_collector_and_rejects_ambiguous_decisions():
    class LegacyHooks:
        async def emit_collect(self, _event, _context):
            return [{"decision": "allow"}, {"decision": "deny"}, "ignored"]

    decision = outbound_before_send_sync(LegacyHooks(), {"content": "raw"})
    assert decision.decision == "deny"
    assert decision.reason == "boundary_decision_missing_or_ambiguous"


def test_boundary_context_is_detached():
    from gateway.outbound_boundary import build_outbound_context

    source = {"route": "chat"}
    context = build_outbound_context(**source)
    source["route"] = "changed"
    assert context == {"route": "chat"}


def test_boundary_thread_bridge_propagates_handler_failure_inside_running_loop():
    class BrokenHooks:
        async def emit_collect_strict(self, _event, _context):
            raise RuntimeError("boom")

    async def invoke():
        return outbound_before_send_sync(BrokenHooks(), {"content": "raw"})

    decision = asyncio.run(invoke())
    assert decision.reason == "boundary_unavailable"


def test_manifest_loader_wraps_discovery_failure(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text("# handler\n", encoding="utf-8")
    _activate(hook_dir)

    class BrokenRegistry:
        def __init__(self, *args, **kwargs):
            pass

        def discover_and_load(self):
            raise RuntimeError("bad handler")

    monkeypatch.setattr("gateway.hooks.HookRegistry", BrokenRegistry)
    with pytest.raises(BoundaryLoadError, match="failed to load"):
        load_installed_outbound_hooks(tmp_path)


def test_manifest_loader_returns_an_active_boundary(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").write_text("# handler\n", encoding="utf-8")
    _activate(hook_dir)

    class ActiveRegistry:
        def __init__(self, *args, **kwargs):
            self.loaded_hooks = [{
                "name": "outbound-actionable",
                "path": str(hook_dir),
                "events": ["outbound:before_send"],
            }]

        def discover_and_load(self):
            return None

    monkeypatch.setattr("gateway.hooks.HookRegistry", ActiveRegistry)
    assert isinstance(load_installed_outbound_hooks(tmp_path), ActiveRegistry)


def test_loader_discovers_only_the_requested_profile_bundle(tmp_path):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text(
        "name: outbound-actionable\nevents: [outbound:before_send]\n", encoding="utf-8"
    )
    (hook_dir / "handler.py").write_text(
        "def handle(event_type, context):\n    return {'decision': 'allow', 'reason': 'profile-bound'}\n",
        encoding="utf-8",
    )
    _activate(hook_dir)

    hooks = load_installed_outbound_hooks(tmp_path)
    decision = outbound_before_send_sync(hooks, {"content": "safe"})

    assert decision.decision == "allow"
    assert hooks.loaded_hooks == [
        {
            "name": "outbound-actionable",
            "description": "",
            "events": ["outbound:before_send"],
            "path": str(hook_dir),
        }
    ]


def test_active_registry_executes_pinned_handler_bytes_after_path_replacement(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text(
        "name: outbound-actionable\nevents: [outbound:before_send]\n", encoding="utf-8"
    )
    (hook_dir / "handler.py").write_text(
        "def handle(*_args):\n    return {'decision': 'allow', 'reason': 'pinned'}\n",
        encoding="utf-8",
    )
    _activate(hook_dir)
    from gateway import outbound_boundary
    original_exec = outbound_boundary._exec_pinned_handler

    def replace_after_read(module_name, handler, source):
        (hook_dir / "handler.py").write_text("raise RuntimeError('replacement executed')\n", encoding="utf-8")
        return original_exec(module_name, handler, source)

    monkeypatch.setattr(outbound_boundary, "_exec_pinned_handler", replace_after_read)
    hooks = load_installed_outbound_hooks(tmp_path)

    assert outbound_before_send_sync(hooks, {"content": "safe"}).reason == "pinned"


def test_loader_fails_when_the_requested_bundle_handler_is_broken(tmp_path):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text(
        "name: outbound-actionable\nevents: [outbound:before_send]\n", encoding="utf-8"
    )
    (hook_dir / "handler.py").write_text("raise RuntimeError('broken bundle')\n", encoding="utf-8")
    _activate(hook_dir)

    with pytest.raises(BoundaryLoadError, match="missing before-send"):
        load_installed_outbound_hooks(tmp_path)


def test_loader_rejects_a_profile_hook_symlink(tmp_path):
    other = tmp_path / "other" / "hooks" / "outbound-actionable"
    other.mkdir(parents=True)
    (other / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (other / "handler.py").write_text("# handler\n", encoding="utf-8")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "outbound-actionable").symlink_to(other, target_is_directory=True)

    with pytest.raises(BoundaryLoadError, match="symlink"):
        load_installed_outbound_hooks(tmp_path)


def test_loader_rejects_missing_or_non_file_managed_members(tmp_path):
    hook_dir = tmp_path / "hooks" / "outbound-actionable"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").mkdir()
    (hook_dir / "handler.py").write_text("# handler\n", encoding="utf-8")
    with pytest.raises(BoundaryLoadError, match="incomplete"):
        load_installed_outbound_hooks(tmp_path)

    (hook_dir / "HOOK.yaml").rmdir()
    (hook_dir / "HOOK.yaml").write_text("events: [outbound:before_send]\n", encoding="utf-8")
    (hook_dir / "handler.py").unlink()
    with pytest.raises(BoundaryLoadError, match="incomplete"):
        load_installed_outbound_hooks(tmp_path)


def test_boundary_thread_bridge_returns_result_inside_running_loop():
    class AllowHooks:
        async def emit_collect_strict(self, _event, _context):
            return [{"decision": "allow", "reason": "safe"}]

    async def invoke():
        return outbound_before_send_sync(AllowHooks(), {"content": "raw"})

    decision = asyncio.run(invoke())
    assert decision.decision == "allow"
    assert decision.content == "raw"


def test_strict_collector_initializes_context_and_awaits_handlers():
    hooks = HookRegistry()

    async def async_handler(_event, context):
        assert context == {}
        return {"decision": "allow"}

    hooks._handlers["outbound:before_send"] = [async_handler]
    assert asyncio.run(hooks.emit_collect_strict("outbound:before_send")) == [{"decision": "allow"}]
