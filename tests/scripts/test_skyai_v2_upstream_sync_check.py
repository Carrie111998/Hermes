from __future__ import annotations

from scripts import skyai_v2_upstream_sync_check as sync_check


def test_disallowed_files_allows_only_skyai_v2_edge_layer() -> None:
    files = [
        "plugins/skyai_customer/public_tools.py",
        "skills/productivity/skyai-customer-hermes-v2/SKILL.md",
        "docs/skyai-v2-hermes-plugin-bootstrap.md",
        "tests/plugins/test_skyai_customer_plugin.py",
        "tests/scripts/test_skyai_v2_bootstrap_dev_profile.py",
        "scripts/skyai_v2_bootstrap_dev_profile.py",
        "scripts/skyai_v2_upstream_sync_check.py",
    ]

    assert sync_check.disallowed_files(files) == []


def test_disallowed_files_blocks_hermes_core_changes() -> None:
    files = [
        "plugins/skyai_customer/public_tools.py",
        "run_agent.py",
        "agent/conversation_loop.py",
    ]

    assert sync_check.disallowed_files(files) == [
        "run_agent.py",
        "agent/conversation_loop.py",
    ]
