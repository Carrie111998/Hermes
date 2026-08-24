import json
from pathlib import Path

from hermes_wisdom.client import Draft
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
