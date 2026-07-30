import subprocess
import sys
from pathlib import Path

from agent.usage_pricing import resolve_billing_route


REPO_ROOT = Path(__file__).parents[2]


def test_claude_cli_billing_route_is_subscription_process():
    route = resolve_billing_route(
        "opus",
        provider="claude-cli",
        base_url="claude-cli://local",
    )

    assert route.provider == "claude-cli"
    assert route.model == "opus"
    assert route.base_url == "claude-cli://local"
    assert route.billing_mode == "subscription_process"


def test_live_verifier_is_directly_executable():
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "verify_claude_cli_provider.py"),
            "--help",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--model" in result.stdout
