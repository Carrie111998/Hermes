import asyncio
import threading
from types import SimpleNamespace

from hermes_cli.model_switch import ModelSwitchResult


def _bound(fn, instance):
    return fn.__get__(instance, type(instance))


def test_prompt_toolkit_model_picker_defers_confirmation_off_key_handler(monkeypatch):
    import cli as cli_mod

    result = ModelSwitchResult(
        success=True,
        new_model="openai/gpt-5.5-pro",
        target_provider="nous",
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kwargs: result,
    )

    captured = {}

    class _Thread:
        def __init__(self, *, target, args, daemon):
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(cli_mod.threading, "Thread", _Thread)

    self_ = SimpleNamespace(
        _app=object(),
        _model_picker_state={
            "stage": "model",
            "provider_data": {"slug": "nous"},
            "model_list": ["openai/gpt-5.5-pro"],
            "selected": 0,
            "user_provs": None,
            "custom_provs": None,
        },
        provider="nous",
        model="openai/gpt-5.5",
        base_url="",
        api_key="",
        _restore_modal_input_snapshot=lambda: None,
        _invalidate=lambda **_kwargs: None,
    )
    self_._close_model_picker = _bound(cli_mod.HermesCLI._close_model_picker, self_)
    self_._confirm_and_apply_model_switch_result = (
        lambda *_args: captured.setdefault("ran_inline", True)
    )

    # The key handler now resolves persistence via resolve_persist_behavior,
    # which defaults to True (persist-by-default). Simulate that call.
    _bound(cli_mod.HermesCLI._handle_model_picker_selection, self_)(persist_global=True)

    assert self_._model_picker_state is None
    assert captured["started"] is True
    assert captured["daemon"] is True
    # Third arg is the fresh picker custom_providers snapshot (None here).
    assert captured["args"] == (result, True, None)
    assert "ran_inline" not in captured


def test_empty_custom_endpoint_row_defers_setup_off_key_handler(monkeypatch):
    """Selecting a bare custom row with no models starts endpoint setup.

    The provider picker cannot descend into an empty model list: that leaves
    only Back/Cancel and makes the highlighted custom row appear inert.  The
    setup flow is interactive, so it must run off the prompt_toolkit key
    handler just like expensive-model confirmation.
    """
    import cli as cli_mod

    captured = {}

    class _Thread:
        def __init__(self, *, target, args, daemon):
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(cli_mod.threading, "Thread", _Thread)

    provider = {
        "slug": "custom",
        "name": "Custom endpoint",
        "models": [],
        "is_user_defined": True,
        "source": "model-config",
    }
    self_ = SimpleNamespace(
        _app=object(),
        _model_picker_state={
            "stage": "provider",
            "providers": [provider],
            "selected": 0,
            "custom_provs": [],
        },
        _restore_modal_input_snapshot=lambda: None,
        _invalidate=lambda **_kwargs: None,
    )
    self_._close_model_picker = _bound(cli_mod.HermesCLI._close_model_picker, self_)
    self_._configure_custom_endpoint_from_picker = lambda *_args: None

    _bound(cli_mod.HermesCLI._handle_model_picker_selection, self_)()

    assert self_._model_picker_state is None
    assert captured["started"] is True
    assert captured["daemon"] is True
    assert captured["target"] is self_._configure_custom_endpoint_from_picker
    assert captured["args"] == (provider, [])


def test_custom_endpoint_picker_setup_applies_saved_route(monkeypatch):
    import cli as cli_mod

    configured = {
        "model": {
            "provider": "custom",
            "default": "local-model",
            "base_url": "http://truenas.local:11434/v1",
            "api_key": "local-key",
        },
        "providers": {},
    }
    calls = {}

    monkeypatch.setattr(
        "hermes_cli.main._model_flow_custom",
        lambda config: calls.setdefault("setup_config", config),
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: configured)
    monkeypatch.setattr(
        "hermes_cli.config.get_compatible_custom_providers",
        lambda _config: [{"name": "TrueNAS local"}],
    )
    result = ModelSwitchResult(
        success=True,
        new_model="local-model",
        target_provider="custom",
        base_url="http://truenas.local:11434/v1",
    )

    def _switch_model(**kwargs):
        calls["switch"] = kwargs
        return result

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", _switch_model)

    self_ = SimpleNamespace(
        _app=None,
        provider="openrouter",
        model="old-model",
        _confirm_and_apply_model_switch_result=lambda *args, **kwargs: calls.setdefault(
            "apply", (args, kwargs)
        ),
        _invalidate=lambda **_kwargs: None,
    )

    _bound(cli_mod.HermesCLI._configure_custom_endpoint_from_picker, self_)(
        {"slug": "custom"}, [{"name": "Stale endpoint"}]
    )

    assert calls["setup_config"] is configured
    assert calls["switch"]["explicit_provider"] == "custom"
    assert calls["switch"]["raw_input"] == "local-model"
    assert calls["switch"]["current_base_url"] == "http://truenas.local:11434/v1"
    assert calls["switch"]["current_api_key"] == "local-key"
    assert calls["apply"][0] == (result, True)
    assert calls["apply"][1]["custom_providers"] == [{"name": "TrueNAS local"}]


