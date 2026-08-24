import argparse

from hermes_cli.subcommands.wisdom import build_wisdom_parser


def parser():
    value = argparse.ArgumentParser()
    build_wisdom_parser(value.add_subparsers(dest="command"))
    return value


def test_all_foundation_commands_are_registered():
    value = parser()
    commands = {
        "setup": [],
        "status": [],
        "scan": [],
        "suggest": [],
        "candidates": [],
        "review": ["draft"],
        "approve": ["draft"],
        "decline": ["draft"],
        "list": [],
        "show": ["skill"],
        "install": ["skill"],
        "versions": ["skill"],
    }
    for command, trailing in commands.items():
        args = value.parse_args(["wisdom", command, *trailing])
        assert args.wisdom_command == command


def test_install_plan_apply_arguments_are_stable():
    value = parser()
    plan = value.parse_args(["wisdom", "install", "skill-1@v2", "--plan", "--json"])
    assert plan.reference == "skill-1@v2"
    assert plan.plan is True
    apply = value.parse_args([
        "wisdom",
        "install",
        "--apply-receipt",
        "wip_123",
        "--accept-partial",
    ])
    assert apply.apply_receipt == "wip_123"
