"""Unit tests for the plain-release-notes translation pass in scripts/release.py.

The script is loaded via importlib (scripts/ is not a package) and only its
pure helpers plus the urllib-based translation call are exercised. Network is
never touched: urlopen is monkeypatched with an in-memory fake.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "release.py"


def _load_release_module():
    spec = importlib.util.spec_from_file_location("hermes_release_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def release():
    return _load_release_module()


def _commit(subject, category):
    return {"subject": subject, "category": category}


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _completion(content: str) -> bytes:
    payload = {"choices": [{"message": {"content": content}}]}
    return json.dumps(payload).encode("utf-8")


# ── _extract_json_object ────────────────────────────────────────────────


class TestExtractJsonObject:
    def test_plain_object(self, release):
        assert release._extract_json_object('{"a": 1}') == {"a": 1}

    def test_fenced_object(self, release):
        raw = '```json\n{"items": []}\n```'
        assert release._extract_json_object(raw) == {"items": []}

    def test_prose_wrapped_object(self, release):
        raw = 'Sure! Here you go:\n\n{"items": [{"index": 0}]}\n\nHope that helps.'
        assert release._extract_json_object(raw) == {"items": [{"index": 0}]}

    def test_garbage_returns_none(self, release):
        assert release._extract_json_object("no json here") is None
        assert release._extract_json_object("") is None
        assert release._extract_json_object("[1, 2, 3]") is None


# ── _plain_note_candidates ──────────────────────────────────────────────


class TestPlainNoteCandidates:
    def test_orders_by_priority(self, release):
        commits = [
            _commit("misc thing", "other"),
            _commit("fix thing", "fixes"),
            _commit("new feature", "features"),
            _commit("breaking change", "breaking"),
            _commit("docs tweak", "docs"),
            _commit("speed up", "improvements"),
        ]
        ordered = release._plain_note_candidates(commits)
        assert [c["category"] for c in ordered] == [
            "breaking", "features", "fixes", "improvements", "other",
        ]

    def test_caps_at_sixty_items(self, release):
        commits = [_commit(f"misc {i}", "other") for i in range(70)]
        assert len(release._plain_note_candidates(commits)) == 60


# ── build_plain_release_notes / build_highlights_markdown ───────────────


class TestPlainMarkdownBuilders:
    def test_build_plain_release_notes_groups_and_skips_empty(self, release):
        items = [
            {"index": 0, "group": "fixed", "text": "Messages are no longer lost"},
            {"index": 1, "group": "new", "text": "A new setup page"},
            {"index": 2, "group": "new", "text": "Faster first message"},
        ]
        markdown = release.build_plain_release_notes(items, "0.21.0", "2026.8.22")

        assert markdown.startswith("# Hermes v0.21.0 (2026.8.22)\n")
        assert "## What's new" in markdown
        assert markdown.index("## What's new") < markdown.index("## Fixed")
        assert "- A new setup page" in markdown
        assert "## Faster" not in markdown  # empty group omitted

    def test_build_highlights_markdown(self, release):
        items = [
            {"index": 0, "group": "fixed", "text": "Messages are no longer lost"},
        ]
        markdown = release.build_highlights_markdown(items)

        assert markdown.startswith("## ✨ Highlights\n")
        assert "### Fixed" in markdown
        assert "- Messages are no longer lost" in markdown


# ── translate_entries_to_plain_english ──────────────────────────────────


class TestTranslateEntries:
    def test_skips_without_api_key(self, release, monkeypatch):
        monkeypatch.delenv("RELEASE_NOTES_API_KEY", raising=False)
        result = release.translate_entries_to_plain_english([_commit("fix thing", "fixes")])
        assert result is None

    def test_translates_and_aligns_by_index(self, release, monkeypatch):
        monkeypatch.setenv("RELEASE_NOTES_API_KEY", "test-key")
        entries = [
            _commit("fix(ledger): claim ledger rows before abandonable boot-send task", "fixes"),
            _commit("feat(desktop): NSIS prereq detection page", "features"),
        ]
        reply = {
            "items": [
                {"index": 0, "group": "fixed", "text": "Queued messages no longer get lost or sent twice"},
                {"index": 1, "group": "new", "text": "Setup now checks for missing prerequisites"},
            ]
        }

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["url"] = request.full_url
            return _FakeResponse(_completion(json.dumps(reply)))

        monkeypatch.setattr(release.urllib.request, "urlopen", fake_urlopen)

        result = release.translate_entries_to_plain_english(entries)

        assert result == [
            {"index": 0, "group": "fixed", "text": "Queued messages no longer get lost or sent twice"},
            {"index": 1, "group": "new", "text": "Setup now checks for missing prerequisites"},
        ]
        # The prompt must carry cleaned subjects, not the conventional prefix.
        prompt = captured["body"]["messages"][1]["content"]
        assert "claim ledger rows" not in prompt  # cleaned subject form
        assert "Claim ledger rows" in prompt
        # Defaults point at the Nous inference gateway and its GLM model.
        assert captured["url"].startswith("https://inference-api.nousresearch.com/v1/chat/completions")
        assert captured["body"]["model"] == "z-ai/glm-5.3"

    def test_returns_none_on_item_count_mismatch(self, release, monkeypatch):
        monkeypatch.setenv("RELEASE_NOTES_API_KEY", "test-key")
        entries = [_commit("fix thing", "fixes"), _commit("feat thing", "features")]
        reply = {"items": [{"index": 0, "group": "fixed", "text": "Only one"}]}

        monkeypatch.setattr(
            release.urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse(_completion(json.dumps(reply)))
        )
        assert release.translate_entries_to_plain_english(entries) is None

    def test_returns_none_on_malformed_item(self, release, monkeypatch):
        monkeypatch.setenv("RELEASE_NOTES_API_KEY", "test-key")
        entries = [_commit("fix thing", "fixes")]
        reply = {"items": [{"index": 0, "group": "not-a-group", "text": "x"}]}

        monkeypatch.setattr(
            release.urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse(_completion(json.dumps(reply)))
        )
        assert release.translate_entries_to_plain_english(entries) is None

    def test_returns_none_when_network_raises(self, release, monkeypatch):
        monkeypatch.setenv("RELEASE_NOTES_API_KEY", "test-key")

        def boom(request, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(release.urllib.request, "urlopen", boom)
        assert release.translate_entries_to_plain_english([_commit("fix thing", "fixes")]) is None
