"""Filesystem lifecycle operations for MCP OAuth storage.

This module owns the snapshot/remove/restore/poison lifecycle while the public
``HermesTokenStorage`` class remains the compatibility surface in
``tools.mcp_oauth``.  The methods intentionally use the storage instance's
private path helpers and call ``self.remove()`` so existing subclass and
monkeypatch seams remain effective.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any

logger = logging.getLogger("tools.mcp_oauth")


class OAuthStorageLifecycleMixin:
    """Provide the on-disk OAuth state lifecycle for ``HermesTokenStorage``."""

    def remove(self) -> None:
        """Delete all stored OAuth state for this server."""
        for p in (
            self._tokens_path(),
            self._client_info_path(),
            self._meta_path(),
            self._cimd_rejected_path(),
            self._client_info_path().with_name(self._client_info_path().name + ".bak"),
        ):
            p.unlink(missing_ok=True)

    def snapshot(self) -> dict[str, bytes]:
        """Capture on-disk OAuth state so a failed re-auth can restore it.

        Maps filename -> bytes for existing primary state files; legacy backups are excluded.
        Feed back to ``restore()`` to undo an intervening ``remove()`` when a
        re-authentication attempt fails, so a still-valid token isn't destroyed.
        """
        snap: dict[str, bytes] = {}
        for p in (self._tokens_path(), self._client_info_path(), self._meta_path()):
            try:
                snap[p.name] = p.read_bytes()
            except OSError:
                pass
        return snap

    def restore(self, snapshot: dict[str, bytes], *, only_if_absent: bool = False) -> None:
        """Revert to a snapshot without overwriting a concurrent successful write."""
        allowed = {self._tokens_path().name, self._client_info_path().name, self._meta_path().name}
        invalid = [fname for fname in snapshot if not isinstance(fname, str) or fname not in allowed]
        if invalid:
            raise ValueError(f"Invalid OAuth snapshot filename(s): {', '.join(repr(name) for name in invalid)}")
        if only_if_absent and any(
            path.exists()
            for path in (self._tokens_path(), self._client_info_path(), self._meta_path())
        ):
            logger.info(
                "Skipping OAuth rollback for %s because newer state exists",
                self._server_name,
            )
            return
        self.remove()
        if not snapshot:
            return
        from tools.mcp_oauth import _get_token_dir

        token_dir = _get_token_dir(self._hermes_home)
        token_dir.mkdir(parents=True, exist_ok=True)
        for fname, data in snapshot.items():
            path = token_dir / fname
            try:
                fd = os.open(
                    str(path),
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    stat.S_IRUSR | stat.S_IWUSR,
                )
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
            except OSError as exc:
                logger.warning("Failed to restore OAuth state %s: %s", fname, exc)

    def poison_client_registration(self) -> bool:
        """Discard a dead dynamically-registered client so it gets re-created.

        Called when the IdP rejects our cached ``client_id`` with
        ``invalid_client`` on the token endpoint — proof the server-side
        registration is gone (IdP redeploy / DB wipe / rebrand). Deleting
        ``client.json`` makes the MCP SDK's ``async_auth_flow`` take the
        ``if not client_info`` branch and re-run RFC 7591 dynamic client
        registration on the next flow. The stale ``meta.json`` is dropped
        too so discovery re-runs against a freshly fetched document.
        Tokens are intentionally left in place — the subsequent
        re-authorization overwrites them, and keeping them avoids losing a
        still-valid refresh token if the re-registration never completes.

        Legacy backups are removed; cleanup errors propagate before callers clear memory.
        Returns True if a client file was present and removed.
        """
        client_path = self._client_info_path()
        backup = client_path.with_name(client_path.name + ".bak")
        if not client_path.exists():
            backup.unlink(missing_ok=True)
            return False
        backup.unlink(missing_ok=True)
        client_path.unlink(missing_ok=True)
        self._meta_path().unlink(missing_ok=True)
        logger.warning("MCP OAuth '%s': invalid_client; removed client.json, meta.json, and legacy backup", self._server_name)
        return True
