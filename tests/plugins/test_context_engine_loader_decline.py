"""Loader must respect a register() that declines to provide an engine.

A context-engine plugin's ``register(ctx)`` may intentionally register
nothing (missing credentials, unmet dependency). The loader used to fall
through to the subclass scan and instantiate the engine class anyway,
bypassing the plugin's own availability gate — selecting a dead engine
instead of falling back to the built-in compressor.
"""

import textwrap

from plugins.context_engine import _load_engine_from_dir

ENGINE_BODY = """
    from agent.context_engine import ContextEngine

    class ProbeEngine(ContextEngine):
        @property
        def name(self):
            return "probe"

        def update_from_response(self, usage):
            pass

        def should_compress(self, prompt_tokens=None):
            return False

        def compress(self, *args, **kwargs):
            return None
"""


def _write_engine(tmp_path, dirname, extra):
    d = tmp_path / dirname
    d.mkdir()
    (d / "__init__.py").write_text(
        textwrap.dedent(ENGINE_BODY) + textwrap.dedent(extra)
    )
    return d


def test_declined_register_is_not_resurrected(tmp_path):
    engine_dir = _write_engine(
        tmp_path,
        "declining_engine",
        """
        def register(ctx):
            # Decline: e.g. required API key is missing.
            return
        """,
    )
    assert _load_engine_from_dir(engine_dir) is None


def test_registering_plugin_returns_engine(tmp_path):
    engine_dir = _write_engine(
        tmp_path,
        "registering_engine",
        """
        def register(ctx):
            ctx.register_context_engine(ProbeEngine())
        """,
    )
    engine = _load_engine_from_dir(engine_dir)
    assert engine is not None
    assert engine.name == "probe"


def test_module_without_register_still_uses_subclass_scan(tmp_path):
    engine_dir = _write_engine(tmp_path, "legacy_engine", "")
    engine = _load_engine_from_dir(engine_dir)
    assert engine is not None
    assert engine.name == "probe"


def test_register_that_raises_falls_through_to_subclass_scan(tmp_path):
    # A register() that blows up (not a deliberate decline) must not strand the
    # engine: the loader falls back to the subclass scan and still returns it.
    engine_dir = _write_engine(
        tmp_path,
        "raising_engine",
        """
        def register(ctx):
            raise RuntimeError("boom during register")
        """,
    )
    engine = _load_engine_from_dir(engine_dir)
    assert engine is not None
    assert engine.name == "probe"
