"""GatewayRunner state context — extracted shared state (DEV-0159).

The 11 "state spine" attributes below are shared across multiple
GatewayRunner method clusters (authorization, dispatch, lifecycle,
kanban, delivery).  Separating them into a single dataclass object lets
future clusters receive a read-only or typed view instead of the full
GatewayRunner, and prepares for the behavior-cluster split (DEV-0155).

Migration plan (state-first order, DEV-0159):

  1. (this commit) Define GatewayContext dataclass; populate
     ``self._ctx`` in ``GatewayRunner.__init__`` alongside the
     existing ``self.<attr>`` copies.  No consumer changes.

  2. Replace ``self.config`` → ``self._ctx.config`` in a single
     follow-up (54 occurrences / the most-contained spine attribute).

  3. Continue through the spine: config → session_store →
     _async_session_store → _gateway_loop → _shutdown_event →
     _executor_lock → _executor → adapters → _running →
     delivery_router → _draining.

  4. After all spine attributes are accessed via ``self._ctx``,
     delete the duplicate ``self.<attr>`` assignments and begin
     behavior-cluster extraction (DEV-0155).

Every step must pass the existing test suite (29,688 tests)
before proceeding.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from gateway.config import GatewayConfig
    from gateway.delivery import DeliveryRouter
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.session_store import SessionStore
    from gateway.async_session_store import AsyncSessionStore
    from gateway.platforms.base import Platform


@dataclass
class GatewayContext:
    """Shared state spine extracted from GatewayRunner (DEV-0159).

    These 11 attributes are accessed by 2+ method clusters and form
    the cross-cutting state that must be extracted first — before
    any behavior cluster can be safely moved into its own module.
    """

    config: "GatewayConfig"
    adapters: Dict["Platform", "BasePlatformAdapter"] = field(default_factory=dict)
    session_store: Optional["SessionStore"] = None
    async_session_store: Optional["AsyncSessionStore"] = None
    delivery_router: Optional["DeliveryRouter"] = None
    _running: bool = False
    _gateway_loop: Optional[asyncio.AbstractEventLoop] = None
    _shutdown_event: Optional[asyncio.Event] = None
    _executor_lock: threading.Lock = field(default_factory=threading.Lock)
    _executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
    _draining: bool = False