def test_custom_endpoint_picker_setup_handles_keyboard_interrupt(monkeypatch):
    import cli as cli_mod

    output = []
    invalidations = []
    monkeypatch.setattr(
        "hermes_cli.main._model_flow_custom",
        lambda _config: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {}})
    monkeypatch.setattr(cli_mod, "_cprint", output.append)

    self_ = SimpleNamespace(
        _app=None,
        _invalidate=lambda **kwargs: invalidations.append(kwargs),
    )

    _bound(cli_mod.HermesCLI._configure_custom_endpoint_from_picker, self_)(
        {"slug": "custom"}, []
    )

    assert output == ["  Custom endpoint setup cancelled."]
    assert invalidations == [{"min_interval": 0.0}]


def test_custom_endpoint_picker_setup_handles_interrupt_while_reloading(monkeypatch):
    import cli as cli_mod

    output = []
    invalidations = []
    load_count = 0

    def _load_config():
        nonlocal load_count
        load_count += 1
        if load_count == 2:
            raise KeyboardInterrupt
        return {"model": {}}

    monkeypatch.setattr("hermes_cli.main._model_flow_custom", lambda _config: None)
    monkeypatch.setattr("hermes_cli.config.load_config", _load_config)
    monkeypatch.setattr(cli_mod, "_cprint", output.append)

    self_ = SimpleNamespace(
        _app=None,
        _invalidate=lambda **kwargs: invalidations.append(kwargs),
    )

    _bound(cli_mod.HermesCLI._configure_custom_endpoint_from_picker, self_)(
        {"slug": "custom"}, []
    )

    assert output == ["  Custom endpoint setup cancelled."]
    assert invalidations == [{"min_interval": 0.0}]


def test_custom_endpoint_picker_setup_reports_missing_route(monkeypatch):
    import cli as cli_mod

    output = []
    invalidations = []
    monkeypatch.setattr("hermes_cli.main._model_flow_custom", lambda _config: None)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {}})
    monkeypatch.setattr(cli_mod, "_cprint", output.append)

    self_ = SimpleNamespace(
        _app=None,
        _invalidate=lambda **kwargs: invalidations.append(kwargs),
    )

    _bound(cli_mod.HermesCLI._configure_custom_endpoint_from_picker, self_)(
        {"slug": "custom"}, []
    )

    assert output == ["  No custom endpoint configured."]
    assert invalidations == [{"min_interval": 0.0}]


def test_custom_endpoint_picker_setup_leaves_prompt_toolkit_loop(monkeypatch):
    """The blocking setup flow must not run on prompt_toolkit's event loop."""
    import cli as cli_mod

    configured = {
        "model": {
            "provider": "custom",
            "default": "local-model",
            "base_url": "http://truenas.local:11434/v1",
        }
    }
    loop = asyncio.new_event_loop()
    loop_ready = threading.Event()
    calls = {}

    def _run_loop():
        asyncio.set_event_loop(loop)
        calls["loop_thread"] = threading.get_ident()
        loop_ready.set()
        loop.run_forever()

    loop_thread = threading.Thread(target=_run_loop)
    loop_thread.start()
    loop_ready.wait(timeout=2)

    async def _run_in_terminal(func, *, in_executor=False):
        calls["in_executor"] = in_executor
        if in_executor:
            return await asyncio.get_running_loop().run_in_executor(None, func)
        return func()

    def _setup(_config):
        calls["setup_thread"] = threading.get_ident()

    monkeypatch.setattr("prompt_toolkit.application.run_in_terminal", _run_in_terminal)
    monkeypatch.setattr("hermes_cli.main._model_flow_custom", _setup)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: configured)
    monkeypatch.setattr(
        "hermes_cli.config.get_compatible_custom_providers", lambda _config: []
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kwargs: ModelSwitchResult(
            success=False, error_message="stop after setup"
        ),
    )

    self_ = SimpleNamespace(
        _app=SimpleNamespace(loop=loop),
        provider="openrouter",
        model="old-model",
        _confirm_and_apply_model_switch_result=lambda *_args, **_kwargs: None,
        _invalidate=lambda **_kwargs: None,
    )

    try:
        _bound(cli_mod.HermesCLI._configure_custom_endpoint_from_picker, self_)(
            {"slug": "custom"}, []
        )
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()

    assert calls["in_executor"] is True
    assert calls["setup_thread"] != calls["loop_thread"]
