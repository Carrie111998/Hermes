"""编码子智能体路由的单元测试。"""

from pathlib import Path

from agent.coding_router import resolve_coding_route


def _config(enabled: bool = True) -> dict:
    return {
        "agent": {
            "coding_context": "auto",
            "coding_route": {
                "enabled": enabled,
                "provider": "volcengine-coding-plan",
                "model": "ark-code-latest",
            },
        }
    }


def test_coding_route_only_applies_in_code_workspace(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\n")
    assert resolve_coding_route(
        platform="cli", cwd=str(tmp_path), model="gpt-5.6-terra", config=_config()
    ) == {"provider": "volcengine-coding-plan", "model": "ark-code-latest"}


def test_coding_route_does_not_apply_to_messaging(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\n")
    assert resolve_coding_route(
        platform="weixin", cwd=str(tmp_path), model="gpt-5.6-terra", config=_config()
    ) is None


def test_coding_route_is_opt_in(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\n")
    assert resolve_coding_route(
        platform="cli", cwd=str(tmp_path), model="gpt-5.6-terra", config=_config(False)
    ) is None
