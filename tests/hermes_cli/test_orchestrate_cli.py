from hermes_cli.subcommands.orchestrate import build_orchestrate_parser


def _handler(_):
    return None


def test_orchestrate_start_parser():
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_orchestrate_parser(subparsers, cmd_orchestrate=_handler)
    args = parser.parse_args(["orchestrate", "start", "CYCLE_ONE"])
    assert args.command == "orchestrate"
    assert args.orchestrate_action == "start"
    assert args.cycle_id == "CYCLE_ONE"
    assert args.func is _handler


def test_orchestrate_status_requires_cycle():
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_orchestrate_parser(subparsers, cmd_orchestrate=_handler)
    args = parser.parse_args(
        ["orchestrate", "status", "dispatch-one", "--cycle", "CYCLE_ONE"]
    )
    assert args.dispatch_id == "dispatch-one"
    assert args.cycle_id == "CYCLE_ONE"


def test_orchestrate_prepare_collects_repeated_scope_fields():
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_orchestrate_parser(subparsers, cmd_orchestrate=_handler)
    args = parser.parse_args(
        [
            "orchestrate", "prepare", "--repo", "/srv/project",
            "--repository-id", "my-project", "--cycle", "FEATURE_EXAMPLE_001",
            "--contract", "FEATURE-EXAMPLE-001", "--goal", "Build it",
            "--accept", "Tests pass", "--allow", "src/example.py",
            "--branch", "feat/example", "--worktree", "/srv/worktrees/example",
        ]
    )
    assert args.acceptance == ["Tests pass"]
    assert args.allowed_paths == ["src/example.py"]
