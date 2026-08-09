import ast
from pathlib import Path


def test_phase_two_source_has_no_command_runner_or_target_control_primitives():
    root = Path("plugins/agentops")
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))

    for forbidden in ("subprocess", "os.system", "launchctl", "shell=True", "Popen("):
        assert forbidden not in source


def test_bridge_has_no_gateway_import_or_plugin_registration():
    tree = ast.parse(Path("plugins/agentops/bridge.py").read_text(encoding="utf-8"))
    imported_modules = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    register_calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr.startswith("register_")
    ]

    assert not any(module == "gateway" or module.startswith("gateway.") for module in imported_modules)
    assert register_calls == []
