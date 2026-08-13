"""End-to-end contracts for isolated agent research and presentation.

These scenarios cross the public tool handlers and dispatcher spawn boundary.
They intentionally fail if routine research reaches a GUI browser launch path.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path


class _ResearchProvider:
    name = "contract-provider"

    def supports_search(self):
        return True

    def supports_extract(self):
        return True

    def search(self, query, limit):
        return {
            "success": True,
            "data": {
                "web": [
                    {
                        "title": "Isolation contract",
                        "url": "https://example.test/isolation",
                        "description": query,
                        "position": 1,
                    }
                ]
            },
        }

    async def extract(self, urls, format=None):
        return [
            {
                "url": urls[0],
                "title": "Isolation contract",
                "content": "Research stayed on the HTTP provider path.",
                "error": None,
            }
        ]


def _forbid_gui_launch(*_args, **_kwargs):
    raise AssertionError("routine research attempted to launch a browser process")


def test_search_page_retrieval_and_multistep_research_never_launch_gui(monkeypatch):
    """Search -> select -> retrieve stays on API/HTTP providers end to end."""
    from agent import web_search_registry
    from tools import desktop_ui, web_tools

    provider = _ResearchProvider()
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_search_backend", lambda: provider.name)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: provider.name)
    monkeypatch.setattr(web_search_registry, "get_provider", lambda _name: provider)
    monkeypatch.setattr(web_search_registry, "get_active_search_provider", lambda: provider)
    monkeypatch.setattr(web_tools, "async_is_safe_url", lambda _url: asyncio.sleep(0, result=True))
    preview_events = []
    desktop_ui.set_emitter(
        lambda _sid, event, payload: preview_events.append((event, payload))
    )
    monkeypatch.setattr(subprocess, "Popen", _forbid_gui_launch)
    monkeypatch.setattr(subprocess, "run", _forbid_gui_launch)

    try:
        search = json.loads(web_tools.web_search_tool("headless research contract", limit=3))
        selected_url = search["data"]["web"][0]["url"]
        extracted = json.loads(asyncio.run(web_tools.web_extract_tool([selected_url])))
    finally:
        desktop_ui.set_emitter(None)

    assert selected_url == "https://example.test/isolation"
    assert extracted["results"][0]["content"] == "Research stayed on the HTTP provider path."
    assert preview_events == []


def test_default_and_ct106_remote_routing_force_headless_and_ignore_cdp(monkeypatch):
    """Local defaults and the CT106-style SSH backend share the same policy."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_BROWSER_INTERACTION", raising=False)
    monkeypatch.setenv("AGENT_BROWSER_HEADED", "true")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")

    from tools import browser_tool

    browser_tool._headed_mode_resolved = False
    browser_tool._cached_headed_mode = None
    assert browser_tool._is_headed_mode() is False
    assert browser_tool._get_cdp_override() == ""

    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "ct106")
    browser_tool._headed_mode_resolved = False
    browser_tool._cached_headed_mode = None
    assert browser_tool._is_headed_mode() is False
    assert browser_tool._get_cdp_override() == ""

    monkeypatch.setenv("HERMES_BROWSER_INTERACTION", "visible")
    browser_tool._headed_mode_resolved = False
    browser_tool._cached_headed_mode = None
    assert browser_tool._is_headed_mode() is True
    assert browser_tool._get_cdp_override() == "http://127.0.0.1:9222"

def test_card_worker_spawn_scrubs_visible_browser_overrides(monkeypatch, tmp_path):
    """A card worker is isolated even when its dispatcher is foreground-enabled."""
    from hermes_cli import kanban_db as kb

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "researcher"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("toolsets:\n  - web\n  - browser\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    task = kb.Task(
        id="t_research",
        title="research",
        body=None,
        assignee="researcher",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=str(workspace),
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_BROWSER_INTERACTION", "visible")
    monkeypatch.setenv("AGENT_BROWSER_HEADED", "true")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class _Proc:
        pid = 4242

    def _capture(_cmd, *args, **kwargs):
        captured["env"] = kwargs["env"]
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _capture)
    assert kb._default_spawn(task, str(workspace)) == 4242
    assert captured["env"]["HERMES_BROWSER_INTERACTION"] == "isolated"
    assert "AGENT_BROWSER_HEADED" not in captured["env"]
    assert "BROWSER_CDP_URL" not in captured["env"]


def test_explicit_artifact_preview_remains_available_while_research_is_isolated(monkeypatch):
    """Presentation is an explicit desktop event, not browser research automation."""
    from tools import desktop_ui
    from tools.open_preview_tool import open_preview_tool

    monkeypatch.setenv("HERMES_BROWSER_INTERACTION", "isolated")
    events = []
    desktop_ui.set_emitter(lambda _sid, event, payload: events.append((event, payload)))
    try:
        result = json.loads(open_preview_tool("/tmp/research-report.html", "Research report"))
    finally:
        desktop_ui.set_emitter(None)

    assert result["success"] is True
    assert events == [
        ("preview.open", {"url": "/tmp/research-report.html", "label": "Research report"})
    ]


def test_browser_docs_explain_research_isolation_presentation_and_migration():
    docs = Path("website/docs/user-guide/features/browser.md").read_text(encoding="utf-8")

    for required in (
        "Research is isolated by default",
        "Artifact presentation is separate",
        "Opt in to a visible browser",
        "Migration notes",
        "Kanban workers",
        "CT106",
    ):
        assert required in docs

    operator_docs = Path("website/docs/developer-guide/browser-supervisor.md").read_text(
        encoding="utf-8"
    )
    for required in (
        "Agent research isolation",
        "HERMES_BROWSER_INTERACTION",
        "HERMES_KANBAN_TASK",
        "open_preview",
    ):
        assert required in operator_docs
