import json
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wisdom.client import Draft, WisdomValidationError
from hermes_wisdom.contract import (
    PackageManifest,
    SystemSpecification,
    author_description_hash,
    canonical_json_bytes,
    sha256_address,
)
from hermes_wisdom.package import PackagePolicyError, verify_content_files
from hermes_wisdom.service import WisdomService
from hermes_wisdom.store import WisdomStore


class FakeClient:
    identity = {"owner": "user-1"}

    def __init__(self):
        self.uploaded = 0
        self.submissions = []

    def upload_private_objects(self, objects):
        self.uploaded += len(objects)

    def submit_draft(self, **payload):
        self.submissions.append(payload)
        return Draft(
            id="draft-1",
            orgId="org-1",
            ownerUserId="user-1",
            slug=payload["slug"],
            draftCommit=payload["commit"],
            contentHash=payload["content_hash"],
            authorDescription=payload["description"],
            authorDescriptionHash=None,
            state="ready",
            packageManifestHash=None,
            packageManifestSchemaVersion=1,
            systemSpec=None,
            scan=None,
            scanVerdict="pass",
            explanation=None,
            updatedAt="revision-1",
        )


class InstallClient:
    def __init__(self, *, fail_record: bool = False):
        manifest = PackageManifest(
            name="managed-skill",
            requirements=SystemSpecification.model_validate({
                "hermes": {"minimum_version": "0.1.0"}
            }),
        )
        self.files = [
            ("SKILL.md", "file", b"# Managed\n"),
            (
                "skill.manifest.json",
                "file",
                canonical_json_bytes(manifest.model_dump(mode="json")),
            ),
        ]
        self.fail_record = fail_record

    def skill(self, _skill_id):
        return SimpleNamespace(
            skill={
                "state": "active",
                "slug": "managed-skill",
                "takedown_generation": 0,
            },
            versions=[{"version": 1}],
        )

    def version(self, _skill_id, _version):
        manifest = json.loads(self.files[1][2])
        return SimpleNamespace(version={"system_spec": manifest["requirements"]})

    def content(
        self,
        _skill_id,
        _version,
        *,
        installation_id,
        takedown_generation,
    ):
        assert installation_id.startswith("hwi_")
        assert takedown_generation == 0
        _records, content_hash = verify_content_files(self.files)
        return SimpleNamespace(content_hash=content_hash), self.files

    def record_install(self, **_kwargs):
        if self.fail_record:
            self.fail_record = False
            raise RuntimeError("network down")
        return SimpleNamespace(effective_update_mode="MANUAL")


class SetupClient:
    display_scopes = ("wisdom:read", "wisdom:install")
    identity = {"claims": {"tool_gateway_admin": True}}

    def __init__(self, org_id: str = "org-1"):
        self.display_org_id = org_id

    def capability(self):
        return {"capabilities": ["wisdom"]}

    def register_identity(self, installation_id):
        return {"installation_id": installation_id, "state": "active"}


class ReviewClient:
    identity = {"owner": "user-1"}

    def __init__(self):
        manifest = canonical_json_bytes(
            PackageManifest(
                name="reviewed-skill",
                requirements=SystemSpecification.model_validate(
                    {"hermes": {"minimum_version": "0.1.0"}}
                ),
            ).model_dump(mode="json")
        )
        self.files = [
            ("SKILL.md", "file", b"# Reviewed skill\n"),
            ("skill.manifest.json", "file", manifest),
        ]
        self.description = "Resolve incidents safely."
        self.revision = "revision-1"
        self.approvals: list[dict] = []
        self.publications = 0

    def _draft(self):
        _records, content_hash = verify_content_files(self.files)
        manifest = next(body for path, _, body in self.files if path == "skill.manifest.json")
        return Draft(
            id="draft-review",
            orgId="org-1",
            ownerUserId="user-1",
            slug="reviewed-skill",
            draftCommit="sha256:" + "a" * 64,
            contentHash=content_hash,
            authorDescription=self.description,
            authorDescriptionHash=author_description_hash(self.description),
            state="ready",
            packageManifestHash=sha256_address(manifest),
            packageManifestSchemaVersion=1,
            systemSpec=None,
            scan={"verdict": "pass", "findings": []},
            scanVerdict="pass",
            explanation="Mechanical facts.",
            updatedAt=self.revision,
        )

    def reconstruct_draft(self, _draft_id):
        draft = self._draft()
        records, content_hash = verify_content_files(self.files)
        return SimpleNamespace(
            detail=SimpleNamespace(
                draft=draft,
                effective_policy={"publication_policy": "open", "policy_version": 1},
            ),
            files=self.files,
            content_files=records,
            content_hash=content_hash,
        )

    def approve(self, _draft_id, **hashes):
        self.approvals.append(hashes)
        return self._draft().model_copy(update={"state": "owner_approved"})

    def publish(self, _draft_id, *, content_hash):
        self.publications += 1
        return {"state": "published", "content_hash": content_hash}


