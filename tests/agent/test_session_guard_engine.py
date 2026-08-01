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
