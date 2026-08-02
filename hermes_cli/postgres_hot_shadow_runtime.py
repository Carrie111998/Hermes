"""Default-off runtime bridge for bounded PostgreSQL hot-read shadowing.

SQLite is always read first and remains authoritative.  PostgreSQL work runs in
one best-effort daemon thread, has no retries, and never returns rows to serving.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import os
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable

from hermes_cli.postgres_hot_migration import TargetConfig, parse_target_dsn
from hermes_cli.postgres_hot_read_adapter import (
    FETCH_TIMEOUT_SECONDS,
    MAX_LIMIT,
    HotReadRequest,
    ShadowComparison,
    compare_shadow_messages,
    make_24h_request,
)

SHADOW_DSN_ENV = "HERMES_POSTGRES_HOT_DSN"
CONNECT_TIMEOUT_SECONDS = 2.0
CLOSE_TIMEOUT_SECONDS = 2.0
_THREAD_NAME = "hermes-postgres-hot-shadow"
_SHADOW_SLOT = threading.Lock()
logger = logging.getLogger(__name__)


def _enabled(config: Mapping[str, object]) -> bool:
    """Only a literal boolean in config.yaml enables network activity."""
    database = config.get("database")
    if not isinstance(database, Mapping):
        return False
    postgres_hot = database.get("postgres_hot")
    if not isinstance(postgres_hot, Mapping):
        return False
    return postgres_hot.get("shadow_enabled") is True


def _safe_count(metadata: Mapping[str, object], key: str) -> int:
    value = metadata.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


async def _close_connection(conn: Any) -> None:
    try:
        await asyncio.wait_for(
            conn.close(timeout=CLOSE_TIMEOUT_SECONDS),
            timeout=CLOSE_TIMEOUT_SECONDS + 0.5,
        )
    except BaseException:
        with contextlib.suppress(BaseException):
            conn.terminate()


async def _connect(target: TargetConfig) -> Any:
    import asyncpg

    return await asyncio.wait_for(
        asyncpg.connect(
            host=target.host,
            port=target.port,
            user=target.user,
            password=target.password,
            database=target.database,
            ssl=target.ssl,
            command_timeout=FETCH_TIMEOUT_SECONDS,
        ),
        timeout=CONNECT_TIMEOUT_SECONDS,
    )


async def _shadow_once(
    dsn: str,
    request: HotReadRequest,
    sqlite_rows: tuple[dict[str, Any], ...],
) -> None:
    target = parse_target_dsn(dsn)
    conn = await _connect(target)

    @contextlib.asynccontextmanager
    async def acquire():
        yield conn

    comparison: ShadowComparison
    try:
        comparison = await compare_shadow_messages(
            acquire=acquire,
            sqlite_rows=sqlite_rows,
            request=request,
        )
    finally:
        await _close_connection(conn)

    metadata = comparison.metadata
    hot_status = metadata.get("hot_status")
    if not isinstance(hot_status, str):
        hot_status = "unknown"
    logger.info(
        "PostgreSQL hot shadow outcome=%s hot_status=%s sqlite_rows=%d postgres_rows=%d",
        comparison.outcome.value,
        hot_status,
        _safe_count(metadata, "sqlite_row_count"),
        _safe_count(metadata, "postgres_row_count"),
    )


def observe_sqlite_session(
    session_db: Any,
    session_id: str,
    *,
    config: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
    now_epoch_s: float | None = None,
    _thread_factory: Callable[..., Any] = threading.Thread,
) -> bool:
    """Schedule one fail-open shadow comparison after a bounded SQLite read.

    Returns ``True`` only when a worker was started.  The return value has no
    serving authority and callers must ignore it for transcript selection.
    """
    effective_environ = os.environ if environ is None else environ
    try:
        if config is None:
            from hermes_cli.config import load_config_readonly

            effective_config = load_config_readonly()
        else:
            effective_config = config
    except Exception:
        logger.warning("PostgreSQL hot shadow config is unavailable; observation skipped")
        return False
    if not _enabled(effective_config):
        return False

    dsn = effective_environ.get(SHADOW_DSN_ENV, "")
    if not dsn:
        logger.warning("PostgreSQL hot shadow DSN is unavailable; observation skipped")
        return False

    if not _SHADOW_SLOT.acquire(blocking=False):
        logger.info("PostgreSQL hot shadow observation skipped; observer busy")
        return False

    started = False
    try:
        now_value = time.time() if now_epoch_s is None else now_epoch_s
        request = make_24h_request(
            session_id,
            now_epoch_s=now_value,
            limit=MAX_LIMIT,
            offset=0,
            include_inactive=False,
        )
        sqlite_rows = session_db.get_messages(
            session_id,
            include_inactive=False,
            limit=MAX_LIMIT,
            offset=0,
            since_timestamp=request.cutoff_epoch_s,
        )
        snapshot = tuple(copy.deepcopy(sqlite_rows))

        def worker() -> None:
            try:
                asyncio.run(_shadow_once(dsn, request, snapshot))
            except Exception:
                # Exception text may contain a DSN, SQL or driver payload.
                logger.warning("PostgreSQL hot shadow worker failed open")
            finally:
                _SHADOW_SLOT.release()

        thread = _thread_factory(target=worker, name=_THREAD_NAME, daemon=True)
        started = True
        try:
            thread.start()
        except BaseException:
            started = False
            raise
        return True
    except Exception:
        logger.warning("PostgreSQL hot shadow setup failed open")
        return False
    finally:
        if not started and _SHADOW_SLOT.locked():
            _SHADOW_SLOT.release()
