"""验证 SessionGuardEngine v2.3 方法委托完整性。

测试目标：确认 6 个 ContextCompressor 覆盖方法被正确代理，
而非使用 ContextEngine 基类的空/no-op 默认实现。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.context_engine import ContextEngine
from plugins.context_engine.session_guard.engine import SessionGuardEngine


# ── 辅助：创建带 mock _real 的引擎 ────────────────────────────

def _engine_with_mock_real():
    """创建一个已注入 mock _real 的 SessionGuardEngine。"""
    e = SessionGuardEngine()
    mock = MagicMock()
    mock.name = "compressor"
    mock.compression_count = 0
    mock.last_prompt_tokens = 0
    mock.last_completion_tokens = 0
    mock.last_total_tokens = 0
    mock.threshold_tokens = 0
    mock.context_length = 32768
    mock.should_compress.return_value = False
    mock.should_compress_info.return_value = (False, "cooldown:30")
    mock.should_defer_preflight_to_real_usage.return_value = True
    mock.prune_tool_results_only.return_value = ([{"role": "user", "content": "x"}], 3)
    mock.has_content_to_compress.return_value = False

    # compress() 模拟真正压缩：返回压缩后结果 + 递增计数
    def _do_compress(*args, **kwargs):
        mock.compression_count += 1
        return [{"role": "system", "content": "compressed"}]
    mock.compress = _do_compress

    mock.on_session_start.return_value = None
    mock.on_session_end.return_value = None
    e._real = mock
    return e, mock


# ── 方法委托测试 ─────────────────────────────────────────────

def test_should_compress_info_delegated():
    """v2.3: should_compress_info 代理到 _real，返回 block reason。"""
    e, mock = _engine_with_mock_real()
    result = e.should_compress_info(50000)
    mock.should_compress_info.assert_called_once_with(50000)
    assert result == (False, "cooldown:30"), f"Expected cooldown reason, got {result}"


def test_should_defer_preflight_to_real_usage_delegated():
    """v2.3: should_defer_preflight_to_real_usage 代理到 _real。"""
    e, mock = _engine_with_mock_real()
    result = e.should_defer_preflight_to_real_usage(30000)
    mock.should_defer_preflight_to_real_usage.assert_called_once_with(30000)
    assert result is True


def test_prune_tool_results_only_delegated():
    """v2.3: prune_tool_results_only 代理到 _real，Pass 2 剪枝生效。"""
    e, mock = _engine_with_mock_real()
    msgs = [{"role": "tool", "content": "big result" * 100}]
    result, n = e.prune_tool_results_only(msgs, 5000)
    mock.prune_tool_results_only.assert_called_once_with(msgs, 5000)
    assert n == 3, f"Expected 3 pruned, got {n}"


def test_has_content_to_compress_delegated():
    """v2.3: has_content_to_compress 代理到 _real。"""
    e, mock = _engine_with_mock_real()
    result = e.has_content_to_compress([{"role": "user", "content": "hello"}])
    mock.has_content_to_compress.assert_called_once()
    assert result is False


def test_on_session_start_delegated():
    """v2.3: on_session_start 代理到 _real，加载持久状态。"""
    e, mock = _engine_with_mock_real()
    e.on_session_start("test-session-123", hermes_home="/test")
    mock.on_session_start.assert_called_once_with("test-session-123", hermes_home="/test")


def test_on_session_end_delegated():
    """v2.3: on_session_end 代理到 _real，清理会话状态。"""
    e, mock = _engine_with_mock_real()
    msgs = [{"role": "user", "content": "bye"}]
    e.on_session_end("test-session-123", msgs)
    mock.on_session_end.assert_called_once_with("test-session-123", msgs)


# ── 核心功能测试 ─────────────────────────────────────────────

def test_compress_without_force_increments_count_and_injects():
    """自动压缩（非 force）递增计数，达到阈值后注入提醒。"""
    e, mock = _engine_with_mock_real()
    e._guard_compress_count = 2  # 第 3 次将触发

    result = e.compress(
        [{"role": "user", "content": "hi"}],
        current_tokens=100000,
        force=False,
    )

    assert e._guard_compress_count == 3
    assert len(result) == 2  # compressed + reminder
    assert "会话守卫强制提醒" in result[-1]["content"]


def test_compress_with_force_does_not_count():
    """手动 /compress（force=True）不递增守卫计数。"""
    e, mock = _engine_with_mock_real()
    e._guard_compress_count = 0

    result = e.compress(
        [{"role": "user", "content": "hi"}],
        current_tokens=100000,
        force=True,
    )

    assert e._guard_compress_count == 0
    assert len(result) == 1  # no reminder injected


def test_on_session_reset_clears_guard_state():
    """/new 或 /reset 后守卫状态归零。"""
    e, mock = _engine_with_mock_real()
    e._guard_compress_count = 5

    e.on_session_reset()

    assert e._guard_compress_count == 0
    mock.on_session_reset.assert_called_once()


def test_force_reminder_at_3():
    """第 3 次压缩后强制提醒（三段策略触发点）。"""
    e, mock = _engine_with_mock_real()
    e._guard_compress_count = 2  # 第 3 次

    result = e.compress(
        [{"role": "user", "content": "hi"}],
        force=False,
    )

    assert e._guard_compress_count == 3
    assert len(result) == 2
    assert "会话守卫强制提醒" in result[-1]["content"]


def test_compress_noop_does_not_count():
    """ContextCompressor no-op 返回时（消息太少/无可压缩窗口），守卫不计数。"""
    e, mock = _engine_with_mock_real()
    # 重设 mock.compress 为 no-op（不递增 compression_count）
    mock.compression_count = 5
    mock.compress = MagicMock(return_value=[{"role": "user", "content": "same"}])

    e._guard_compress_count = 1
    result = e.compress(
        [{"role": "user", "content": "short"}],
        force=False,
    )

    # compression_count 没变 → 守卫不计数
    assert e._guard_compress_count == 1
    # 消息未变
    assert len(result) == 1


def test_compress_user_ended_safely_merges():
    """compressed 结果以 user 结尾时，提醒合并到消息末而非新建 user（防交替违规）。"""
    e, mock = _engine_with_mock_real()
    e._guard_compress_count = 2  # 第 3 次触发
    # compress 返回以 user 结尾的结果
    mock.compress = MagicMock()
    mock.compress.side_effect = None
    mock.compress.return_value = None
    # 需要每次调用递增计数
    mock.compression_count = 0

    def _do_compress(*args, **kwargs):
        mock.compression_count += 1
        return [{"role": "system", "content": "summary"}, {"role": "user", "content": "original user"}]

    mock.compress = _do_compress

    result = e.compress(
        [{"role": "user", "content": "long conversation"}],
        force=False,
    )

    assert e._guard_compress_count == 3  # 2 → 3（本次确实压缩了）
    # 应该只有 2 条消息（system + user），提醒合并到最后一条 user
    assert len(result) == 2
    assert result[-1]["role"] == "user"
    assert "会话守卫强制提醒" in result[-1]["content"]
    assert "original user" in result[-1]["content"]


def test_compress_non_user_ended_appends():
    """compressed 结果以 system 结尾时，安全追加 user 提醒消息。"""
    e, mock = _engine_with_mock_real()
    e._guard_compress_count = 2
    # compress 以 system 消息结尾
    mock.compress.side_effect = None

    result = e.compress(
        [{"role": "user", "content": "long conversation"}],
        force=False,
    )

    assert e._guard_compress_count == 3
    # 原结果 (system) + 新追加 (user)
    assert len(result) == 2
    assert result[-1]["role"] == "user"
    assert "会话守卫强制提醒" in result[-1]["content"]


# ── __getattr__ 兜底测试 ─────────────────────────────────────

def test_getattr_delegates_unknown_attrs():
    """未显式声明的方法/属性通过 __getattr__ 兜底代理。"""
    e, mock = _engine_with_mock_real()
    mock._context_probed = True

    result = e._context_probed
    assert result is True


def test_getattr_raises_for_guard_internals():
    """守卫内部属性（_guard_* / _real）不被 __getattr__ 代理。"""
    e, _ = _engine_with_mock_real()
    try:
        _ = e._guard_compress_count  # 应该走正常属性，不走 __getattr__
    except AttributeError:
        assert False, "_guard_compress_count 应该直接可访问"

    # 未初始化的引擎访问 _real 不会走 __getattr__
    e2 = SessionGuardEngine()
    assert e2._real is None  # 直接属性访问


def test_name():
    e = SessionGuardEngine()
    assert e.name == "session_guard"


def test_subclass_of_context_engine():
    assert issubclass(SessionGuardEngine, ContextEngine)


# ── 确认方法来自 SessionGuardEngine 而非 ContextEngine ────────

DELEGATED_METHODS = [
    "should_compress_info",
    "should_defer_preflight_to_real_usage",
    "prune_tool_results_only",
    "has_content_to_compress",
    "on_session_start",
    "on_session_end",
]


def test_all_six_methods_defined_on_session_guard():
    """v2.3: 全部 6 个方法必须在 SessionGuardEngine 上显式定义。"""
    for method_name in DELEGATED_METHODS:
        ses_method = getattr(SessionGuardEngine, method_name, None)
        ce_method = getattr(ContextEngine, method_name, None)
        assert ses_method is not ce_method, (
            f"{method_name} 未在 SessionGuardEngine 上显式定义，"
            f"将使用 ContextEngine 默认实现（绕过 _real 代理）"
        )
        assert ses_method is not None, f"{method_name} 在 SessionGuardEngine 上找不到"


# ═══════════════════════════════════════════════════════════════
# 集成测试（真实 ContextCompressor + SessionGuardEngine）
# ═══════════════════════════════════════════════════════════════

def _make_real_compressor():
    """创建一个状态完备的真实 ContextCompressor（绕过 __init__，避免 HTTP 探测）。

    参考 tests/agent/test_context_compressor_cross_session_guard.py 中的
    _make_compressor() 构造模式。所有属性手动设置，使 compress() 全流程
    可在无本地 LLM、无网络的环境下运行。
    """
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor.__new__(ContextCompressor)
    # ── 基础配置 ──
    c.quiet_mode = True
    c.model = "test/model"
    c.provider = "test"
    c.base_url = "http://test"
    c.api_key = "test-key"
    c.api_mode = ""
    c.context_length = 128000
    c.threshold_tokens = 64000
    c.threshold_percent = 0.50
    c.tail_token_budget = 500       # 小 tail 预算，确保有中间窗口可压缩
    c.protect_last_n = 12
    c.protect_first_n = 3
    c.summary_model = ""
    c.summary_target_ratio = 0.20
    c.summary_budget_tokens = 2000
    c.max_tokens = None
    c.min_tail_user_messages = 1
    # ── token 追踪 ──
    c.last_prompt_tokens = 100000
    c.last_completion_tokens = 0
    c.last_real_prompt_tokens = 0
    c.last_compression_rough_tokens = 0
    c.last_rough_tokens_when_real_prompt_fit = 0
    c.awaiting_real_usage_after_compression = False
    # ── 压缩状态 ──
    c.compression_count = 0
    c._context_probed = False
    c._last_compression_savings_pct = 100.0
    c._ineffective_compression_count = 0
    c._last_compression_made_progress = False
    # ── 摘要相关 ──
    c._previous_summary = None
    c._summary_has_user_turn = None
    c._summary_failure_cooldown_until = 0.0
    c._max_compaction_summary_tokens = 2000
    c.abort_on_summary_failure = False
    c._last_compress_aborted = False
    c._summary_model_fallen_back = False
    c._last_summary_error = None
    c._last_summary_dropped_count = 0
    c._last_summary_fallback_used = False
    c._last_aux_model_failure_error = None
    c._last_aux_model_failure_model = None
    c._last_summary_auth_failure = False
    c._last_summary_network_failure = False
    # ── 内部状态（避免 lazy resolution 行为）──
    c._config_threshold_percent = 0.50
    c._base_threshold_percent = 0.50
    c._configured_threshold_percent = 0.50
    c._resolved_context_length = 128000
    c._threshold_tokens = 64000
    c._tail_token_budget = 500
    c._max_summary_tokens = 2000
    c._log_init_summary = False
    c._anti_thrash_recovery_deadline = 0.0
    c._fallback_compression_streak = 0
    c._verify_compaction_cleared_threshold = False
    c._cooldown_persist_failed = False
    # ── 可选功能（禁用）──
    c.proactive_prune_tokens = 0
    c.proactive_prune_min_result_chars = 8000
    c.proactive_prune_min_reclaim_tokens = 0
    c.threshold_tokens_cap = None
    c.model_thresholds = {}
    c._session_db = None
    c._session_id = ""
    return c


def test_real_noop_short_messages():
    """集成测试：短消息列表不会触发压缩，_guard_compress_count 保持 0。

    构造 < _min_for_compress 的消息，调用真实 ContextCompressor 的
    compress()，验证其 no-op 返回行为 + 守卫不计数。
    """
    real = _make_real_compressor()
    engine = SessionGuardEngine()
    engine._real = real
    engine._guard_compress_count = 0

    short_msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
        {"role": "user", "content": "How are you?"},
        {"role": "assistant", "content": "I'm good, thanks!"},
    ]

    result = engine.compress(short_msgs, force=False)

    # 守卫计数器不变（ContextCompressor 因消息太少 no-op）
    assert engine._guard_compress_count == 0, (
        f"_guard_compress_count 应为 0（未实际压缩），实际 {engine._guard_compress_count}"
    )
    # 消息原样返回
    assert result == short_msgs, (
        f"短消息列表应原样返回，但结果不同。\n输入: {len(short_msgs)} 条\n输出: {len(result)} 条"
    )


def test_real_compress_counts_and_safe_injection():
    """集成测试：真实压缩后计数器递增 + 第 3 次压缩注入提醒。

    构造 ~80 条 user/assistant 交换（足够触发压缩），mock
    _generate_summary 返回测试摘要，预设 _guard_compress_count=2，
    验证压缩后计数为 3 且结果中包含会话守卫提醒。
    """
    real = _make_real_compressor()

    # 构造长消息列表：system + 80 对 user/assistant 交换 + padding
    long_msgs = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(80):
        long_msgs.append({"role": "user", "content": f"Question {i}: what is 2+2?"})
        long_msgs.append(
            {"role": "assistant",
             "content": f"Answer {i}: the answer is 4. " + "extra padding to increase token count. " * 60}
        )

    engine = SessionGuardEngine()
    engine._real = real
    engine._guard_compress_count = 2  # 第 3 次将触发提醒

    with patch.object(real, "_generate_summary",
                      return_value="[CONTEXT COMPACTION] test summary for integration test"):
        result = engine.compress(long_msgs, force=False)

    # 守卫计数递增（从 2 到 3）
    assert engine._guard_compress_count == 3, (
        f"_guard_compress_count 应为 3，实际 {engine._guard_compress_count}"
    )
    # 内部 ContextCompressor 的 compression_count 也递增
    assert real.compression_count == 1, (
        f"真实 compressor 的 compression_count 应为 1，实际 {real.compression_count}"
    )
    # 结果中注入了提醒
    reminder_found = any(
        "会话守卫强制提醒" in (m.get("content", "") or "")
        for m in result
    )
    assert reminder_found, (
        "压缩结果中应包含「会话守卫强制提醒」，但未找到。\n"
        f"结果消息数: {len(result)}\n"
        f"结果 role 序列: {[m.get('role') for m in result]}"
    )
    # 角色交替安全：结果不以连续 user 结尾（提醒合并到末条 user 或新建）
    roles = [m.get("role") for m in result]
    for i in range(len(roles) - 1):
        assert roles[i] != roles[i + 1] or roles[i] != "user", (
            f"角色交替违规：连续 user 消息出现在索引 {i} 和 {i + 1}"
        )
