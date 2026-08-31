"""Regression smoke test: /refine must remain a dispatchable CLI method.

An indentation slip that re-indents ``_handle_refine_command`` under
``_handle_heartbeat_command`` turns it into a nested function that Python
accepts silently while the class loses the method entirely — every
``/refine`` invocation (with or without ``--report``) then fails dispatch.
This test catches that class of bug at import time.
"""

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class TestRefineCommandRegistration:
    def test_handle_refine_command_is_a_class_method(self):
        assert hasattr(CLICommandsMixin, "_handle_refine_command"), (
            "_handle_refine_command is not a CLICommandsMixin method — "
            "check for accidental re-indentation inside another handler"
        )

    def test_handle_refine_command_is_callable(self):
        fn = getattr(CLICommandsMixin, "_handle_refine_command", None)
        assert callable(fn), (
            "_handle_refine_command exists but is not callable — "
            "it may be a nested function instead of a method"
        )
