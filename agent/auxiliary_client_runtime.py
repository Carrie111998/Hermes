"""Load the sharded auxiliary-client implementation into its public module."""

from pathlib import Path
from typing import MutableMapping, Any


_PARTS_DIR = Path(__file__).with_name("auxiliary_client_parts")


def install(target_globals: MutableMapping[str, Any]) -> None:
    """Install implementation shards into ``agent.auxiliary_client``."""
    for part_path in sorted(_PARTS_DIR.glob("part_*.py")):
        source = part_path.read_text(encoding="utf-8")
        exec(compile(source, str(part_path), "exec"), target_globals, target_globals)
