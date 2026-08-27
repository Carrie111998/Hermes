"""自动编码 Worker 路由测试。"""

from types import SimpleNamespace

from agent.automatic_coding_worker import maybe_run_automatic_coding_worker
from agent.coding_router import is_explicit_coding_task


def test_explicit_coding_task_classifier_is_narrow():
    assert is_explicit_coding_task("修复这个 Python bug 并运行测试")
    assert is_explicit_coding_task("Refactor the parser and add test")
    assert not is_explicit_coding_task("总结 README 的内容")
    assert not is_explicit_coding_task("查一下今天北京天气")


def test_auto_worker_skips_non_coding_task(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"agent": {"coding_worker": {"enabled": True, "auto_route": True}}},
    )
    agent = SimpleNamespace(platform="cli", model="test")
    assert maybe_run_automatic_coding_worker(agent, "总结 README") is None


def test_auto_worker_returns_real_worker_result(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\n")
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "agent": {
                "coding_context": "auto",
                "coding_worker": {
                    "enabled": True,
                    "auto_route": True,
                    "worker": "codex",
                    "review_worker": "claude",
                    "timeout_seconds": 30,
                },
            }
        },
    )
    monkeypatch.setattr("tools.delegate_tool._resolve_workspace_hint", lambda _: str(tmp_path))
    monkeypatch.setattr(
        "tools.coding_worker_tool.coding_worker",
        lambda **_: '{"worker":"codex","implementation":{"status":"ok","exit_code":0,"output":"done"},"review_worker":"claude","review":{"status":"ok","exit_code":0,"output":"APPROVED"}}',
    )
    agent = SimpleNamespace(platform="cli", model="test")
    result = maybe_run_automatic_coding_worker(agent, "修复测试失败")
    assert result["completed"] is True
    assert "APPROVED" in result["final_response"]
