"""会话守卫引擎 v1.0 — 代理 ContextCompressor，在超过压缩上限后强制提醒。

架构：代理模式。所有压缩逻辑委托给内置 ContextCompressor，
仅在 compress() 返回后检查是否需要注入会话守卫提醒。

三段策略：
    1-2 次自动压缩 → 不提醒（正常监测）
    3+ 次自动压缩 → 强制提醒（每次压缩后注入提醒消息）

配置（通过实例属性）：
    guard_remind_after: int = 3   # 第 N 次压缩后开始强制提醒
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.context_engine import ContextEngine

# 延迟导入，避免在插件发现阶段触发重依赖
_CC = None


def _get_cc():
    global _CC
    if _CC is None:
        from agent.context_compressor import ContextCompressor
        _CC = ContextCompressor
    return _CC


class SessionGuardEngine(ContextEngine):
    """在默认 ContextCompressor 外围添加会话守卫提醒。"""

    # ── 守卫配置 ──────────────────────────────────────────────
    guard_remind_after: int = 3   # 第 N 次压缩后开始强制提醒

    def __init__(self):
        super().__init__()
        self._real = None                 # 内部 ContextCompressor（延迟创建）
        self._guard_compress_count = 0    # 本次会话自动压缩计数
        self._guard_model = ""
        self._guard_context_length = 0

    # ── ContextEngine 抽象方法 ────────────────────────────────

    @property
    def name(self) -> str:
        return "session_guard"

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        if self._real is not None:
            self._real.update_from_response(usage)

    def should_compress(self, prompt_tokens: int = None) -> bool:
        if self._real is not None:
            return self._real.should_compress(prompt_tokens)
        return False

    # ── v2.3: 显式委托所有 ContextCompressor 覆盖的方法 ──────
    # 这些方法在 ContextEngine 基类中有默认实现，__getattr__ 无法拦截，
    # 必须显式委托给 _real 以保留 ContextCompressor 的增强逻辑。

    def should_compress_info(self, prompt_tokens: int = None) -> "tuple[bool, str | None]":
        """委托给内部 ContextCompressor，保留冷却/反抖动等 block reason。"""
        if self._real is not None:
            return self._real.should_compress_info(prompt_tokens)
        return False, None

    def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:
        """委托给内部 ContextCompressor，避免粗糙估算导致的误触发预检。"""
        if self._real is not None:
            return self._real.should_defer_preflight_to_real_usage(rough_tokens)
        return False

    def prune_tool_results_only(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int | None = None,
    ) -> "tuple[List[Dict[str, Any]], int]":
        """委托给内部 ContextCompressor，让 Pass 2 工具结果剪枝正常工作。"""
        if self._real is not None:
            return self._real.prune_tool_results_only(messages, current_tokens)
        return messages, 0

    def has_content_to_compress(self, messages: List[Dict[str, Any]]) -> bool:
        """委托给内部 ContextCompressor，让 /compress 命令正确判断。"""
        if self._real is not None:
            return self._real.has_content_to_compress(messages)
        return True

    # ── 生命周期（含 v2.3 修复后的完整委托）──────────────────

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """委托给内部 ContextCompressor，加载持久状态（无效压缩计数等）。"""
        self._ensure_real()
        self._real.on_session_start(session_id, **kwargs)

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """委托给内部 ContextCompressor，清理会话状态防跨会话污染。"""
        if self._real is not None:
            self._real.on_session_end(session_id, messages)

    def on_session_reset(self) -> None:
        self._guard_compress_count = 0
        if self._real is not None:
            self._real.on_session_reset()

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: Any = "",
        provider: str = "",
        api_mode: str = "",
        max_tokens: int | None = None,
    ) -> None:
        self._guard_model = model
        self._guard_context_length = context_length
        self._ensure_real()
        self._real.update_model(
            model=model,
            context_length=context_length,
            base_url=base_url,
            api_key=api_key,
            provider=provider,
            api_mode=api_mode,
            max_tokens=max_tokens,
        )

    # ── 核心：压缩 + 提醒注入 ─────────────────────────────────

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
        force: bool = False,
        memory_context: str = "",
    ) -> List[Dict[str, Any]]:
        self._ensure_real()
        result = self._real.compress(
            messages,
            current_tokens=current_tokens,
            focus_topic=focus_topic,
            force=force,
            memory_context=memory_context,
        )

        # 只对自动压缩计数（手动 /compress 不计数）
        if not force:
            self._guard_compress_count += 1
            if self._should_inject_reminder():
                reminder = self._build_reminder()
                result = list(result) + [reminder]

        return result

    # ── 内部方法 ──────────────────────────────────────────────

    def _ensure_real(self):
        if self._real is not None:
            return
        CC = _get_cc()
        self._real = CC(
            model=self._guard_model or "default",
            threshold_percent=0.50,
        )

    def _should_inject_reminder(self) -> bool:
        n = self._guard_compress_count

        # 三段策略：
        #   第 1-2 次压缩 → 不提醒（蓝色→黄色期，正常监测）
        #   第 3 次压缩   → 强制提醒（红色期已达上限）
        #   第 4+ 次压缩  → 每次都强制提醒
        if n >= self.guard_remind_after:
            return True

        return False

    def _build_reminder(self) -> Dict[str, Any]:
        n = self._guard_compress_count
        content = (
            f"🔴 [会话守卫强制提醒 - 第 {n} 次压缩] "
            "当前会话已超过建议的压缩上限，早期对话细节大量丢失。"
            "你必须严格执行以下操作：\n"
            "1) 先正常完成用户当前问题的回复；\n"
            "2) 在回复末尾用 Markdown 引用块(>)和醒目格式，"
            "提醒用户：⚠️ 会话已达压缩上限，建议立即执行总结并新建会话；\n"
            "3) 主动引导用户输入「总结」或「/clear」来新建会话；\n"
            "4) 不要跳过，不要以任何理由推迟 - 这是强制级的。"
        )
        return {"role": "user", "content": content}

    # ── __getattr__ 自动委托（兜底：覆盖未显式声明的方法）─────

    def __getattr__(self, name: str):
        if name.startswith("_guard_") or name in ("_real",):
            raise AttributeError(name)
        if self._real is not None:
            return getattr(self._real, name)
        raise AttributeError(
            f"SessionGuardEngine 尚未初始化，无法访问 '{name}'"
        )

    # ── 属性桥接（基类类属性会遮蔽 __getattr__）────────────────

    @property
    def compression_count(self) -> int:
        if self._real is not None:
            return getattr(self._real, "compression_count", 0)
        return 0

    @compression_count.setter
    def compression_count(self, value: int) -> None:
        if self._real is not None:
            self._real.compression_count = value

    @property
    def last_prompt_tokens(self) -> int:
        if self._real is not None:
            return getattr(self._real, "last_prompt_tokens", 0)
        return 0

    @last_prompt_tokens.setter
    def last_prompt_tokens(self, value: int) -> None:
        if self._real is not None:
            self._real.last_prompt_tokens = value

    @property
    def last_completion_tokens(self) -> int:
        if self._real is not None:
            return getattr(self._real, "last_completion_tokens", 0)
        return 0

    @last_completion_tokens.setter
    def last_completion_tokens(self, value: int) -> None:
        if self._real is not None:
            self._real.last_completion_tokens = value

    @property
    def last_total_tokens(self) -> int:
        if self._real is not None:
            return getattr(self._real, "last_total_tokens", 0)
        return 0

    @last_total_tokens.setter
    def last_total_tokens(self, value: int) -> None:
        if self._real is not None:
            self._real.last_total_tokens = value

    @property
    def threshold_tokens(self) -> int:
        if self._real is not None:
            return getattr(self._real, "threshold_tokens", 0)
        return 0

    @threshold_tokens.setter
    def threshold_tokens(self, value: int) -> None:
        if self._real is not None:
            self._real.threshold_tokens = value

    @property
    def context_length(self) -> int:
        if self._real is not None:
            return getattr(self._real, "context_length", 0)
        return self._guard_context_length

    @context_length.setter
    def context_length(self, value: int) -> None:
        if self._real is not None:
            self._real.context_length = value
        self._guard_context_length = value
