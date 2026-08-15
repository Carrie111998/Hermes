"""Tests for plugins.platforms.discord.command_registry (feature I1)."""

import pytest

from plugins.platforms.discord.command_registry import (
    CommandDef,
    CommandRegistryError,
    command_fingerprint,
    duplicate_diagnostics,
    normalize_command,
    should_sync,
)


def make_cmd(name="ping", command_type=1, integration_types=None, guild_id=None):
    return CommandDef(
        name=name,
        command_type=command_type,
        integration_types=integration_types,
        guild_id=guild_id,
    )


# --- normalize_command -------------------------------------------------------


def test_normalize_strips_name():
    assert normalize_command(make_cmd(name="  ping  ")).name == "ping"


def test_normalize_defaults_integration_types_to_zero():
    assert normalize_command(make_cmd(integration_types=None)).integration_types == [0]
    assert normalize_command(make_cmd(integration_types=[])).integration_types == [0]


def test_normalize_dedupes_and_sorts_integration_types():
    norm = normalize_command(make_cmd(integration_types=[2, 0, 2, 1, 0]))
    assert norm.integration_types == [0, 1, 2]


def test_normalize_preserves_type_and_guild():
    norm = normalize_command(make_cmd(command_type=3, guild_id="123456"))
    assert norm.command_type == 3
    assert norm.guild_id == "123456"


# --- command_fingerprint -----------------------------------------------------


def test_fingerprint_stable_for_equal_commands():
    a = command_fingerprint(make_cmd(name="ping", integration_types=[1, 0], guild_id="g1"))
    b = command_fingerprint(make_cmd(name="ping", integration_types=[0, 1], guild_id="g1"))
    assert a == b


def test_fingerprint_is_case_insensitive_on_name():
    assert command_fingerprint(make_cmd(name="Ping")) == command_fingerprint(
        make_cmd(name="ping")
    )


def test_fingerprint_treats_none_guild_as_global():
    assert command_fingerprint(make_cmd(guild_id=None)) == command_fingerprint(
        make_cmd(guild_id="global")
    )


def test_fingerprint_differs_on_any_change():
    base = make_cmd()
    assert command_fingerprint(make_cmd(name="pong")) != command_fingerprint(base)
    assert command_fingerprint(make_cmd(command_type=2)) != command_fingerprint(base)
    assert command_fingerprint(make_cmd(integration_types=[1])) != command_fingerprint(base)
    assert command_fingerprint(make_cmd(guild_id="123")) != command_fingerprint(base)


# --- duplicate_diagnostics ---------------------------------------------------


def test_duplicate_diagnostics_empty_when_unique():
    cmds = [
        make_cmd(name="ping"),
        make_cmd(name="pong"),
        make_cmd(name="slash", command_type=2),
    ]
    assert duplicate_diagnostics(cmds) == {}


def test_duplicate_diagnostics_finds_duplicates_by_normalized_type_name():
    cmds = [
        make_cmd(name="ping"),
        make_cmd(name="  PING  "),
        make_cmd(name="ping", guild_id="123"),  # different guild -> not a duplicate
    ]
    diag = duplicate_diagnostics(cmds)
    assert len(diag) == 1
    (fp, names) = next(iter(diag.items()))
    assert set(names) == {"ping", "PING"}
    assert fp == command_fingerprint(make_cmd(name="ping"))


def test_duplicate_diagnostics_distinguishes_by_type():
    cmds = [make_cmd(name="ping"), make_cmd(name="ping", command_type=2)]
    assert duplicate_diagnostics(cmds) == {}


# --- should_sync -------------------------------------------------------------


def test_should_sync_true_when_never_deployed():
    assert should_sync(make_cmd(), None) is True


def test_should_sync_false_when_unchanged():
    fp = command_fingerprint(make_cmd())
    assert should_sync(make_cmd(), fp) is False


def test_should_sync_true_when_changed():
    deployed = command_fingerprint(make_cmd(name="ping"))
    assert should_sync(make_cmd(name="pong"), deployed) is True
    assert should_sync(make_cmd(command_type=2), deployed) is True
    assert should_sync(make_cmd(integration_types=[1]), deployed) is True


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize("bad_name", ["", "   ", "\t\n "])
def test_invalid_name_raises_in_should_sync(bad_name):
    with pytest.raises(CommandRegistryError):
        should_sync(make_cmd(name=bad_name), None)


@pytest.mark.parametrize("bad_type", ["1", None, 1.5, True])
def test_invalid_command_type_raises_in_should_sync(bad_type):
    with pytest.raises(CommandRegistryError):
        should_sync(make_cmd(command_type=bad_type), None)


def test_command_registry_error_is_value_error():
    assert issubclass(CommandRegistryError, ValueError)


def test_invalid_input_raises_in_normalize_too():
    with pytest.raises(ValueError):
        normalize_command(make_cmd(name="  "))
    with pytest.raises(CommandRegistryError):
        normalize_command(make_cmd(command_type="1"))
