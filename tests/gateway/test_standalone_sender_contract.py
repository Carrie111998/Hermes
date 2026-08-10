"""Contract invariants for plugin standalone delivery senders."""

from __future__ import annotations

import ast
from pathlib import Path


class TestRegisteredStandaloneSenderContract:
    def test_every_registered_plugin_sender_accepts_optional_contact_callback(self):
        """Every concrete registration must support the delivery contact boundary.

        Registration sites are discovered rather than copied into a platform list so
        newly registered senders automatically inherit this contract.
        """
        plugins_root = Path(__file__).parents[2] / "plugins"
        registrations = []

        for path in plugins_root.glob("**/*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.keyword) or node.arg != "standalone_sender_fn":
                    continue
                assert isinstance(node.value, ast.Name), (
                    f"{path}: standalone_sender_fn must name an inspectable function"
                )
                registrations.append((path, tree, node.value.id))

        assert registrations, "no plugin standalone sender registrations discovered"
        for path, tree, function_name in registrations:
            sender = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function_name
            )
            defaults = dict(zip(sender.args.kwonlyargs, sender.args.kw_defaults))
            parameter = next(
                (arg for arg in sender.args.kwonlyargs if arg.arg == "on_provider_contact"),
                None,
            )
            assert parameter is not None, f"{path}:{function_name} lacks on_provider_contact"
            default = defaults[parameter]
            assert isinstance(default, ast.Constant) and default.value is None, (
                f"{path}:{function_name}.on_provider_contact must default to None"
            )
