"""Tests for gateway's legacy 3-bucket classifier shim.

The centralized router resolves richer profiles and the gateway compatibility
shim maps them to historic buckets:
- trivial  -> deterministic/fast/fast_plus
- moderate -> balanced
- high     -> creative/strong/maximum/ultra
"""

import pytest

from gateway.run import GatewayRunner


classify = GatewayRunner._classify_turn_complexity


# ---------- trivial bucket ----------

@pytest.mark.parametrize(
    "message",
    [
        "execute this",
        "send it",
        "do it",
        "go ahead",
        "ship it",
        "proceed",
    ],
)
def test_approved_execution_is_trivial(message):
    assert classify(message) == "trivial"


@pytest.mark.parametrize(
    "message",
    [
        "what time is it",
        "what's the time",
        "current time",
        "current time in Tokyo",
        "what is the current date",
    ],
)
def test_current_time_is_trivial(message):
    assert classify(message) == "trivial"


@pytest.mark.parametrize(
    "message",
    [
        "distance to Starbucks",
        "distance to the nearest Starbucks",
        "how far is the airport",
        "how far to downtown Seattle",
    ],
)
def test_distance_lookups_are_trivial(message):
    assert classify(message) == "trivial"


@pytest.mark.parametrize(
    "message",
    [
        "tallest building",
        "the tallest building",
        "what's the tallest building",
        "what is the largest ocean",
        "the fastest animal",
    ],
)
def test_basic_fact_superlatives_are_trivial(message):
    assert classify(message) == "trivial"


def test_empty_message_is_trivial():
    assert classify("") == "trivial"
    assert classify("   ") == "trivial"


# ---------- high (creative/strategy) bucket ----------

@pytest.mark.parametrize(
    "message",
    [
        "build a YouTube video packet for this topic",
        "create title generation and thumbnail copy options",
        "research YouTube outliers and recommend ideas",
        "do deeper research and synthesize findings",
        "build our July 4 launch strategy",
        "rethink our offer and pricing strategy",
        "design architecture for a new multi-service migration",
        "terra failed twice, solve this now",
    ],
)
def test_creative_and_strategy_routes_are_high(message):
    assert classify(message) == "high"


# ---------- moderate bucket ----------

@pytest.mark.parametrize(
    "message",
    [
        "debug the gateway",
        "write a Python script to parse CSV",
        "write a bash script to rotate logs",
        "write a regex to match SKUs",
        "investigate why the cron is failing",
        "refactor the auth mixin",
        "roll back the last migration",
        "build a new automation for this workflow",
        "modify the existing workflow and keep behavior stable",
        "make the requested GitHub changes from this issue",
        "fix an ordinary Cloudflare configuration issue",
        "draft an email sequence from an approved strategy",
        "research testimonials and incorporate them into existing copy",
    ],
)
def test_operational_and_engineering_is_moderate(message):
    assert classify(message) == "moderate"


def test_long_operational_prompt_stays_moderate():
    # Long multi-step implementation should remain moderate unless it carries
    # explicit strategy/creative escalation signals.
    message = (
        "walk through the gateway startup path, then inspect the readiness "
        "checks, then diff the current provider routing against last "
        "week's snapshot, and finally propose a rollback plan for the "
        "auth mixin change. This is production critical and touches "
        "customer sessions."
    )
    assert classify(message) == "moderate"


# ---------- key false positives ----------

def test_write_python_script_is_not_escalated():
    assert classify("write a Python script to parse CSV") == "moderate"


def test_approved_strategy_email_stays_moderate():
    assert classify("draft an email sequence from approved strategy") == "moderate"


def test_write_config_file_is_not_writing():
    # "write" + "config" is engineering/worker work, not strategic escalation.
    assert classify("write a config file for the new worker") == "moderate"