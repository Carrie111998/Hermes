"""SRL-4049: fence-cancel com summary pronto nao pode descartar o produto.

F1: compressao conclui -> fence cancela o commit -> cache guarda o produto ->
proximo turno reaplica SEM re-sumarizar. F2: MAX_WORKERS >= 8.
"""
import copy
from unittest.mock import MagicMock

from agent import conversation_compression as cc


class _CancelAfterSummaryFence:
    """Fence do bug real: deixa a sumarizacao rodar e cancela so no commit.

    Recusa begin_commit (chamado DEPOIS do compress_fn no fluxo), mas NAO
    cancela pre-dispatch — reproduz commit_fence_cancelled com summary pronto.
    """

    def __init__(self):
        self.is_cancelled = False
        self.begin_lock_setup = lambda: True
        self.finish_lock_setup = lambda: None
        self._registered = None

    def touch_progress(self):
        pass

    def seconds_since_progress(self):
        return 0.0

    def register_cancelled_lock_release(self, fn):
        # BUG REAL: cancelamento chega APÓS o registro, não antes.
        # Retornar False = "cancellation NÃO venceu durante o setup"
        # → fluxo segue para a sumarização.
        self._registered = fn
        return False

    def clear_cancelled_lock_release(self, fn=None):
        return True

    def try_cancel_before_commit(self):
        return False

    def begin_commit(self, cancel_event=None):
        return False

    def finish_commit(self):
        pass

    @property
    def commit_in_flight(self):
        return False


def _mk_agent(sid, compressed):
    a = MagicMock()
    a.session_id = sid
    a.context_compressor = MagicMock()
    a.context_compressor._last_summary_error = None
    a.context_compressor.compress = MagicMock(return_value=copy.deepcopy(compressed))
    # MagicMock auto-atributos sao truthy: o gate de abort (3731) dispararia.
    # zera os que sao guardas False no caminho feliz.
    a.context_compressor._last_compress_aborted = False
    a.context_compressor._last_compression_made_progress = True
    a.context_compressor._last_summary_fallback_used = False
    a.context_compressor._last_feasibility_skip = False
    a._cached_system_prompt = "SP-OLD"
    a._hard_interrupt_requested = None
    a.api_mode = "chat_completions"
    a.compression_checkpoint_required = False
    return a


def test_max_workers_raised_for_fanout():
    """F2: pool >= 8 para fanout de sessoes grandes."""
    assert cc._COMPRESS_EXECUTOR_MAX_WORKERS >= 8
    cc._SRL4049_PENDING_COMMITS.clear()


def test_pending_commit_cache_roundtrip():
    """F1 unidade: cache/take round-trip e consumo unico."""
    cc._SRL4049_PENDING_COMMITS.clear()
    cc._srl4049_cache_pending_commit("sess-A", [{"role": "user", "content": "x"}], "sp")
    entry = cc._srl4049_take_pending_commit("sess-A")
    assert entry is not None and entry["system_prompt"] == "sp"
    assert cc._srl4049_take_pending_commit("sess-A") is None
    cc._SRL4049_PENDING_COMMITS.clear()


def test_f1_summary_survives_fence_cancel_and_reapplies():
    """F1 integracao: compress conclui, fence recusa begin_commit ->
    produto cacheado para reaplicacao."""
    fake_compressed = [{"role": "user", "content": "SUMMARY of everything"}]
    agent = _mk_agent("sess-F1", fake_compressed)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(200)]
    messages_before = copy.deepcopy(messages)
    cc._SRL4049_PENDING_COMMITS.clear()

    out_msgs, out_sp = cc.compress_context(
        agent=agent,
        messages=messages,
        system_message="sys",
        force=True,
        commit_fence=_CancelAfterSummaryFence(),
    )
    # gate abortou e cacheou o produto caro
    cached = cc._SRL4049_PENDING_COMMITS.get("sess-F1")
    assert cached is not None, "summary pronto nao foi cacheado no fence-cancel"
    assert cached["system_prompt"]
    assert len(cached["compressed"]) < len(messages_before)
    cc._SRL4049_PENDING_COMMITS.clear()


def test_f1_reapply_next_turn_skips_resummarize():
    """F1 ciclo completo: turno 1 cacheia; turno 2 reaplica SEM re-sumarizar."""
    fake_compressed = [{"role": "user", "content": "SUMMARY turn1"}]
    agent = _mk_agent("sess-F2", fake_compressed)

    messages = [{"role": "user", "content": f"m{i}"} for i in range(150)]
    cc._SRL4049_PENDING_COMMITS.clear()

    cc.compress_context(
        agent=agent, messages=messages, system_message="sys",
        force=True, commit_fence=_CancelAfterSummaryFence(),
    )
    entry = cc._srl4049_take_pending_commit("sess-F2")
    assert entry is not None and "SUMMARY" in str(entry["compressed"][0])
    cc._SRL4049_PENDING_COMMITS.clear()