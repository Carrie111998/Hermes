"""Test that CriticSubscriber is registered in gateway startup."""

from events.subscribers.critic_trigger import CriticSubscriber


class TestCriticSubscriberRegistration:
    def test_registered_in_startup(self, monkeypatch):
        # Avoid touching real disk: patch the EventBus path resolver, the
        # cron stale config, and skip the polling thread start.
        from events import gateway_integration
        from events.bus import EventBus

        class _FakeThread:
            def __init__(self, *a, **kw): pass
            def start(self): pass
            def join(self, timeout=None): pass

        monkeypatch.setattr(gateway_integration.threading, "Thread", _FakeThread)
        # Force EventBus to use an in-memory path (sqlite tempfile)
        import tempfile
        tmp = tempfile.mkdtemp()
        monkeypatch.setattr(
            gateway_integration, "EventBus",
            lambda *a, **kw: EventBus(db_path=f"{tmp}/event_bus.db"),
        )

        try:
            gateway_integration.startup()
            registry = gateway_integration._registry
            assert registry is not None
            critic_subs = [
                s for s in registry.subscribers
                if isinstance(s, CriticSubscriber)
            ]
            assert len(critic_subs) == 1, \
                "CriticSubscriber must be registered exactly once"
        finally:
            gateway_integration.shutdown()
