"""Regression test for #92732.

`_ensure_runtime_credentials()`'s auth-recovery path resolves a bare
`provider: custom` fallback entry's credentials once, but only persisted
`requested_provider`/`model` onto `self` — not the resolved `api_key`/
`base_url`. `_init_agent()` calls `_ensure_runtime_credentials()` again
moments later in the same turn, which re-resolves `requested_provider`
("custom") from scratch with no explicit key/base_url, and a bare custom
provider has nothing to resolve against — so the second call fails with
"No API key found for provider 'custom'." even though the first call had
already confirmed the fallback provider was reachable.
"""

import sys
import types
from contextlib import nullcontext

import pytest

from hermes_cli.auth import AuthError


def _reset_modules(prefixes):
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _restore_cli_and_tool_modules():
    prefixes = ("tools", "cli", "run_agent")
    original_modules = {
        name: module
        for name, module in sys.modules.items()
        if any(name == p or name.startswith(p + ".") for p in prefixes)
    }
    try:
        yield
    finally:
        _reset_modules(prefixes)
        sys.modules.update(original_modules)


def _install_prompt_toolkit_stubs():
    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    class _Condition:
        def __init__(self, func):
            self.func = func

        def __bool__(self):
            return bool(self.func())

    class _ANSI(str):
        pass

    root = types.ModuleType("prompt_toolkit")
    history = types.ModuleType("prompt_toolkit.history")
    styles = types.ModuleType("prompt_toolkit.styles")
    patch_stdout = types.ModuleType("prompt_toolkit.patch_stdout")
    application = types.ModuleType("prompt_toolkit.application")
    layout = types.ModuleType("prompt_toolkit.layout")
    processors = types.ModuleType("prompt_toolkit.layout.processors")
    filters = types.ModuleType("prompt_toolkit.filters")
    dimension = types.ModuleType("prompt_toolkit.layout.dimension")
    menus = types.ModuleType("prompt_toolkit.layout.menus")
    widgets = types.ModuleType("prompt_toolkit.widgets")
    key_binding = types.ModuleType("prompt_toolkit.key_binding")
    completion = types.ModuleType("prompt_toolkit.completion")
    formatted_text = types.ModuleType("prompt_toolkit.formatted_text")

    history.FileHistory = _Dummy
    styles.Style = _Dummy
    patch_stdout.patch_stdout = lambda *args, **kwargs: nullcontext()
    application.Application = _Dummy
    layout.Layout = _Dummy
    layout.HSplit = _Dummy
    layout.Window = _Dummy
    layout.FormattedTextControl = _Dummy
    layout.ConditionalContainer = _Dummy
    processors.Processor = _Dummy
    processors.Transformation = _Dummy
    processors.PasswordProcessor = _Dummy
    processors.ConditionalProcessor = _Dummy
    filters.Condition = _Condition
    dimension.Dimension = _Dummy
    menus.CompletionsMenu = _Dummy
    widgets.TextArea = _Dummy
    key_binding.KeyBindings = _Dummy
    completion.Completer = _Dummy
    completion.Completion = _Dummy
    formatted_text.ANSI = _ANSI
    root.print_formatted_text = lambda *args, **kwargs: None

    sys.modules.setdefault("prompt_toolkit", root)
    sys.modules.setdefault("prompt_toolkit.history", history)
    sys.modules.setdefault("prompt_toolkit.styles", styles)
    sys.modules.setdefault("prompt_toolkit.patch_stdout", patch_stdout)
    sys.modules.setdefault("prompt_toolkit.application", application)
    sys.modules.setdefault("prompt_toolkit.layout", layout)
    sys.modules.setdefault("prompt_toolkit.layout.processors", processors)
    sys.modules.setdefault("prompt_toolkit.filters", filters)
    sys.modules.setdefault("prompt_toolkit.layout.dimension", dimension)
    sys.modules.setdefault("prompt_toolkit.layout.menus", menus)
    sys.modules.setdefault("prompt_toolkit.widgets", widgets)
    sys.modules.setdefault("prompt_toolkit.key_binding", key_binding)
    sys.modules.setdefault("prompt_toolkit.completion", completion)
    sys.modules.setdefault("prompt_toolkit.formatted_text", formatted_text)


def _import_cli():
    for name in list(sys.modules):
        if name == "cli" or name == "run_agent" or name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)

    if "firecrawl" not in sys.modules:
        sys.modules["firecrawl"] = types.SimpleNamespace(Firecrawl=object)

    import importlib

    try:
        importlib.import_module("prompt_toolkit")
    except ModuleNotFoundError:
        _install_prompt_toolkit_stubs()
    return importlib.import_module("cli")


def test_fallback_custom_provider_credentials_survive_second_resolve(monkeypatch):
    cli = _import_cli()

    fallback_base_url = "http://192.168.99.155:11434/v1"
    fallback_api_key = "fallback-key"
    calls = []

    def _runtime_resolve(*, requested=None, explicit_api_key=None, explicit_base_url=None, **kwargs):
        calls.append((requested, explicit_api_key, explicit_base_url))
        if requested == "bogus-provider":
            raise AuthError("bogus-provider auth failed")
        if requested == "custom":
            if explicit_api_key == fallback_api_key and explicit_base_url == fallback_base_url:
                return {
                    "provider": "custom",
                    "api_mode": "chat_completions",
                    "base_url": explicit_base_url,
                    "api_key": explicit_api_key,
                    "source": "fallback_providers",
                }
            # Mirrors real runtime_provider.py: a bare `provider: custom`
            # with no explicit base_url/api_key has nothing to trust, so
            # it falls through to the OpenRouter default with no key.
            return {
                "provider": "custom",
                "api_mode": "chat_completions",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": None,
                "source": "default",
            }
        raise AssertionError(f"unexpected requested provider: {requested!r}")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))
    monkeypatch.setattr("hermes_cli.fallback_config.resolve_entry_api_key", lambda fb: fallback_api_key)

    shell = cli.HermesCLI(model="claude-sonnet-5", compact=True, max_turns=1)
    shell.requested_provider = "bogus-provider"
    shell._fallback_model = [
        {
            "provider": "custom",
            "model": "qwen3:32b",
            "base_url": fallback_base_url,
            "api_key": fallback_api_key,
        }
    ]

    # First call: primary auth fails, fallback engages and resolves fine.
    assert shell._ensure_runtime_credentials() is True
    assert shell.api_key == fallback_api_key
    assert shell.base_url == fallback_base_url

    # Second call: simulates _init_agent()'s own call to this method later
    # in the same turn. It must re-derive the same fallback runtime instead
    # of losing the credentials and failing.
    assert shell._ensure_runtime_credentials() is True
    assert shell.api_key == fallback_api_key
    assert shell.base_url == fallback_base_url
