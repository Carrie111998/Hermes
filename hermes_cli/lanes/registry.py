"""Lazy lane discovery from the audited manifest."""

from __future__ import annotations

import importlib
from pathlib import Path

from hermes_cli.lanes.errors import (
    LaneModuleNotFound,
    LaneNotEnabledError,
    LaneNotFound,
)
from hermes_cli.lanes.manifest import LaneConfig, LaneManifest, load_manifest


class LaneRegistry:
    def __init__(
        self,
        *,
        manifest_path: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        self.manifest_path = manifest_path
        self.db_path = db_path
        self.manifest: LaneManifest
        self.reload()

    def reload(self) -> LaneManifest:
        self.manifest = load_manifest(
            self.manifest_path,
            db_path=self.db_path,
        )
        return self.manifest

    def list(self) -> tuple[LaneConfig, ...]:
        return self.manifest.lanes

    def config(self, lane_id: str) -> LaneConfig:
        lane = self.manifest.by_id().get(str(lane_id).strip().lower())
        if lane is None:
            raise LaneNotFound(f"unknown lane: {lane_id}")
        return lane

    def activate(self, lane_id: str):
        lane = self.config(lane_id)
        if not lane.enabled:
            raise LaneNotEnabledError(f"lane is disabled: {lane.lane_id}")
        try:
            module = importlib.import_module(lane.module)
        except ModuleNotFoundError as exc:
            raise LaneModuleNotFound(
                f"lane module is not installed: {lane.module}"
            ) from exc
        factory = getattr(module, "build_lane", None)
        if not callable(factory):
            raise LaneModuleNotFound(
                f"lane module has no build_lane(): {lane.module}"
            )
        return factory()


__all__ = ["LaneRegistry"]