def _review_service(tmp_path: Path, *, client: ReviewClient):
    store = WisdomStore(tmp_path / "state")
    skill_path = tmp_path / "skills" / "reviewed-skill"
    skill_path.mkdir(parents=True)
    skill_id = store.register_skill(
        skill_path,
        content_hash=client._draft().contentHash,
        source_kind="local",
    )
    draft = client._draft()
    store.record_draft(
        {
            "id": draft.id,
            "skill_id": skill_id,
            "source_hash": draft.contentHash,
            "overlay_path": str(skill_path),
            "draft_commit": draft.draftCommit,
            "server_revision": draft.updatedAt,
            "state": draft.state,
            "description": draft.authorDescription or "",
            "content_hash": draft.contentHash,
            "description_hash": draft.authorDescriptionHash or "",
            "manifest_hash": draft.packageManifestHash or "",
        }
    )
    return WisdomService(store=store, client=client)


def _install_service(monkeypatch, tmp_path: Path, *, client: InstallClient):
    skills = tmp_path / "skills"
    monkeypatch.setattr("hermes_wisdom.service.get_skills_dir", lambda: skills)
    monkeypatch.setattr(
        "hermes_wisdom.service._scan_summary",
        lambda _path: {
            "guard": {"allowed": True, "findings": [], "reason": None},
            "skill_evaluator": {"status": "disabled", "findings": []},
        },
    )
    store = WisdomStore(tmp_path / "state")
    store.installation_identity()
    store.verify_installation_identity("org-1")
    return WisdomService(store=store, client=client)


def test_prepare_requires_local_owner_edit_before_any_network(
    monkeypatch, tmp_path: Path
):
    skill = tmp_path / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Test.\n---\n# Test\n", encoding="utf-8"
    )
    fake = FakeClient()
    service = WisdomService(store=WisdomStore(tmp_path / "state"), client=fake)
    monkeypatch.setattr("hermes_wisdom.service._find_skill_dir", lambda _name: skill)
    monkeypatch.setattr(service, "_eligible_paths", lambda: [skill])
    monkeypatch.setattr(
        "hermes_wisdom.service.draft_description", lambda _body: "Drafted locally."
    )

    prepared = service.suggest("my-skill")
    assert prepared["network_submission"] is False
    assert fake.uploaded == 0
    overlay = Path(prepared["overlay_path"])
    manifest = json.loads((overlay / "skill.manifest.json").read_text(encoding="utf-8"))
    manifest["requirements"]["known_limitations"] = ["Owner reviewed limitation"]
    (overlay / "skill.manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )

    submitted = service.suggest(
        "my-skill",
        description="Owner-approved copy.",
        system_specification=manifest["requirements"],
    )
    assert submitted["draft"]["id"] == "draft-1"
    assert fake.uploaded > 0
    assert fake.submissions[0].keys() == {
        "slug",
        "commit",
        "content_hash",
        "description",
    }
    serialized = json.dumps(fake.submissions[0])
    for forbidden in ("usage", "refinement", "candidate", "ranking", "stability"):
        assert forbidden not in serialized


def test_setup_persists_explicit_disclosure_and_enables_the_profile(
    monkeypatch, tmp_path: Path
):
    skills = tmp_path / "skills"
    monkeypatch.setattr("hermes_wisdom.service.get_skills_dir", lambda: skills)
    config = {"wisdom": {"enabled": False}}

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: copy.deepcopy(config))

    def save_config(value):
        config.clear()
        config.update(copy.deepcopy(value))

    monkeypatch.setattr("hermes_cli.config.save_config", save_config)
    service = WisdomService(store=WisdomStore(tmp_path / "state"), client=SetupClient())

    with pytest.raises(PackagePolicyError, match="explicit acceptance"):
        service.setup()
    first = service.setup(disclosure_accepted=True)
    second = service.setup(disclosure_accepted=True)

    assert config["wisdom"]["enabled"] is True
    assert (
        config["wisdom"]["disclosure_acknowledged_at"]
        == first["disclosure_acknowledged_at"]
    )
    assert second["disclosure_acknowledged_at"] == first["disclosure_acknowledged_at"]
    assert second["installation_id"] == first["installation_id"]

    service._client = SetupClient("org-2")
    third = service.setup(disclosure_accepted=True)
    assert third["installation_id"] != first["installation_id"]
    assert service.store.active_org_id() == "org-2"


