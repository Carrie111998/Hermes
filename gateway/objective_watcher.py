"""Gateway mixin for the governed objective runtime."""

from __future__ import annotations


class GatewayObjectiveWatcherMixin:
    async def _objective_runtime_watcher(self) -> None:
        from hermes_cli.objective_service import gateway_watcher

        await gateway_watcher(self)
