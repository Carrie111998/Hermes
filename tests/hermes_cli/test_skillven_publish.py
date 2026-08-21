"""RED-first tests for hermes_cli/skillven_publish.py (Hermes Agent 18-B).

Covers bundle validation, payload building, token resolution, the HTTP
client's preview/publish/read-back calls, and the do_skillven_publish
orchestration (scan gate, preview gate, confirmation gate, no secret leaks).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from hermes_cli import skillven_publish


class _FakeConsole:
    def __init__(self):
        self.lines: list[str] = []

    def print(self, msg=""):
        self.lines.append(str(msg))


def _write_bundle(
    tmp_path,
    *,
    name="demo-skill",
    version="1.0.0",
    extra_files=None,
    skill_md=None,
    metadata_overrides=None,
):
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text(
        skill_md or "# Demo Skill\n\nBody text with no frontmatter.\n",
        encoding="utf-8",
    )
    metadata = {
        "name": name,
        "version": version,
        "description": "A demo skill for tests.",
        "license": "MIT",
        "compatible_agents": ["claude", "hermes"],
        "required_tools": [],
        "permissions": [],
        "risk_level": "low",
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)
    (root / "metadata.yaml").write_text(yaml.safe_dump(metadata), encoding="utf-8")
    if extra_files:
        for rel, content in extra_files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Bundle building / validation (spec items 2, 3)
# ---------------------------------------------------------------------------


def test_build_skillven_payload_includes_all_files_base64_encoded(tmp_path):
    root = _write_bundle(
        tmp_path, extra_files={"reference/notes.md": "reference content"}
    )

    payload = skillven_publish.build_skillven_payload(root)

    paths = sorted(f["path"] for f in payload["files"])
    assert paths == ["SKILL.md", "metadata.yaml", "reference/notes.md"]
    by_path = {f["path"]: f["content_base64"] for f in payload["files"]}
    assert (
        base64.b64decode(by_path["reference/notes.md"]).decode() == "reference content"
    )
    assert payload["metadata"]["name"] == "demo-skill"
    assert payload["metadata"]["version"] == "1.0.0"


def test_build_skillven_payload_requires_metadata_yaml(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text("# Demo\n", encoding="utf-8")

    with pytest.raises(skillven_publish.SkillvenPublishError, match="metadata.yaml"):
        skillven_publish.build_skillven_payload(root)


def test_build_skillven_payload_requires_skill_md(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    (root / "metadata.yaml").write_text(yaml.safe_dump({"name": "x"}), encoding="utf-8")

    with pytest.raises(skillven_publish.SkillvenPublishError):
        skillven_publish.build_skillven_payload(root)


def test_build_skillven_payload_rejects_frontmatter_skill_md(tmp_path):
    root = _write_bundle(tmp_path, skill_md="---\nname: demo\n---\nBody\n")

    with pytest.raises(skillven_publish.SkillvenPublishError, match="frontmatter"):
        skillven_publish.build_skillven_payload(root)


def test_build_skillven_payload_rejects_incomplete_metadata(tmp_path):
    root = _write_bundle(tmp_path)
    (root / "metadata.yaml").write_text(
        yaml.safe_dump({"name": "demo-skill"}), encoding="utf-8"
    )

    with pytest.raises(skillven_publish.SkillvenPublishError, match="missing"):
        skillven_publish.build_skillven_payload(root)


def test_collect_bundle_files_rejects_symlink(tmp_path):
    root = _write_bundle(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)

    with pytest.raises(skillven_publish.SkillvenPublishError, match="symlink"):
        skillven_publish.build_skillven_payload(root)


def test_validate_relative_path_rejects_escape_absolute_and_nul(tmp_path):
    root = tmp_path

    with pytest.raises(skillven_publish.SkillvenPublishError):
        skillven_publish._validate_relative_path(root, Path("../escape.txt"))
    with pytest.raises(skillven_publish.SkillvenPublishError):
        skillven_publish._validate_relative_path(root, Path("/etc/passwd"))
    with pytest.raises(skillven_publish.SkillvenPublishError):
        skillven_publish._validate_relative_path(root, Path("bad\x00name"))


def test_check_bundle_limits_rejects_oversize_file(tmp_path):
    with pytest.raises(skillven_publish.SkillvenPublishError, match="too large"):
        skillven_publish._check_bundle_limits([
            ("SKILL.md", b"x" * (skillven_publish.MAX_FILE_BYTES + 1))
        ])


def test_check_bundle_limits_allows_file_exactly_max_bytes():
    skillven_publish._check_bundle_limits([
        ("SKILL.md", b"x" * skillven_publish.MAX_FILE_BYTES)
    ])


def test_check_bundle_limits_allows_bundle_exactly_max_bytes():
    files = [(f"f{i}.bin", b"x" * skillven_publish.MAX_FILE_BYTES) for i in range(4)]
    assert sum(len(c) for _, c in files) == skillven_publish.MAX_BUNDLE_BYTES

    skillven_publish._check_bundle_limits(files)


def test_check_bundle_limits_rejects_bundle_over_max_bytes():
    files = [(f"f{i}.bin", b"x" * skillven_publish.MAX_FILE_BYTES) for i in range(4)]
    files.append(("extra.bin", b"x"))
    assert sum(len(c) for _, c in files) == skillven_publish.MAX_BUNDLE_BYTES + 1

    with pytest.raises(skillven_publish.SkillvenPublishError, match="too large"):
        skillven_publish._check_bundle_limits(files)


def test_check_bundle_limits_allows_max_file_count():
    files = [(f"f{i}.txt", b"x") for i in range(skillven_publish.MAX_BUNDLE_FILE_COUNT)]

    skillven_publish._check_bundle_limits(files)


def test_check_bundle_limits_rejects_over_max_file_count():
    files = [
        (f"f{i}.txt", b"x") for i in range(skillven_publish.MAX_BUNDLE_FILE_COUNT + 1)
    ]

    with pytest.raises(skillven_publish.SkillvenPublishError, match="(?i)too many files"):
        skillven_publish._check_bundle_limits(files)


def test_check_bundle_limits_allows_max_path_depth():
    path = "/".join(f"d{i}" for i in range(skillven_publish.MAX_PATH_DEPTH - 1)) + "/file.md"
    assert len(path.split("/")) == skillven_publish.MAX_PATH_DEPTH

    skillven_publish._check_bundle_limits([(path, b"x")])


def test_check_bundle_limits_rejects_over_max_path_depth():
    path = "/".join(f"d{i}" for i in range(skillven_publish.MAX_PATH_DEPTH)) + "/file.md"
    assert len(path.split("/")) == skillven_publish.MAX_PATH_DEPTH + 1

    with pytest.raises(skillven_publish.SkillvenPublishError, match="(?i)too deep"):
        skillven_publish._check_bundle_limits([(path, b"x")])


# ---------------------------------------------------------------------------
# Token resolution (spec item 4)
# ---------------------------------------------------------------------------


def test_get_publish_token_missing_raises_without_network(monkeypatch):
    monkeypatch.delenv("SKILLVEN_PUBLISH_TOKEN", raising=False)

    with pytest.raises(
        skillven_publish.SkillvenPublishError, match="SKILLVEN_PUBLISH_TOKEN"
    ):
        skillven_publish.get_publish_token()


def test_get_publish_token_reads_secret_env_var(monkeypatch):
    monkeypatch.setenv("SKILLVEN_PUBLISH_TOKEN", "tkn-abc")

    assert skillven_publish.get_publish_token() == "tkn-abc"


# ---------------------------------------------------------------------------
# SkillvenClient HTTP mapping (spec items 5, 6)
# ---------------------------------------------------------------------------


def test_client_attaches_bearer_token_header():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("authorization"))
        return httpx.Response(
            200, json={"preview": True, "installable": True, "warnings": []}
        )

    client = skillven_publish.SkillvenClient(
        "https://example.test", "tkn-secret", transport=httpx.MockTransport(handler)
    )
    client.preview({"metadata": {}, "files": []})

    assert captured == ["Bearer tkn-secret"]


@pytest.mark.parametrize(
    "status,expected_snippet",
    [(401, "invalid"), (403, "own"), (409, "duplicate")],
)
def test_client_maps_known_error_codes_to_specific_safe_messages(
    status, expected_snippet
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"code": "x", "message": "server detail", "secret": "leak-me"}
        )

    client = skillven_publish.SkillvenClient(
        "https://example.test", "tkn-secret", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(skillven_publish.SkillvenPublishError) as exc_info:
        client.preview({"metadata": {}, "files": []})

    msg = str(exc_info.value)
    assert expected_snippet in msg.lower()
    assert "leak-me" not in msg
    assert "tkn-secret" not in msg


def test_client_does_not_dump_full_response_body_on_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "code": "internal",
                "message": "boom",
                "stack_trace": "TOKEN_LEAK tkn-secret",
            },
        )

    client = skillven_publish.SkillvenClient(
        "https://example.test", "tkn-secret", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(skillven_publish.SkillvenPublishError) as exc_info:
        client.preview({"metadata": {}, "files": []})

    msg = str(exc_info.value)
    assert "internal" in msg
    assert "boom" in msg
    assert "TOKEN_LEAK" not in msg
    assert "tkn-secret" not in msg


def test_client_maps_timeout_to_safe_message():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    client = skillven_publish.SkillvenClient(
        "https://example.test", "tkn-secret", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(skillven_publish.SkillvenPublishError, match="(?i)time"):
        client.preview({"metadata": {}, "files": []})


def test_client_maps_network_error_to_safe_message():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = skillven_publish.SkillvenClient(
        "https://example.test", "tkn-secret", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(skillven_publish.SkillvenPublishError, match="(?i)network"):
        client.preview({"metadata": {}, "files": []})


# ---------------------------------------------------------------------------
# do_skillven_publish orchestration (spec items 2, 5, 6, 7)
# ---------------------------------------------------------------------------


def test_do_skillven_publish_aborts_before_any_client_when_scan_dangerous(tmp_path):
    root = _write_bundle(tmp_path)
    created = []

    def factory(registry, token):
        created.append((registry, token))
        raise AssertionError("client must not be created for a dangerous scan verdict")

    console = _FakeConsole()
    skillven_publish.do_skillven_publish(
        root,
        registry="https://example.test",
        scan_result=SimpleNamespace(verdict="dangerous"),
        console=console,
        client_factory=factory,
    )

    assert created == []


def test_do_skillven_publish_aborts_when_preview_has_warnings(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLVEN_PUBLISH_TOKEN", "tkn-secret")
    root = _write_bundle(tmp_path)
    published = []

    class FakeClient:
        def __init__(self, registry, token):
            pass

        def preview(self, payload):
            return {"preview": True, "installable": True, "warnings": ["needs review"]}

        def publish(self, payload):
            published.append(payload)
            return {}

        def get_version(self, name, version):
            return {}

        def close(self):
            pass

    console = _FakeConsole()
    skillven_publish.do_skillven_publish(
        root,
        registry="https://example.test",
        scan_result=SimpleNamespace(verdict="safe"),
        console=console,
        client_factory=lambda r, t: FakeClient(r, t),
        confirm=lambda prompt: True,
    )

    assert published == []


def test_do_skillven_publish_aborts_when_not_installable(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLVEN_PUBLISH_TOKEN", "tkn-secret")
    root = _write_bundle(tmp_path)
    published = []

    class FakeClient:
        def __init__(self, registry, token):
            pass

        def preview(self, payload):
            return {"preview": True, "installable": False, "warnings": []}

        def publish(self, payload):
            published.append(payload)
            return {}

        def get_version(self, name, version):
            return {}

        def close(self):
            pass

    console = _FakeConsole()
    skillven_publish.do_skillven_publish(
        root,
        registry="https://example.test",
        scan_result=SimpleNamespace(verdict="safe"),
        console=console,
        client_factory=lambda r, t: FakeClient(r, t),
        confirm=lambda prompt: True,
    )

    assert published == []


def test_do_skillven_publish_aborts_when_user_declines(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLVEN_PUBLISH_TOKEN", "tkn-secret")
    root = _write_bundle(tmp_path)
    published = []

    class FakeClient:
        def __init__(self, registry, token):
            pass

        def preview(self, payload):
            return {"preview": True, "installable": True, "warnings": []}

        def publish(self, payload):
            published.append(payload)
            return {}

        def get_version(self, name, version):
            return {}

        def close(self):
            pass

    console = _FakeConsole()
    skillven_publish.do_skillven_publish(
        root,
        registry="https://example.test",
        scan_result=SimpleNamespace(verdict="safe"),
        console=console,
        client_factory=lambda r, t: FakeClient(r, t),
        confirm=lambda prompt: False,
    )

    assert published == []


def test_do_skillven_publish_approved_sends_preview_then_publish_same_payload_no_leak(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SKILLVEN_PUBLISH_TOKEN", "tkn-secret-value")
    root = _write_bundle(tmp_path)

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/skills/preview":
            return httpx.Response(
                200, json={"preview": True, "installable": True, "warnings": []}
            )
        if request.url.path == "/v1/skills" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "name": body["metadata"]["name"],
                    "version": body["metadata"]["version"],
                    "installable": True,
                    "source_skill_md_sha256": "deadbeef",
                    "files": body["files"],
                },
            )
        if request.url.path.startswith("/v1/skills/") and request.method == "GET":
            return httpx.Response(200, json={"version": "1.0.0", "installable": True})
        return httpx.Response(404, json={"code": "not_found", "message": "no route"})

    console = _FakeConsole()
    skillven_publish.do_skillven_publish(
        root,
        registry="https://example.test",
        scan_result=SimpleNamespace(verdict="safe"),
        console=console,
        client_factory=lambda r, t: skillven_publish.SkillvenClient(
            r, t, transport=httpx.MockTransport(handler)
        ),
        confirm=lambda prompt: True,
    )

    assert [r.url.path for r in seen] == [
        "/v1/skills/preview",
        "/v1/skills",
        "/v1/skills/demo-skill/versions/1.0.0",
    ]
    assert all(
        r.headers.get("authorization") == "Bearer tkn-secret-value" for r in seen
    )

    preview_body = json.loads(seen[0].content.decode())
    publish_body = json.loads(seen[1].content.decode())
    assert preview_body == publish_body

    output = "\n".join(console.lines)
    assert "tkn-secret-value" not in output
    assert "demo-skill" in output
    assert "1.0.0" in output
    assert "deadbeef" in output