def test_status_does_not_enroll_an_unconfigured_profile(monkeypatch, tmp_path: Path):
    store = WisdomStore(tmp_path / "state")
    service = WisdomService(store=store, client=SetupClient())
    monkeypatch.setattr("hermes_wisdom.service._config", lambda: {})

    status = service.status()

    assert status["configured"] is False
    assert status["setup_required_reason"] == "not_configured"
    assert status["installation_id"] is None
    assert store.existing_installation_identity() is None
    with pytest.raises(PackagePolicyError, match="wisdom setup"):
        service.require_setup()


def test_setup_guard_rejects_a_changed_authenticated_org(monkeypatch, tmp_path: Path):
    store = WisdomStore(tmp_path / "state")
    store.installation_identity()
    store.verify_installation_identity("org-1")
    service = WisdomService(store=store, client=SetupClient("org-2"))
    monkeypatch.setattr(
        "hermes_wisdom.service._config",
        lambda: {
            "enabled": True,
            "disclosure_acknowledged_at": "2026-08-25T00:00:00+00:00",
        },
    )

    with pytest.raises(PackagePolicyError, match="organization changed"):
        service.require_setup()

    status = service.status()
    assert status["configured"] is False
    assert status["setup_required_reason"] == "organization_changed"
    assert status["verified_org_id"] == "org-1"
    assert status["authenticated_org_id"] == "org-2"


def test_org_change_does_not_rotate_identity_before_gateway_accepts(
    monkeypatch, tmp_path: Path
):
    class RejectingSetupClient(SetupClient):
        def register_identity(self, installation_id):
            raise RuntimeError("gateway rejected identity")

    skills = tmp_path / "skills"
    monkeypatch.setattr("hermes_wisdom.service.get_skills_dir", lambda: skills)
    store = WisdomStore(tmp_path / "state")
    old_identity = store.installation_identity()
    store.verify_installation_identity("org-1")
    service = WisdomService(store=store, client=RejectingSetupClient("org-2"))

    with pytest.raises(RuntimeError, match="gateway rejected"):
        service.setup(disclosure_accepted=True)

    assert store.existing_installation_identity() == old_identity
    assert store.active_org_id() == "org-1"


def test_org_change_switches_marker_before_local_ledger(monkeypatch, tmp_path: Path):
    skills = tmp_path / "skills"
    monkeypatch.setattr("hermes_wisdom.service.get_skills_dir", lambda: skills)
    store = WisdomStore(tmp_path / "state")
    store.installation_identity()
    store.verify_installation_identity("org-1")
    service = WisdomService(store=store, client=SetupClient("org-2"))
    monkeypatch.setattr(
        store,
        "activate_installation_identity",
        lambda *_args: (_ for _ in ()).throw(OSError("injected ledger failure")),
    )

    with pytest.raises(OSError, match="injected ledger"):
        service.setup(disclosure_accepted=True)

    assert (skills / "_wisdom" / ".active_org").read_text() == "org-2\n"
    assert store.active_org_id() == "org-1"


def test_approval_requires_a_complete_review_receipt(tmp_path: Path):
    service = _review_service(tmp_path, client=ReviewClient())

    with pytest.raises(PackagePolicyError, match="fresh complete-package review"):
        service.approve("draft-review")


@pytest.mark.parametrize("mutation", ["content", "description", "manifest", "revision"])
def test_approval_rejects_each_stale_receipt_binding(tmp_path: Path, mutation: str):
    client = ReviewClient()
    service = _review_service(tmp_path, client=client)
    service.review("draft-review", acknowledge=True)

    if mutation == "content":
        client.files[0] = ("SKILL.md", "file", b"# Mutated content\n")
    elif mutation == "description":
        client.description = "Changed owner copy."
    elif mutation == "manifest":
        changed = json.loads(client.files[1][2])
        changed["name"] = "changed-manifest"
        client.files[1] = (
            "skill.manifest.json",
            "file",
            canonical_json_bytes(changed),
        )
    else:
        client.revision = "revision-2"

    with pytest.raises(PackagePolicyError, match="receipt is stale"):
        service.approve("draft-review")
    assert client.approvals == []
    assert client.publications == 0


