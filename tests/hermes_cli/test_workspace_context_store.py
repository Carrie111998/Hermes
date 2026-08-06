import stat

import pytest

from hermes_cli.workspace_context_store import WorkspaceContextStore


def test_workspace_context_store_is_profile_local_deduped_and_private(tmp_path):
    store = WorkspaceContextStore(tmp_path)
    saved = store.set(
        "project-1",
        notion_page_ids=["1234567890abcdef1234567890abcdef", "1234567890abcdef1234567890abcdef"],
        slack_channel_ids=["c123abc", "C123ABC"],
    )
    assert saved == {
        "notion_page_ids": ["1234567890abcdef1234567890abcdef"],
        "slack_channel_ids": ["C123ABC"],
    }
    assert store.get("project-1") == saved
    assert WorkspaceContextStore(tmp_path / "other").get("project-1") == {
        "notion_page_ids": [],
        "slack_channel_ids": [],
    }
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_workspace_context_store_rejects_unbounded_or_non_channel_bindings(tmp_path):
    store = WorkspaceContextStore(tmp_path)
    with pytest.raises(ValueError, match="channel IDs"):
        store.set("project-1", notion_page_ids=[], slack_channel_ids=["D123"])
    with pytest.raises(ValueError, match="page IDs"):
        store.set("project-1", notion_page_ids=["https://notion.so/page"], slack_channel_ids=[])
    with pytest.raises(ValueError, match="project ID"):
        store.get("../escape")
