"""Test that alibaba-coding-plan and alibaba-coding-plan-cn don't both appear
in the /model picker off a single shared key.

Regression (#101122): both profiles listened to the same
ALIBABA_CODING_PLAN_API_KEY / DASHSCOPE_API_KEY env vars, so setting either
made *both* providers show up in the picker even though the user only has one
endpoint's key. Fixed the same way as kimi-coding / kimi-coding-cn (#10526):
give the China entry its own, non-overlapping env var
(ALIBABA_CODING_PLAN_CN_API_KEY) in both the plugin's ProviderProfile and the
hermes_cli.auth.PROVIDER_REGISTRY entry that takes priority over it.
"""

import os
from unittest.mock import patch

from hermes_cli.model_switch import list_authenticated_providers
from hermes_cli.providers import resolve_provider_full


# -- Only the CN key set ------------------------------------------------------


@patch.dict(os.environ, {"ALIBABA_CODING_PLAN_CN_API_KEY": "sk-cn-fake"}, clear=False)
def test_alibaba_cn_appears_when_only_cn_key_set():
    """alibaba-coding-plan-cn should appear when only its own key is set."""
    providers = list_authenticated_providers(current_provider="alibaba-coding-plan-cn")

    cn = next((p for p in providers if p["slug"] == "alibaba-coding-plan-cn"), None)
    assert cn is not None, (
        "alibaba-coding-plan-cn should appear when ALIBABA_CODING_PLAN_CN_API_KEY is set"
    )
    assert cn["is_current"] is True

    intl = next((p for p in providers if p["slug"] == "alibaba-coding-plan"), None)
    assert intl is None, (
        "alibaba-coding-plan should NOT appear when only the CN key is set"
    )


# -- Only the intl key set (the original bug) --------------------------------


@patch.dict(os.environ, {"ALIBABA_CODING_PLAN_API_KEY": "sk-intl-fake"}, clear=False)
def test_alibaba_cn_does_not_appear_when_only_intl_key_set():
    """#101122: alibaba-coding-plan-cn must NOT appear off the intl-only key."""
    providers = list_authenticated_providers(current_provider="alibaba-coding-plan")

    intl = next((p for p in providers if p["slug"] == "alibaba-coding-plan"), None)
    assert intl is not None, (
        "alibaba-coding-plan should appear when ALIBABA_CODING_PLAN_API_KEY is set"
    )
    assert intl["is_current"] is True

    cn = next((p for p in providers if p["slug"] == "alibaba-coding-plan-cn"), None)
    assert cn is None, (
        "alibaba-coding-plan-cn should NOT appear when only the shared intl key "
        "(ALIBABA_CODING_PLAN_API_KEY) is set -- this was the duplicate-entry bug"
    )


@patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-dashscope-fake"}, clear=False)
def test_alibaba_cn_does_not_appear_when_only_shared_dashscope_key_set():
    """The other half of the shared pair (#101122): DASHSCOPE_API_KEY alone
    must not surface the CN entry either."""
    providers = list_authenticated_providers(current_provider="alibaba-coding-plan")

    intl = next((p for p in providers if p["slug"] == "alibaba-coding-plan"), None)
    assert intl is not None

    cn = next((p for p in providers if p["slug"] == "alibaba-coding-plan-cn"), None)
    assert cn is None, (
        "alibaba-coding-plan-cn should NOT appear when only DASHSCOPE_API_KEY is set"
    )


# -- Provider identity preserved ----------------------------------------------


@patch.dict(os.environ, {
    "ALIBABA_CODING_PLAN_API_KEY": "sk-intl-fake",
    "ALIBABA_CODING_PLAN_CN_API_KEY": "sk-cn-fake",
}, clear=False)
def test_resolve_provider_full_preserves_alibaba_cn_provider_identity():
    """Explicit alibaba-coding-plan-cn must resolve to its own endpoint/key,
    not fall back to the intl one."""
    pdef = resolve_provider_full("alibaba-coding-plan-cn", None, None)
    assert pdef is not None
    assert pdef.id == "alibaba-coding-plan-cn"
    assert pdef.base_url == "https://coding.dashscope.aliyuncs.com/v1"
    assert pdef.api_key_env_vars == ("ALIBABA_CODING_PLAN_CN_API_KEY", "ALIBABA_CODING_PLAN_CN_BASE_URL")
