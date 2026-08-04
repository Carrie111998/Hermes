"""Lifecycle/connect mixin for the Telegram adapter (adapter god-file slice).

Extracted from ``plugins/platforms/telegram/adapter.py``: the connect /
disconnect lifecycle, the bot-identity refresh loop, post-connect
housekeeping (command menu, status indicator, DM-topic setup), the status
indicator, and delayed-delivery cancellation. ``TelegramAdapter`` imports
``TelegramLifecycleMixin`` back and inherits from it (the mixin pattern
proven by the gateway authorization/topic mixins); the shared error
classifiers (``_looks_like_network_error`` / ``_looks_like_polling_conflict``)
and the fallback-IP reader (``_fallback_ips``) the moved methods call are
moved with the cluster and still resolve via ``self`` (MRO).

Adapter-local module globals the moved methods read at call time (error
redaction, ``TELEGRAM_AVAILABLE``, the thread-deadline helpers, PTB classes,
proxy/fallback-IP discovery) stay on the adapter and are imported lazily
inside each method body, so this module never imports the adapter at import
time -> no import cycle, and monkeypatches of ``adapter.<name>`` keep
working.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Optional

# Keep log records under the adapter's logger name so operator log filters
# and caplog assertions keyed on the adapter keep working after the slice.
logger = logging.getLogger("plugins.platforms.telegram.adapter")


class TelegramLifecycleMixin:
    """Connect/disconnect lifecycle methods for TelegramAdapter."""

    async def _bot_identity_refresh_loop(self) -> None:
        """Keep the cached @username fresh when no heartbeat is running.

        Polling mode re-reads identity via the heartbeat's ``get_me()`` probe.
        Webhook mode has no such probe — nothing calls ``get_me()`` again after
        ``initialize()`` — so without this loop a BotFather rename breaks
        mention routing until the gateway restarts.
        """
        while True:
            try:
                await asyncio.sleep(self._BOT_IDENTITY_TTL_SECONDS)
                if getattr(self, "_polling_teardown_started", False):
                    return
                if self.has_fatal_error:
                    return
                await self._refresh_bot_identity(force=True)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.debug(
                    "[%s] Telegram identity refresh loop iteration failed",
                    self.name, exc_info=True,
                )

    def _start_post_connect_housekeeping(self) -> None:
        """Kick off deferred post-connect housekeeping in the background.

        Idempotent: if a previous housekeeping task is still running (e.g. a
        rapid reconnect), it is left in place rather than double-scheduled.
        """
        task = self._post_connect_task
        if task and not task.done():
            return
        self._post_connect_task = asyncio.ensure_future(
            self._run_post_connect_housekeeping()
        )

    async def _run_post_connect_housekeeping(self) -> None:
        """Register the command menu, surface the status indicator, and set up
        DM topics — all off the connect path so a slow Bot API call cannot blow
        the gateway connect timeout (#46298). Every step is non-fatal."""
        try:
            # Register bot commands so Telegram shows a hint menu when users type /
            # List is derived from the central COMMAND_REGISTRY — adding a new
            # gateway command there automatically adds it to the Telegram menu.
            try:
                from telegram import (
                    BotCommand,
                    BotCommandScopeAllPrivateChats,
                    BotCommandScopeAllGroupChats,
                    BotCommandScopeDefault,
                )
                from hermes_cli.commands import telegram_menu_commands, telegram_menu_max_commands
                if not self._bot:
                    return
                # Telegram allows up to 100 commands but has an undocumented
                # payload size limit (~4KB total).  Hermes defaults to 60 to
                # keep built-ins plus common skill commands visible while
                # staying under the threshold; users can tune the cap via
                # platforms.telegram.extra.command_menu.
                max_commands = telegram_menu_max_commands()
                menu_commands, hidden_count = telegram_menu_commands(max_commands=max_commands)
                bot_commands = [BotCommand(name, desc) for name, desc in menu_commands]
                # Register for all scopes independently — Telegram picks the
                # narrowest matching scope per chat type (forum topics fall
                # through to AllGroupChats or Default).
                for scope_cls in (BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats):
                    scope_name = getattr(scope_cls, "__name__", str(scope_cls))
                    try:
                        await self._bot.set_my_commands(bot_commands, scope=scope_cls())
                        logger.info("[%s] set_my_commands OK for scope %s (%d cmds)", self.name, scope_name, len(bot_commands))
                    except Exception as scope_err:
                        logger.warning("[%s] set_my_commands FAILED for scope %s: %s", self.name, scope_name, scope_err)
                # Forum topics don't inherit AllGroupChats — Telegram resolves
                # commands via BotCommandScopeChat(chat_id) for forum groups.
                # Lazy registration happens in _ensure_forum_commands on first
                # message from a forum topic (see _handle_text_message).
                if hidden_count:
                    logger.info(
                        "[%s] Telegram menu: %d commands registered, %d hidden (over %d limit). Use /commands for full list.",
                        self.name, len(menu_commands), hidden_count, max_commands,
                    )
            except Exception as e:
                logger.warning(
                    "[%s] Could not register Telegram command menu: %s",
                    self.name,
                    _redact_telegram_error_text(e),
                    exc_info=True,
                )

            # Surface the gateway as "Online" in the bot's short description
            # (opt-in via extra.status_indicator). Non-fatal.
            try:
                await self._set_status_indicator(online=True)
            except Exception:
                pass

            # Set up DM topics (Bot API 9.4 — Private Chat Topics)
            # Runs after connection is established so the bot can call createForumTopic.
            # Failures here are non-fatal — the bot works fine without topics.
            try:
                await self._setup_dm_topics()
            except Exception as topics_err:
                logger.warning(
                    "[%s] DM topics setup failed (non-fatal): %s",
                    self.name, topics_err, exc_info=True,
                )
        except asyncio.CancelledError:
            raise
        finally:
            if self._post_connect_task is asyncio.current_task():
                self._post_connect_task = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Telegram via polling or webhook.

        By default, uses long polling (outbound connection to Telegram).
        If ``TELEGRAM_WEBHOOK_URL`` is set, starts an HTTP webhook server
        instead.  Webhook mode is useful for cloud deployments (Fly.io,
        Railway) where inbound HTTP can wake a suspended machine.

        ``is_reconnect`` distinguishes a cold first boot (False — drop any
        stale Bot API queue) from a watcher reconnect after a prolonged
        outage (True — preserve the updates Telegram queued while the bot
        was offline, otherwise every message sent during the outage is
        silently lost). The in-process network-error ladder and the
        409-conflict handler already pass ``drop_pending_updates=False``
        for the same reason; bootstrap follows suit on the reconnect path.

        Env vars for webhook mode::

            TELEGRAM_WEBHOOK_URL    Public HTTPS URL (e.g. https://app.fly.dev/telegram)
            TELEGRAM_WEBHOOK_PORT   Local listen port (default 8443)
            TELEGRAM_WEBHOOK_HOST   Bind host (default: unset → dual-stack,
                                    all interfaces IPv4+IPv6)
            TELEGRAM_WEBHOOK_SECRET Secret token for update verification
        """
        from plugins.platforms.telegram.adapter import (
            Application,
            CallbackQueryHandler,
            HTTPXRequest,
            TELEGRAM_AVAILABLE,
            TelegramFallbackTransport,
            TelegramMessageHandler,
            _await_with_thread_deadline,
            _redact_telegram_error_text,
            _shutdown_abandoned_app,
            discover_fallback_ips,
            filters,
            resolve_proxy_url,
        )
        # Explicit connect() is the only operation allowed to reopen polling
        # after a completed, serialized teardown. Background recovery never
        # clears this fence.
        self._polling_teardown_started = False
        # Mode selection is re-evaluated on every explicit connection. Keep
        # webhook state false unless this connection starts its webhook.
        self._webhook_mode = False

        if not TELEGRAM_AVAILABLE:
            logger.error(
                "[%s] python-telegram-bot not installed. Run: pip install python-telegram-bot",
                self.name,
            )
            self._set_fatal_error("missing_dependency", "python-telegram-bot not installed", retryable=False)
            return False
        
        if not self.config.token:
            logger.error("[%s] No bot token configured", self.name)
            self._set_fatal_error("missing_credentials", "No bot token configured", retryable=False)
            return False
        
        try:
            if not self._acquire_platform_lock('telegram-bot-token', self.config.token, 'Telegram bot token'):
                return False

            # Build the application
            builder = Application.builder().token(self.config.token)
            custom_base_url = self.config.extra.get("base_url")
            if custom_base_url:
                builder = builder.base_url(custom_base_url)
                builder = builder.base_file_url(
                    self.config.extra.get("base_file_url", custom_base_url)
                )
                logger.info(
                    "[%s] Using custom Telegram base_url: %s",
                    self.name, custom_base_url,
                )
            # In local-mode telegram-bot-api, file_path is an absolute path on the
            # server's filesystem rather than a relative HTTP path. PTB needs
            # local_mode=True so download_*() reads from disk instead of issuing
            # an HTTP GET that would 404. Requires that the same path is
            # readable by the Hermes process (shared mount, same machine, etc.).
            if self.config.extra.get("local_mode"):
                builder = builder.local_mode(True)
                logger.info("[%s] Using Telegram local_mode (read files from disk)", self.name)

            # PTB defaults (pool_timeout=1s) are too aggressive on flaky networks and
            # can trigger "Pool timeout: All connections in the connection pool are occupied"
            # during reconnect/bootstrap. Use safer defaults and allow env overrides.
            def _env_int(name: str, default: int) -> int:
                try:
                    return int(os.getenv(name, str(default)))
                except (TypeError, ValueError):
                    return default

            def _env_float(name: str, default: float) -> float:
                try:
                    return float(os.getenv(name, str(default)))
                except (TypeError, ValueError):
                    return default

            request_kwargs = {
                "connection_pool_size": _env_int("HERMES_TELEGRAM_HTTP_POOL_SIZE", 512),
                "pool_timeout": _env_float("HERMES_TELEGRAM_HTTP_POOL_TIMEOUT", 8.0),
                "connect_timeout": _env_float("HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT", 10.0),
                "read_timeout": _env_float("HERMES_TELEGRAM_HTTP_READ_TIMEOUT", 20.0),
                "write_timeout": _env_float("HERMES_TELEGRAM_HTTP_WRITE_TIMEOUT", 20.0),
                # Not a duplicate of write_timeout: PTB routes any request
                # carrying files to media_write_timeout instead, so the line
                # above never applied to an upload and every upload was pinned
                # to PTB's own 20s default. httpx budgets this per socket
                # write rather than across the upload, so it is stall
                # tolerance, not a size or bandwidth allowance — a slow but
                # steady uplink never accumulates against it. 60s rides out
                # the buffer stalls a congested link produces; going higher
                # only lengthens how long a dead socket takes to report
                # itself.
                "media_write_timeout": 60.0,
            }

            # CLOSE_WAIT fd leak (#31599, same class as #18451): PTB's
            # HTTPXRequest builds the underlying httpx.AsyncClient with
            # `limits = httpx.Limits(max_connections=connection_pool_size)`
            # and *no* keepalive tuning, so httpx's default
            # keepalive_expiry=5.0 applies. Behind an HTTP proxy (Cloudflare
            # Warp etc.) a peer-initiated FIN can sit in CLOSE_WAIT longer
            # than that, leaking fds in the general request pool (_request[1])
            # which _drain_polling_connections never resets. Wire the shared
            # platform_httpx_limits() helper into the httpx client so idle
            # keepalive sockets drain aggressively, while preserving PTB's
            # max_connections (= connection_pool_size). httpx_kwargs is spread
            # last into PTB's client kwargs, so `limits` here wins.
            from gateway.platforms._http_client_limits import platform_httpx_limits

            _base_limits = platform_httpx_limits()
            if _base_limits is not None:
                import httpx as _httpx

                _pool_limits = _httpx.Limits(
                    max_connections=request_kwargs["connection_pool_size"],
                    max_keepalive_connections=_base_limits.max_keepalive_connections,
                    keepalive_expiry=_base_limits.keepalive_expiry,
                )
            else:  # pragma: no cover — httpx always present alongside PTB
                _pool_limits = None

            def _with_limits(httpx_kwargs: Optional[dict] = None) -> dict:
                """Merge tuned keepalive limits into httpx client kwargs.

                Used by the proxy and direct-DNS branches, where httpx honours
                the client-level ``limits`` kwarg. A caller-supplied ``limits``
                is left untouched; otherwise the CLOSE_WAIT-safe limits are
                injected. The fallback-IP branch does NOT use this helper — see
                the ``_transport_kwargs`` note below for why.
                """
                kwargs = dict(httpx_kwargs or {})
                if _pool_limits is not None and "limits" not in kwargs:
                    kwargs["limits"] = _pool_limits
                return kwargs

            disable_fallback = (os.getenv("HERMES_TELEGRAM_DISABLE_FALLBACK_IPS", "").strip().lower() in {"1", "true", "yes", "on"})
            fallback_ips = self._fallback_ips()
            if not fallback_ips:
                logger.warning("[%s] Discovering Telegram API fallback IPs via DNS-over-HTTPS…", self.name)
                fallback_ips = await discover_fallback_ips()
                logger.info(
                    "[%s] Auto-discovered Telegram fallback IPs: %s",
                    self.name,
                    ", ".join(fallback_ips),
                )

            proxy_targets = ["api.telegram.org", *fallback_ips]
            proxy_url = resolve_proxy_url("TELEGRAM_PROXY", target_hosts=proxy_targets)
            if fallback_ips and not proxy_url and not disable_fallback:
                logger.info(
                    "[%s] Telegram fallback IPs active: %s",
                    self.name,
                    ", ".join(fallback_ips),
                )
                # Keep request/update pools separate to reduce contention during
                # polling reconnect + bot API bootstrap/delete_webhook calls.
                # httpx ignores the client-level `limits` kwarg when a custom
                # `transport` is supplied (#58790).  Unlike the proxy/direct
                # branches (which inject limits at the client level via
                # `_with_limits`), this branch MUST pass the tuned limits
                # directly into TelegramFallbackTransport so its inner
                # AsyncHTTPTransport instances honour keepalive_expiry — do not
                # route this through `_with_limits`, httpx would discard it.
                _transport_kwargs: dict = {}
                if _pool_limits is not None:
                    _transport_kwargs["limits"] = _pool_limits
                request = HTTPXRequest(
                    **request_kwargs,
                    httpx_kwargs={
                        "transport": TelegramFallbackTransport(
                            fallback_ips, **_transport_kwargs
                        )
                    },
                )
                get_updates_request = HTTPXRequest(
                    **request_kwargs,
                    httpx_kwargs={
                        "transport": TelegramFallbackTransport(
                            fallback_ips, **_transport_kwargs
                        )
                    },
                )
            elif proxy_url:
                logger.info("[%s] Proxy detected; passing explicitly to HTTPXRequest: %s", self.name, proxy_url)
                request = HTTPXRequest(
                    **request_kwargs, proxy=proxy_url, httpx_kwargs=_with_limits()
                )
                get_updates_request = HTTPXRequest(
                    **request_kwargs, proxy=proxy_url, httpx_kwargs=_with_limits()
                )
            else:
                if disable_fallback:
                    logger.info("[%s] Telegram fallback-IP transport disabled via env", self.name)
                request = HTTPXRequest(**request_kwargs, httpx_kwargs=_with_limits())
                get_updates_request = HTTPXRequest(
                    **request_kwargs, httpx_kwargs=_with_limits()
                )

            get_updates_request = self._instrument_polling_request(get_updates_request)
            builder = builder.request(request).get_updates_request(get_updates_request)
            self._app = builder.build()
            self._bot = self._app.bot
            
            # Register handlers
            self._app.add_handler(TelegramMessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_text_message
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.COMMAND,
                self._handle_command
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.LOCATION | getattr(filters, "VENUE", filters.LOCATION),
                self._handle_location_message
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL | filters.Sticker.ALL,
                self._handle_media_message
            ))
            # Handle inline keyboard button callbacks (update prompts)
            self._app.add_handler(CallbackQueryHandler(self._handle_callback_query))
            
            # Start polling — retry initialize() for transient TLS resets.
            # Each attempt is capped by _init_timeout so a single unreachable
            # fallback-IP chain can't block startup indefinitely.
            _max_connect = 8
            _init_timeout = _env_float("HERMES_TELEGRAM_INIT_TIMEOUT", 30.0)
            # Total watchdog: ensure the entire connect loop has an upper bound
            # even if the retry loop itself silently stalls (#67498). This is
            # the per-attempt timeout PLUS generous margins between attempts so
            # we never hang past the sum even when all attempts are exhausted.
            _total_deadline = (
                asyncio.get_running_loop().time()
                + _init_timeout * _max_connect
                + 120.0  # extra margin for between-attempt sleeps + overhead
            )
            for _attempt in range(_max_connect):
                rebuild_app = False
                try:
                    # Check total watchdog deadline — if we blew past it the
                    # retry ladder must yield even if no individual attempt
                    # has raised.
                    if asyncio.get_running_loop().time() >= _total_deadline:
                        raise OSError(
                            f"Telegram initialization timed out after {_max_connect} attempts "
                            f"({_init_timeout:.0f}s each) — total connect watchdog "
                            f"deadline ({_init_timeout * _max_connect + 120.0:.0f}s) exceeded. "
                            f"Check network connectivity to api.telegram.org "
                            f"or set HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT / "
                            f"HERMES_TELEGRAM_INIT_TIMEOUT to a lower value."
                        )
                    logger.warning(
                        "[%s] Connecting to Telegram (attempt %d/%d)…",
                        self.name, _attempt + 1, _max_connect,
                    )
                    await _await_with_thread_deadline(
                        self._app.initialize(),
                        timeout=_init_timeout,
                        # On timeout the initialize() task is abandoned without
                        # awaiting its cancellation (it may be wedged in a
                        # shielded scope). Best-effort release the half-built
                        # app's httpx client/connection pool so it isn't leaked
                        # across the retry ladder (mirrors the client-close-on-
                        # timeout pattern in agent/auxiliary_client.py).
                        on_abandon=lambda app=self._app: _shutdown_abandoned_app(app),
                    )
                    break
                except asyncio.TimeoutError:
                    rebuild_app = True
                    if _attempt < _max_connect - 1:
                        wait = min(2 ** _attempt, 15)
                        logger.warning(
                            "[%s] Connect attempt %d/%d timed out after %.0fs — retrying in %ds",
                            self.name, _attempt + 1, _max_connect, _init_timeout, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise OSError(
                            f"Telegram initialization timed out after {_max_connect} attempts "
                            f"({_init_timeout:.0f}s each). Check network connectivity to api.telegram.org "
                            f"or set HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT to a lower value."
                        )
                except OSError as init_err:
                    rebuild_app = True
                    if _attempt < _max_connect - 1:
                        wait = min(2 ** _attempt, 15)
                        logger.warning(
                            "[%s] Connect attempt %d/%d failed: %s — retrying in %ds",
                            self.name, _attempt + 1, _max_connect, init_err, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise
                except Exception as init_err:
                    rebuild_app = True
                    if not self._looks_like_network_error(init_err):
                        raise
                    if _attempt < _max_connect - 1:
                        wait = min(2 ** _attempt, 15)
                        logger.warning(
                            "[%s] Connect attempt %d/%d failed: %s — retrying in %ds",
                            self.name, _attempt + 1, _max_connect, init_err, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise
                except BaseException:
                    # Catch CancelledError and other BaseException subclasses
                    # that the existing except handlers miss. Log the event so
                    # the operator can diagnose, then reraise so cancellation
                    # semantics are preserved (#67498).
                    # NOTE: placed LAST so Exception handlers above have
                    # priority — BaseException catches everything including
                    # Exception.
                    logger.warning(
                        "[%s] Connect attempt %d/%d interrupted by %s — propagating",
                        self.name,
                        _attempt + 1,
                        _max_connect,
                        "CancelledError"
                        if isinstance(sys.exc_info()[1], asyncio.CancelledError)
                        else type(sys.exc_info()[1]).__name__,
                    )
                    raise
                finally:
                    # After a failed attempt the app may be in a partially-
                    # initialized state (closed transports, half-built handlers).
                    # Rebuild from the same token/config so the next attempt
                    # starts with a fresh Application — the old one is discarded
                    # and will be GC'd (#67498).
                    if rebuild_app and _attempt < _max_connect - 1:
                        old_app = self._app
                        self._app = builder.build()
                        self._bot = self._app.bot
                        # Re-register handlers on the new app
                        self._app.add_handler(TelegramMessageHandler(
                            filters.TEXT & ~filters.COMMAND,
                            self._handle_text_message
                        ))
                        self._app.add_handler(TelegramMessageHandler(
                            filters.COMMAND,
                            self._handle_command
                        ))
                        self._app.add_handler(TelegramMessageHandler(
                            filters.LOCATION | getattr(filters, "VENUE", filters.LOCATION),
                            self._handle_location_message
                        ))
                        self._app.add_handler(TelegramMessageHandler(
                            filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL | filters.Sticker.ALL,
                            self._handle_media_message
                        ))
                        self._app.add_handler(CallbackQueryHandler(self._handle_callback_query))
                        # Best-effort discard the old app's resources
                        try:
                            await _shutdown_abandoned_app(old_app)
                        except Exception:
                            pass
            await self._app.start()

            # Decide between webhook and polling mode
            webhook_started = await self._start_webhook(is_reconnect=is_reconnect)
            if not webhook_started:
                # ── Polling mode (default) ───────────────────────────
                # Clear any stale webhook first so polling doesn't inherit a
                # previous webhook registration and silently stop receiving
                # updates. Best-effort: a transient Bot API network error here
                # must not fail gateway startup — degrade to background polling
                # recovery instead.
                await self._delete_webhook_best_effort(
                    require_success=not is_reconnect
                )

                loop = asyncio.get_running_loop()

                def _polling_error_callback(error: Exception) -> None:
                    if getattr(self, "_polling_teardown_started", False):
                        return
                    if self._polling_error_task and not self._polling_error_task.done():
                        return
                    if self._looks_like_polling_conflict(error):
                        # Synchronously stop PTB's internal network_retry_loop
                        # BEFORE scheduling our async recovery task.  PTB calls
                        # this callback synchronously inside its loop and then
                        # keeps polling on its own; if we only schedule a task
                        # here, PTB's retry and our stop->restart overlap and
                        # produce a fresh 409.  Disarming the loop now makes it
                        # exit on its next tick so recovery owns polling alone.
                        self._disarm_ptb_retry_loop()
                        self._polling_error_task = loop.create_task(self._handle_polling_conflict(error))
                        self._background_tasks.add(self._polling_error_task)
                        self._polling_error_task.add_done_callback(self._background_tasks.discard)
                    elif self._looks_like_network_error(error):
                        logger.warning("[%s] Telegram network _redact_telegram_error_text(error), scheduling reconnect: %s", self.name, error)
                        self._polling_error_task = loop.create_task(self._handle_polling_network_error(error))
                        self._background_tasks.add(self._polling_error_task)
                        self._polling_error_task.add_done_callback(self._background_tasks.discard)
                    else:
                        logger.error("[%s] Telegram polling _redact_telegram_error_text(error): %s", self.name, error, exc_info=True)

                # Store reference for retry use in _handle_polling_conflict
                self._polling_error_callback_ref = _polling_error_callback

                polling_started = await self._start_polling_resilient(
                    # On a cold first boot drop the stale Bot API queue; on a
                    # watcher reconnect after an outage preserve it so messages
                    # sent while the bot was offline are delivered (#46621).
                    drop_pending_updates=not is_reconnect,
                    error_callback=_polling_error_callback,
                    require_progress=not is_reconnect,
                )
                if not polling_started:
                    logger.warning(
                        "[%s] Connected in degraded Telegram mode: gateway is alive, "
                        "polling will be retried in the background",
                        self.name,
                    )
            
            self._mark_connected()
            mode = "webhook" if self._webhook_mode else "polling"
            logger.info("[%s] Connected to Telegram (%s mode)", self.name, mode)

            # Start the persistent heartbeat loop in polling mode. Webhook mode
            # receives updates via incoming pushes — there is no long-poll
            # socket to wedge in CLOSE-WAIT, so the loop is not needed there.
            if not self._webhook_mode:
                if self._polling_heartbeat_task and not self._polling_heartbeat_task.done():
                    self._polling_heartbeat_task.cancel()
                self._polling_heartbeat_task = asyncio.ensure_future(
                    self._polling_heartbeat_loop()
                )

            # Seed the live identity from whatever PTB cached during
            # initialize(), then keep it fresh. Polling mode rides the
            # heartbeat's get_me() probe; webhook mode has no probe at all, so
            # it gets a dedicated low-frequency refresh loop — otherwise a
            # BotFather rename breaks mention routing until restart.
            self._note_bot_username(getattr(self._bot, "username", None))
            self._bot_identity_checked_at = time.monotonic()
            if self._webhook_mode:
                identity_task = getattr(self, "_bot_identity_refresh_task", None)
                if identity_task and not identity_task.done():
                    identity_task.cancel()
                self._bot_identity_refresh_task = asyncio.ensure_future(
                    self._bot_identity_refresh_loop()
                )

            # Command-menu registration, DM-topic setup, and the status
            # indicator each make Bot API calls that can stall for certain
            # tokens. Running them here — inside the connect() coroutine that
            # the gateway wraps in a connect timeout — means one slow call
            # blows the whole connect and the adapter never comes up, even
            # though polling/webhook is already live (#46298). Defer them to a
            # cancellable background task so connect() returns as soon as the
            # transport is up.
            self._start_post_connect_housekeeping()

            return True
            
        except Exception as e:
            self._release_platform_lock()
            safe_error = _redact_telegram_error_text(e)
            message = f"Telegram startup failed: {safe_error}"
            self._set_fatal_error("telegram_connect_error", message, retryable=True)
            logger.error("[%s] Failed to connect to Telegram: %s", self.name, safe_error)
            return False

    async def _set_status_indicator(self, online: bool) -> None:
        """Set the bot's short description to the online/offline status text.

        The short description is the line shown under the bot's name in its
        profile. It is the closest Bot API surface to a presence indicator —
        bots have no real online/offline dot (that's a user-account feature).

        No-op unless ``extra.status_indicator`` is enabled. Best-effort: any
        failure is logged at debug and swallowed so it never blocks connect or
        disconnect. The default (no language_code) description applies to every
        user who doesn't have a language-specific one set.
        """
        from plugins.platforms.telegram.adapter import _redact_telegram_error_text
        if not getattr(self, "_status_indicator_enabled", False):
            return
        bot = self._bot
        if bot is None:
            return
        text = self._status_online_text if online else self._status_offline_text
        # Telegram caps short_description at 120 chars.
        text = text[:120]
        try:
            await bot.set_my_short_description(short_description=text)
            logger.info("[%s] Set bot status indicator to %r", self.name, text)
        except Exception as e:
            logger.debug(
                "[%s] Failed to set bot status indicator to %r: %s",
                self.name, text, _redact_telegram_error_text(e),
            )

    async def _cancel_pending_delivery_tasks(self) -> None:
        """Cancel every delayed-delivery task family before disconnect completes.

        Covers media-group, photo-batch and text-batch flush tasks plus the
        polling-error recovery task. Each sits behind an ``asyncio.sleep()``;
        if teardown leaves them running they dispatch ``handle_message`` into a
        torn-down session. Skips the current task so the coroutine driving
        teardown does not cancel itself.
        """
        current_task = asyncio.current_task()
        pending_tasks: list[asyncio.Task] = []
        awaitable_tasks: list[asyncio.Task] = []
        seen: set[int] = set()

        def collect(task: Optional[asyncio.Task]) -> None:
            if not task or task.done() or task is current_task:
                return
            marker = id(task)
            if marker in seen:
                return
            seen.add(marker)
            pending_tasks.append(task)
            if asyncio.isfuture(task) or asyncio.iscoroutine(task):
                awaitable_tasks.append(task)

        for task in list(self._media_group_tasks.values()):
            collect(task)
        for task in list(self._pending_photo_batch_tasks.values()):
            collect(task)
        for task in list(self._pending_text_batch_tasks.values()):
            collect(task)
        collect(getattr(self, "_polling_error_task", None))
        collect(getattr(self, "_polling_progress_verifier_task", None))

        for task in pending_tasks:
            task.cancel()
        if awaitable_tasks:
            await asyncio.gather(*awaitable_tasks, return_exceptions=True)

        self._media_group_tasks.clear()
        self._media_group_events.clear()
        self._pending_photo_batch_tasks.clear()
        self._pending_photo_batches.clear()
        self._pending_text_batch_tasks.clear()
        self._pending_text_batches.clear()
        if getattr(self, "_polling_error_task", None) is not current_task:
            self._polling_error_task = None
        if getattr(self, "_polling_progress_verifier_task", None) is not current_task:
            self._polling_progress_verifier_task = None

    async def disconnect(self) -> None:
        """Stop polling/webhook, cancel pending delayed deliveries, and disconnect."""
        from plugins.platforms.telegram.adapter import _UPDATER_STOP_TIMEOUT, _redact_telegram_error_text
        # Mark disconnected first so the drop guard short-circuits any flush
        # that wins the race against teardown and prevents new delayed tasks
        # from being scheduled by late update handlers.
        self._mark_disconnected()
        self._polling_teardown_started = True
        self._polling_progress_accepting = False
        self._polling_generation = getattr(self, "_polling_generation", 0) + 1
        self._send_path_degraded = True

        # Recovery can be suspended in stop/drain/start while disconnect begins.
        # Cancel and await both polling lifecycle owners immediately after the
        # fence, before any other teardown await lets them start a new generation.
        current_task = asyncio.current_task()
        lifecycle_tasks: list[asyncio.Task] = []
        lifecycle_seen: set[int] = set()
        for task in (
            getattr(self, "_polling_error_task", None),
            getattr(self, "_polling_progress_verifier_task", None),
        ):
            if not task or task.done() or task is current_task:
                continue
            marker = id(task)
            if marker in lifecycle_seen:
                continue
            lifecycle_seen.add(marker)
            task.cancel()
            if asyncio.isfuture(task) or asyncio.iscoroutine(task):
                lifecycle_tasks.append(task)
        if lifecycle_tasks:
            await asyncio.gather(*lifecycle_tasks, return_exceptions=True)
        if getattr(self, "_polling_error_task", None) is not current_task:
            self._polling_error_task = None
        if getattr(self, "_polling_progress_verifier_task", None) is not current_task:
            self._polling_progress_verifier_task = None

        # Cancellation callbacks may have run while awaited; the teardown fence
        # remains authoritative regardless of their finalizers.
        self._polling_progress_accepting = False
        self._send_path_degraded = True

        # Cancel deferred post-connect housekeeping (command-menu / DM-topic /
        # status-indicator Bot API calls) so it cannot fire into a half-torn-down
        # bot client (#46298). getattr guards the object.__new__ test pattern
        # where __init__ (which sets this attr) is never called.
        post_connect_task = getattr(self, "_post_connect_task", None)
        if post_connect_task and not post_connect_task.done():
            post_connect_task.cancel()
            await asyncio.gather(post_connect_task, return_exceptions=True)
        self._post_connect_task = None

        # Cancel the heartbeat before tearing down the app so the probe task
        # cannot fire get_me() into a half-shutdown bot client.
        polling_heartbeat_task = getattr(self, "_polling_heartbeat_task", None)
        if polling_heartbeat_task and not polling_heartbeat_task.done():
            polling_heartbeat_task.cancel()
            try:
                await polling_heartbeat_task
            except asyncio.CancelledError:
                pass
        self._polling_heartbeat_task = None

        # Cancel the webhook-mode identity refresh loop on the same fence as
        # the heartbeat so it cannot fire get_me() into a torn-down client.
        identity_task = getattr(self, "_bot_identity_refresh_task", None)
        if identity_task and not identity_task.done():
            identity_task.cancel()
            try:
                await identity_task
            except asyncio.CancelledError:
                pass
        self._bot_identity_refresh_task = None

        # Mark the bot "Offline" in its short description while the bot's HTTP
        # client is still alive (before app shutdown closes it). Opt-in via
        # extra.status_indicator. Non-fatal. This is the clean-shutdown path;
        # a hard crash leaves the last-known status, which is the expected
        # limitation of a profile-text indicator.
        try:
            await self._set_status_indicator(online=False)
        except Exception:
            pass

        await self._cancel_pending_delivery_tasks()

        if self._app:
            try:
                # Only stop the updater if it's running.  Bounded with a
                # timeout: a CLOSE-WAIT socket can wedge stop() on epoll
                # indefinitely, which would hang disconnect() (and any
                # gateway shutdown/restart waiting on it) forever.  On timeout
                # we fall through to app.stop()/shutdown() to force teardown.
                if self._app.updater and self._app.updater.running:
                    try:
                        await asyncio.wait_for(self._app.updater.stop(), timeout=_UPDATER_STOP_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[%s] updater.stop() timed out during disconnect "
                            "(likely CLOSE-WAIT socket); forcing app shutdown",
                            self.name,
                        )
                if self._app.running:
                    await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning(
                    "[%s] Error during Telegram disconnect: %s",
                    self.name, _redact_telegram_error_text(e),
                )
        self._release_platform_lock()

        self._app = None
        self._bot = None
        logger.info("[%s] Disconnected from Telegram", self.name)

    def _fallback_ips(self) -> list[str]:
        """Return validated fallback IPs from config (populated by _apply_env_overrides)."""
        from plugins.platforms.telegram.adapter import parse_fallback_ip_env
        configured = self.config.extra.get("fallback_ips", []) if getattr(self.config, "extra", None) else []
        if isinstance(configured, str):
            configured = configured.split(",")
        return parse_fallback_ip_env(",".join(str(v) for v in configured) if configured else None)

    @staticmethod
    def _looks_like_polling_conflict(error: Exception) -> bool:
        text = str(error).lower()
        return (
            error.__class__.__name__.lower() == "conflict"
            or "terminated by other getupdates request" in text
            or "another bot instance is running" in text
        )

    @staticmethod
    def _looks_like_network_error(error: Exception) -> bool:
        """Return True for transient transport failures that warrant reconnect."""
        name = error.__class__.__name__.lower()
        if name in {"badrequest", "invalidtoken", "forbidden", "retryafter"}:
            return False
        if name in {"networkerror", "timedout", "connectionerror"}:
            return True
        try:
            from telegram.error import (
                BadRequest,
                Forbidden,
                InvalidToken,
                NetworkError,
                RetryAfter,
                TimedOut,
            )
            if isinstance(error, (BadRequest, InvalidToken, Forbidden, RetryAfter)):
                return False
            if isinstance(error, (NetworkError, TimedOut)):
                return True
        except ImportError:
            pass
        return isinstance(error, OSError)
