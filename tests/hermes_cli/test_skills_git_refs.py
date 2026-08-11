"""CLI and slash parsing coverage for immutable GitHub skill selectors."""

import sys
from unittest.mock import patch


def test_cli_install_forwards_ref(monkeypatch):
    from hermes_cli.main import main

    captured = {}
    monkeypatch.setattr("hermes_cli.skills_hub.skills_command", lambda args: captured.update(ref=args.ref, pr=args.pr))
    monkeypatch.setattr(sys, "argv", ["hermes", "skills", "install", "owner/repo/demo", "--ref", "release/v1"])
    main()
    assert captured == {"ref": "release/v1", "pr": ""}


def test_cli_install_forwards_pr(monkeypatch):
    from hermes_cli.main import main

    captured = {}
    monkeypatch.setattr("hermes_cli.skills_hub.skills_command", lambda args: captured.update(ref=args.ref, pr=args.pr))
    monkeypatch.setattr(sys, "argv", ["hermes", "skills", "install", "owner/repo/demo", "--pr", "42"])
    main()
    assert captured == {"ref": "", "pr": "42"}


def test_slash_install_forwards_ref_and_rejects_conflicting_selector():
    from hermes_cli.skills_hub import handle_skills_slash

    with patch("hermes_cli.skills_hub.do_install") as install:
        handle_skills_slash("/skills install owner/repo/demo --ref release/v1")
        assert install.call_args.kwargs["ref"] == "release/v1"
        assert install.call_args.kwargs["pr"] == ""

    with patch("hermes_cli.skills_hub.do_install") as install:
        handle_skills_slash("/skills install owner/repo/demo --ref main --pr 42")
        install.assert_not_called()
