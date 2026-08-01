"""会话守卫 Context Engine 插件入口。

通过 register(ctx) 模式注册到 Hermes。
启用方式：hermes config set context.engine session_guard
恢复默认：hermes config set context.engine compressor
"""

from plugins.context_engine.session_guard.engine import SessionGuardEngine


def register(ctx):
    """插件入口 — Hermes 在加载本引擎时调用。"""
    engine = SessionGuardEngine()
    ctx.register_context_engine(engine)
