"""Configuration for the embedded Obsidian Memory Duo provider."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ObsidianDuoConfig:
    vault_path: str
    managed_folder: str = "Hermes Memory"
    index_mode: str = "lazy"
    sync_mode: str = "none"
    sync_command: tuple[str, ...] = ()
    sync_debounce_seconds: float = 30.0
    inference_mode: str = "inherit_session"
    cost_policy: str = "no_paid_fallback"
    queue_maxsize: int = 256
    recall_max_memories: int = 12
    recall_max_tokens: int = 5000
    managed_scan_min_interval_seconds: float = 5.0
    external_catalog_refresh_seconds: float = 300.0
    external_index_batch_size: int = 32

    def __post_init__(self) -> None:
        if self.index_mode not in {"lazy", "managed_first"}:
            raise ValueError("index_mode must be 'lazy' or 'managed_first'")
        if self.sync_mode not in {"none", "command"}:
            raise ValueError("sync_mode must be 'none' or 'command'")
        if self.inference_mode not in {"disabled", "inherit_session"}:
            raise ValueError("inference_mode must be 'disabled' or 'inherit_session'")
        if self.cost_policy != "no_paid_fallback":
            raise ValueError("cost_policy must be 'no_paid_fallback'")
        if not self.vault_path.strip() or not self.managed_folder.strip():
            raise ValueError("vault_path and managed_folder are required")
        if self.queue_maxsize <= 0 or self.recall_max_memories <= 0 or self.recall_max_tokens <= 0:
            raise ValueError("queue and recall bounds must be positive")
        if self.sync_debounce_seconds < 0 or self.managed_scan_min_interval_seconds < 0 or self.external_catalog_refresh_seconds < 0:
            raise ValueError("intervals cannot be negative")
        if self.external_index_batch_size <= 0:
            raise ValueError("external_index_batch_size must be positive")
        self.sync_command = tuple(self.sync_command)

    @classmethod
    def path_for(cls, hermes_home: Path) -> Path:
        return Path(hermes_home) / "obsidian_duo.json"

    @classmethod
    def find_config(cls, hermes_home: Optional[Path] = None) -> Optional[Path]:
        if hermes_home is None:
            from hermes_constants import get_hermes_home

            hermes_home = get_hermes_home()
        path = cls.path_for(Path(hermes_home))
        return path if path.is_file() else None

    @classmethod
    def load(cls, hermes_home: Path) -> "ObsidianDuoConfig":
        path = cls.path_for(Path(hermes_home))
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        values = {key: value for key, value in data.items() if key in allowed}
        return cls(**values)

    def save(self, hermes_home: Path) -> None:
        path = self.path_for(Path(hermes_home))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
