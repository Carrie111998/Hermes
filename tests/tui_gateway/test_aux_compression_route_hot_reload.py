"""Switching the auxiliary compression model must reach the live compressor.

The compaction trigger is clamped to the auxiliary compression model's
context window (``check_compression_model_feasibility``), so
``auxiliary.compression.model`` is a threshold input. It was absent from the
live-config signature, which is keyed on ``compression.*`` /
``model.context_length`` only — so pointing compression at a different model
mid-session changed nothing until the session was restarted:

  * away from a small model: the trigger stayed clamped to the OLD small
    window, compacting far more often than configured, forever;
  * toward a small model: the trigger stayed above the new window, handing
    the summariser a region it cannot ingest — the failure the clamp exists
    to prevent.

Only the ROUTE keys belong in the signature. ``timeout`` /
``reasoning_effort`` / ``extra_body`` are read per call by auxiliary_client
and must not churn it.
"""

from types import SimpleNamespace

from agent.context_compressor import ContextCompressor
from tui_gateway import server

MAIN_CTX = 272_000


def _cfg(aux_compression: dict | None = None, **compression):
    cfg = {
        "model": {
            "default": "gpt-5.6-sol",
            "provider": "openai-codex",
            "context_length": MAIN_CTX,
        },
        "compression": {"threshold": 0.75, **compression},
    }
    if aux_compression is not None:
        cfg["auxiliary"] = {"compression": aux_compression}
    return cfg


def _session():
    compressor = ContextCompressor(
        model="gpt-5.6-sol",
        threshold_percent=0.75,
        config_context_length=MAIN_CTX,
        quiet_mode=True,
    )
    agent = SimpleNamespace(
        model="gpt-5.6-sol",
        provider="openai-codex",
        context_compressor=compressor,
        compression_enabled=True,
        compression_idle_compact_after_seconds=0,
        codex_responses_native_compaction=False,
        codex_responses_compact_threshold=200_000,
    )
    return {"agent": agent, "session_key": "sid-aux-route"}, compressor


class TestSignatureTracksTheAuxRoute:
    def test_changing_the_aux_model_changes_the_signature(self):
        before = server._tui_compression_config_signature(
            _cfg({"provider": "openai-codex", "model": "small-aux"})
        )
        after = server._tui_compression_config_signature(
            _cfg({"provider": "openai-codex", "model": "big-aux"})
        )
        assert before != after, (
            "the aux model decides the clamped trigger; a switch must be adopted"
        )

    def test_changing_the_aux_provider_or_base_url_changes_the_signature(self):
        base = _cfg({"provider": "openai-codex", "model": "aux"})
        assert server._tui_compression_config_signature(base) != (
            server._tui_compression_config_signature(
                _cfg({"provider": "openrouter", "model": "aux"})
            )
        )
        assert server._tui_compression_config_signature(base) != (
            server._tui_compression_config_signature(
                _cfg({"provider": "openai-codex", "model": "aux",
                      "base_url": "https://example.invalid/v1"})
            )
        )

    def test_per_call_aux_knobs_do_not_churn_the_signature(self):
        """timeout/reasoning_effort are read per call — they must not re-derive."""
        before = server._tui_compression_config_signature(
            _cfg({"provider": "openai-codex", "model": "aux", "timeout": 300})
        )
        after = server._tui_compression_config_signature(
            _cfg({
                "provider": "openai-codex",
                "model": "aux",
                "timeout": 900,
                "reasoning_effort": "low",
            })
        )
        assert before == after

    def test_absent_auxiliary_section_is_stable(self):
        assert server._tui_compression_config_signature(_cfg()) == (
            server._tui_compression_config_signature(_cfg())
        )


class TestLiveAdoptionReClampsToTheCurrentAuxWindow:
    def _adopt(self, monkeypatch, session, cfg, aux_ctx, aux_model="aux"):
        monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
        monkeypatch.setattr(
            "agent.auxiliary_client.get_text_auxiliary_client",
            lambda *a, **k: (object(), aux_model),
        )
        monkeypatch.setattr(
            "agent.auxiliary_client._resolve_task_provider_model",
            lambda *a, **k: ("openai-codex", aux_model, "", "", ""),
        )
        monkeypatch.setattr(
            "agent.model_metadata.get_model_context_length",
            lambda *a, **k: aux_ctx,
        )
        agent = session["agent"]
        for attr, value in (
            ("_compression_warning", None),
            ("_custom_providers", {}),
            ("status_callback", None),
            ("_current_main_runtime", lambda: {}),
            ("_emit_status", lambda _m: None),
        ):
            setattr(agent, attr, value)
        server._sync_agent_compression_with_config("sid-aux-route", session)

    def test_switching_to_a_small_aux_model_clamps_the_live_trigger(
        self, monkeypatch
    ):
        session, compressor = _session()
        configured = compressor.threshold_tokens
        self._adopt(
            monkeypatch,
            session,
            _cfg({"provider": "openai-codex", "model": "small-aux"}),
            aux_ctx=128_000,
        )
        assert compressor.threshold_tokens == 128_000 < configured

    def test_switching_back_to_a_large_aux_model_restores_the_trigger(
        self, monkeypatch
    ):
        session, compressor = _session()
        configured = compressor.threshold_tokens
        self._adopt(
            monkeypatch,
            session,
            _cfg({"provider": "openai-codex", "model": "small-aux"}),
            aux_ctx=128_000,
        )
        assert compressor.threshold_tokens == 128_000

        self._adopt(
            monkeypatch,
            session,
            _cfg({"provider": "openai-codex", "model": "big-aux"}),
            aux_ctx=MAIN_CTX,
        )
        assert compressor.threshold_tokens == configured

    def test_adoption_never_raises_the_trigger_past_the_aux_window(
        self, monkeypatch
    ):
        """Raising compression.threshold must not outrun a small aux model."""
        session, compressor = _session()
        self._adopt(
            monkeypatch,
            session,
            _cfg({"provider": "openai-codex", "model": "small-aux"}, threshold=0.9),
            aux_ctx=128_000,
        )
        assert compressor.threshold_tokens <= 128_000
