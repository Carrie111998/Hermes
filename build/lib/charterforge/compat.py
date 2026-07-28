"""Bounded compatibility for the inherited Hermes implementation."""

from __future__ import annotations

import os


def install_legacy_environment_aliases() -> None:
    """Expose canonical environment values to inherited internal readers.

    Canonical values always win. The translation is process-local and does not
    mutate files or emit secrets. It can be removed incrementally as internal
    modules adopt ``CHARTERFORGE_*`` directly.
    """
    for name, value in tuple(os.environ.items()):
        if not name.startswith("CHARTERFORGE_"):
            continue
        legacy_name = "HERMES_" + name.removeprefix("CHARTERFORGE_")
        os.environ[legacy_name] = value
