from agent.usage_pricing import resolve_billing_route


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
