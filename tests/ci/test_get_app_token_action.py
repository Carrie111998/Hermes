"""Regression guards for the GitHub App token composite action."""

from pathlib import Path


ACTION = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "actions"
    / "get-app-token"
    / "action.yml"
)


def test_app_token_requires_both_credentials_before_minting():
    action = ACTION.read_text(encoding="utf-8")

    assert "CLIENT_ID: ${{ inputs.client-id }}" in action
    assert "PRIVATE_KEY: ${{ inputs.private-key }}" in action
    assert (
        'if [ -n "$CLIENT_ID" ] && [ -n "$PRIVATE_KEY" ]; then'
        in action
    )
