"""Read-only dirty-checkout and exported-patch migration inventory tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from grover_runtime.migration_inventory import build_migration_inventory


def _snapshot(root: Path) -> dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_inventory_is_read_only_by_default_and_reports_dirty_checkout(tmp_path: Path):
    repo = tmp_path / "dirty"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "Fixture")
    adapter = repo / "plugins" / "platforms" / "telegram" / "adapter.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("BASE = True\n", encoding="utf-8")
    _git(repo, "add", "plugins/platforms/telegram/adapter.py")
    _git(repo, "commit", "-q", "-m", "fixture")
    adapter.write_text("BASE = False\n", encoding="utf-8")
    plugin = repo / "grover_runtime" / "service.py"
    plugin.parent.mkdir()
    plugin.write_text("PRIVATE = True\n", encoding="utf-8")
    before = _snapshot(repo)

    inventory = build_migration_inventory(repo, patch_files=())

    assert _snapshot(repo) == before
    assert inventory["checkout"]["dirty"] is True
    categories = inventory["checkout"]["categories"]
    assert categories["upstreamable_adapter"] == [
        "plugins/platforms/telegram/adapter.py"
    ]
    assert categories["grizzly_specific"] == ["grover_runtime/service.py"]
    assert not (repo / "migration-inventory.json").exists()


def test_exported_patch_inventory_separates_mixed_upstream_and_private_paths(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    patch = tmp_path / "custom.patch"
    patch.write_text(
        """diff --git a/plugins/platforms/telegram/adapter.py b/plugins/platforms/telegram/adapter.py
--- a/plugins/platforms/telegram/adapter.py
+++ b/plugins/platforms/telegram/adapter.py
@@ -1 +1 @@
-old
+new
diff --git a/grover_runtime/action_service_client.py b/grover_runtime/action_service_client.py
new file mode 100644
--- /dev/null
+++ b/grover_runtime/action_service_client.py
@@ -0,0 +1 @@
+private
""",
        encoding="utf-8",
    )

    inventory = build_migration_inventory(repo, patch_files=(patch,))

    patch_row = inventory["exported_patches"][0]
    assert patch_row["sha256"] == hashlib.sha256(patch.read_bytes()).hexdigest()
    assert patch_row["categories"] == {
        "grizzly_specific": ["grover_runtime/action_service_client.py"],
        "upstreamable_adapter": ["plugins/platforms/telegram/adapter.py"],
    }
    json.dumps(inventory, allow_nan=False)
