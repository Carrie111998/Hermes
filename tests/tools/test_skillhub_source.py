"""Tests for the opt-in native SkillHub source adapter."""

from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock, patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import skills_hub as hub
from tools.skills_hub import (
    HubLockFile,
    SkillHubSource,
    SkillMeta,
    SkillBundle,
    SkillSource,
    bundle_content_hash,
    content_hash,
    create_source_router,
    ensure_hub_dirs,
    refresh_skillhub_installed_skills,
)


def _json_response(data, *, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {"code": 0, "msg": "ok", "data": data}
    return response


def _skill_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: deploy-k8s\ndescription: deploy\n---\n\n# Deploy\n",
        )
        archive.writestr("references/runbook.md", "runbook")
        archive.writestr("../escape.txt", "must not extract")
    return out.getvalue()


class TestSkillHubSource:
    def test_search_maps_cli_api_items_and_filters_namespace(self):
        source = SkillHubSource(
            "https://skillhub.example.internal",
            namespace="company-common",
            token="secret",
        )
        response = _json_response({
            "items": [
                {
                    "namespace": "company-common",
                    "slug": "deploy-k8s",
                    "latestVersion": "1.2.0",
                    "summary": "Deploy Kubernetes services",
                },
                {
                    "namespace": "other-team",
                    "slug": "same-name",
                    "latestVersion": "1.0.0",
                    "summary": "Other team",
                },
            ],
            "total": 2,
            "limit": 20,
        })

        with patch("tools.skills_hub.httpx.get", return_value=response) as mock_get:
            result = source.search("deploy", limit=20)

        assert len(result) == 1
        assert result[0].source == "skillhub"
        assert result[0].identifier == "skillhub://company-common/deploy-k8s"
        assert result[0].extra["latest_version"] == "1.2.0"
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"

    def test_inspect_uses_resolve_fingerprint_without_following_download_url(self):
        source = SkillHubSource("https://skillhub.example.internal")
        response = _json_response({
            "namespace": "company-common",
            "slug": "deploy-k8s",
            "version": "1.2.0",
            "versionId": 4,
            "fingerprint": "sha256:abc",
            "downloadUrl": "https://untrusted.example/download",
        })

        with patch("tools.skills_hub.httpx.get", return_value=response):
            result = source.inspect("skillhub://company-common/deploy-k8s")

        assert isinstance(result, SkillMeta)
        assert result.extra["fingerprint"] == "sha256:abc"
        assert result.extra["source_url"].endswith(
            "/api/cli/v1/skills/company-common/deploy-k8s/resolve"
        )

    def test_fetch_resolves_latest_and_extracts_only_safe_text_files(self):
        source = SkillHubSource("https://skillhub.example.internal")
        with patch.object(
            source,
            "_request_json",
            return_value={"version": "1.2.0", "fingerprint": "sha256:abc"},
        ), patch.object(source, "_download", return_value=_skill_zip()):
            bundle = source.fetch("skillhub://company-common/deploy-k8s")

        assert isinstance(bundle, SkillBundle)
        assert bundle.name == "deploy-k8s"
        assert bundle.source == "skillhub"
        assert bundle.identifier == "skillhub://company-common/deploy-k8s"
        assert set(bundle.files) == {"SKILL.md", "references/runbook.md"}
        assert bundle.metadata["version"] == "1.2.0"

    def test_fetch_rejects_bundle_without_root_skill_md(self):
        source = SkillHubSource("https://skillhub.example.internal")
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("README.md", "not a skill")

        with patch.object(source, "_request_json", return_value={"version": "1.0.0"}), \
             patch.object(source, "_download", return_value=content.getvalue()):
            assert source.fetch("company-common/not-a-skill") is None

    def test_invalid_registry_and_identifier_are_rejected(self):
        try:
            SkillHubSource("not-a-url")
        except ValueError as exc:
            assert "absolute HTTP(S)" in str(exc)
        else:
            raise AssertionError("invalid registry was accepted")

        source = SkillHubSource("https://skillhub.example.internal")
        assert source.inspect("skillhub://company-common/../escape") is None


def test_router_adds_skillhub_only_when_configured():
    with patch(
        "tools.skills_hub._skillhub_settings",
        return_value={
            "registry": "https://skillhub.example.internal",
            "namespace": "company-common",
            "token": None,
            "auto_update": True,
        },
    ):
        sources = create_source_router()

    assert sum(1 for source in sources if source.source_id() == "skillhub") == 1


def test_router_does_not_add_skillhub_without_registry():
    with patch("tools.skills_hub._skillhub_settings", return_value=None):
        sources = create_source_router()

    assert all(source.source_id() != "skillhub" for source in sources)


def test_background_refresh_updates_real_temp_profile_and_lockfile(tmp_path):
    class FakeSkillHub(SkillSource):
        def source_id(self):
            return "skillhub"

        def fetch(self, identifier):
            return SkillBundle(
                "deploy-k8s",
                {"SKILL.md": "---\nname: deploy-k8s\nversion: 1.1.0\n---\n\n# new\n"},
                "skillhub",
                identifier,
                "community",
                {"registry": "https://skillhub.example.internal", "version": "1.1.0"},
            )

        def search(self, query, limit=10):
            return []

        def inspect(self, identifier):
            return None

    token = set_hermes_home_override(tmp_path)
    try:
        skill_dir = tmp_path / "skills" / "team" / "deploy-k8s"
        skill_dir.mkdir(parents=True)
        old_content = "---\nname: deploy-k8s\nversion: 1.0.0\n---\n\n# old\n"
        (skill_dir / "SKILL.md").write_text(old_content)
        ensure_hub_dirs()

        identifier = "skillhub://company-common/deploy-k8s"
        old_bundle = SkillBundle(
            "deploy-k8s", {"SKILL.md": old_content}, "skillhub", identifier, "community"
        )
        lock = HubLockFile()
        lock.record_install(
            "deploy-k8s",
            "skillhub",
            identifier,
            "community",
            "clean",
            bundle_content_hash(old_bundle),
            "team/deploy-k8s",
            ["SKILL.md"],
        )

        with patch.object(
            hub,
            "_skillhub_settings",
            return_value={
                "registry": "https://skillhub.example.internal",
                "namespace": "company-common",
                "token": None,
                "auto_update": True,
            },
        ):
            result = refresh_skillhub_installed_skills(
                lock=lock,
                sources=[FakeSkillHub()],
            )

        assert result[0]["status"] == "updated"
        assert "1.1.0" in (skill_dir / "SKILL.md").read_text()
        installed = lock.get_installed("deploy-k8s")
        assert installed is not None
        assert installed["source"] == "skillhub"
        assert installed["content_hash"] == content_hash(skill_dir)
    finally:
        reset_hermes_home_override(token)