def test_successful_approval_consumes_receipt_and_replay_is_denied(tmp_path: Path):
    client = ReviewClient()
    service = _review_service(tmp_path, client=client)
    store = service.store
    reviewed = service.review("draft-review", acknowledge=True)

    result = service.approve("draft-review")

    assert result["publication"]["state"] == "published"
    assert client.approvals == [
        {
            "content_hash": reviewed["hashes"]["content"],
            "description_hash": reviewed["hashes"]["author_description"],
            "manifest_hash": reviewed["hashes"]["package_manifest"],
        }
    ]
    assert client.publications == 1
    assert store.receipt("draft-review") is None
    with pytest.raises(PackagePolicyError, match="fresh complete-package review"):
        service.approve("draft-review")
    assert client.publications == 1


def test_setup_rejects_an_unsafe_org_path_before_writing_marker(
    monkeypatch, tmp_path: Path
):
    skills = tmp_path / "skills"
    monkeypatch.setattr("hermes_wisdom.service.get_skills_dir", lambda: skills)
    service = WisdomService(
        store=WisdomStore(tmp_path / "state"), client=SetupClient("../other-org")
    )

    with pytest.raises(WisdomValidationError, match="malformed"):
        service.setup(disclosure_accepted=True)

    assert not (skills / "_wisdom" / ".active_org").exists()


def test_install_retries_from_staged_bytes_after_directory_swap_failure(
    monkeypatch, tmp_path: Path
):
    client = InstallClient()
    service = _install_service(monkeypatch, tmp_path, client=client)
    plan = service.install_plan("skill-1")
    real_replace = __import__("os").replace
    failed = False

    def replace_once(source, destination):
        nonlocal failed
        if not failed and Path(destination).name == "managed-skill":
            failed = True
            raise OSError("injected swap failure")
        return real_replace(source, destination)

    monkeypatch.setattr("hermes_wisdom.service.os.replace", replace_once)
    with pytest.raises(OSError, match="injected"):
        service.install_apply(plan["receipt"])
    assert service.store.pending_operations()[0]["phase"] == "staged"

    monkeypatch.setattr("hermes_wisdom.service.os.replace", real_replace)
    assert service.reconcile_pending_install_records() == ["skill-1"]
    installation = service.store.installation("skill-1")
    assert installation["state"] == "active"
    assert Path(installation["target_path"], "SKILL.md").read_text() == "# Managed\n"


def test_install_recovery_verifies_target_when_swap_won_before_journal_advance(
    monkeypatch, tmp_path: Path
):
    client = InstallClient()
    service = _install_service(monkeypatch, tmp_path, client=client)
    plan = service.install_plan("skill-1")
    real_advance = service.store.advance
    failed = False

    def advance_once(operation_id, phase, *, done=False):
        nonlocal failed
        if not failed and phase == "files_committed":
            failed = True
            raise OSError("injected journal failure")
        return real_advance(operation_id, phase, done=done)

    monkeypatch.setattr(service.store, "advance", advance_once)
    with pytest.raises(OSError, match="journal"):
        service.install_apply(plan["receipt"])
    pending = service.store.pending_operations()[0]
    payload = json.loads(pending["payload_json"])
    assert pending["phase"] == "staged"
    assert not Path(payload["staging_path"]).exists()
    assert Path(payload["target_path"]).is_dir()

    monkeypatch.setattr(service.store, "advance", real_advance)
    result = service.install_apply(plan["receipt"])
    assert result["installed"] is True


def test_install_retries_only_gateway_record_after_local_commit(
    monkeypatch, tmp_path: Path
):
    client = InstallClient(fail_record=True)
    service = _install_service(monkeypatch, tmp_path, client=client)
    plan = service.install_plan("skill-1")
    with pytest.raises(RuntimeError, match="network down"):
        service.install_apply(plan["receipt"])
    assert service.store.pending_operations()[0]["phase"] == "local_ledger_committed"
    result = service.install_apply(plan["receipt"])
    assert result["installed"] is True
    assert service.store.pending_operations() == []
