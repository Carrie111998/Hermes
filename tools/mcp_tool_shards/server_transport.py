"""Executable source shard for the legacy MCP tool seam.

The source is compiled with the original module namespace so public
imports and monkeypatch targets remain tools.mcp_tool-compatible.
"""
import linecache
from pathlib import Path

_SOURCE = r'''

class _MCPServerTaskTransportMixin:
    __slots__ = ()

    async def _wait_for_reconnect_or_shutdown(
            self, timeout: Optional[float] = None
        ) -> str:
            """Block until a reconnect or shutdown is requested while parked.

            Used by :meth:`run` after the reconnect budget is exhausted. The
            task stays alive (so ``_reconnect_event`` always has a listener) but
            does no work until something explicitly asks it to come back —
            OAuth recovery, a manual ``/mcp`` refresh — or, when ``timeout`` is
            given, until the timeout elapses (a periodic self-probe). The timed
            wake matters because parking deregisters this server's tools, so
            no tool call can ever reach the circuit-breaker's half-open probe
            or ``_signal_reconnect`` — without a self-probe a parked server
            would be unrevivable short of a full reload.

            Returns:
                ``"shutdown"`` if the server should exit the run loop entirely,
                ``"reconnect"`` if it should rebuild the transport (explicit
                request or self-probe timeout). The reconnect event is cleared
                before returning so the next park cycle starts from a fresh
                signal. Shutdown takes precedence.
            """
            shutdown_task = asyncio.ensure_future(self._shutdown_event.wait())
            reconnect_task = asyncio.ensure_future(self._reconnect_event.wait())
            try:
                await asyncio.wait(
                    {shutdown_task, reconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=timeout,
                )
            finally:
                for t in (shutdown_task, reconnect_task):
                    if not t.done():
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
            if self._shutdown_event.is_set():
                return "shutdown"
            self._reconnect_event.clear()
            return "reconnect"

    async def _run_stdio(self, config: dict):
            """Run the server using stdio transport."""
            if config.get("identity_header") is not None:
                # Headers don't exist on stdio transports — warn and ignore so a
                # copy-pasted HTTP config block doesn't silently mislead.
                logger.warning(
                    "MCP server '%s': identity_header is only supported on "
                    "HTTP/SSE transports — ignored for stdio servers", self.name,
                )
            if not _ensure_mcp_sdk():
                raise ImportError(
                    f"MCP server '{self.name}' requires the 'mcp' Python SDK, but "
                    "it is not installed. Run `hermes setup` to install MCP support, "
                    "then retry."
                )

            command = config.get("command")
            args = config.get("args", [])
            user_env = config.get("env")

            if not command:
                raise ValueError(
                    f"MCP server '{self.name}' has no 'command' in config"
                )

            safe_env = _build_safe_env(user_env)
            command, safe_env = _resolve_stdio_command(command, safe_env)

            # Check package against OSV malware database before spawning.
            # Run off the event loop (the urllib HTTPS call is blocking) and bound
            # it with a wall-clock timeout so a stalled SSL handshake can't freeze
            # MCP discovery / gateway startup (#29184). The check is fail-open, so
            # on timeout we log and proceed rather than blocking indefinitely.
            # NOTE: must run against the REAL command/args — the watchdog wrap
            # below rewrites argv to `python -m tools.mcp_stdio_watchdog …`,
            # which would silently turn the preflight into a no-op.
            from tools.osv_check import check_package_for_malware
            try:
                malware_error = await asyncio.wait_for(
                    asyncio.to_thread(check_package_for_malware, command, args),
                    timeout=_OSV_MALWARE_CHECK_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP server '%s': OSV malware preflight timed out after %.0fs "
                    "(network slow/unreachable) — proceeding without the check.",
                    self.name, _OSV_MALWARE_CHECK_TIMEOUT_S,
                )
                malware_error = None
            if malware_error:
                raise ValueError(
                    f"MCP server '{self.name}': {malware_error}"
                )

            # Wrap the real command in a parent-death watchdog supervisor so an
            # ungraceful exit of this Hermes process (kill -9, crash, force-quit)
            # can't leave the stdio MCP child (and its own descendants, e.g.
            # mcp-remote's spawned `node`) running forever. On a clean exit,
            # MCPServerTask.shutdown() / _kill_orphaned_mcp_children() still do
            # the reaping as before -- this only covers the case where that code
            # never gets to run. POSIX-only (relies on process groups); no-op
            # elsewhere, matching existing killpg-based cleanup's platform scope.
            # Applied AFTER the OSV preflight so the check inspects the real
            # package, not the watchdog wrapper.
            command, args = _wrap_command_with_watchdog(command, args)

            server_params = StdioServerParameters(
                command=command,
                args=args,
                env=safe_env if safe_env else None,
                cwd=config.get("cwd"),
                # On Windows, pipe I/O can deliver non-UTF-8 bytes at chunk
                # boundaries.  Use "replace" to substitute undecodable bytes
                # with U+FFFD instead of crashing with UnicodeDecodeError.
                encoding_error_handler="replace",
            )

            sampling_kwargs = self._sampling.session_kwargs() if self._sampling else {}
            if self._elicitation:
                sampling_kwargs.update(self._elicitation.session_kwargs())
            if _MCP_NOTIFICATION_TYPES and _MCP_MESSAGE_HANDLER_SUPPORTED:
                sampling_kwargs["message_handler"] = self._make_message_handler()
            if _MCP_LOGGING_CALLBACK_SUPPORTED:
                sampling_kwargs["logging_callback"] = self._make_logging_callback()

            # Reap any orphaned subprocesses from prior failed connection
            # attempts before spawning a new one.  Without this, each retry in
            # the run() reconnect loop spawns a fresh process pair while the
            # previous failed pair lingers — leading to rapid zombie
            # accumulation (see #57355, #57228).  The unscoped sweep also
            # opportunistically reaps orphans left by *other* servers that
            # never reconnect; per-server filtering via ``server_name`` remains
            # available for scoped call sites.  Run in a worker thread: the
            # reaper blocks up to 2s (SIGTERM → wait → SIGKILL) when orphans
            # exist, which would otherwise stall the shared MCP event loop.
            await asyncio.to_thread(_kill_orphaned_mcp_children)

            # Snapshot child PIDs before spawning so we can track the new one.
            pids_before = _snapshot_child_pids()
            new_pids: set = set()
            # Redirect subprocess stderr into a shared log file so MCP servers
            # (FastMCP banners, slack-mcp startup JSON, etc.) don't dump onto
            # the user's TTY and corrupt the TUI.  Preserves debuggability via
            # ~/.hermes/logs/mcp-stderr.log.
            _write_stderr_log_header(self.name)
            _errlog = _get_mcp_stderr_log()
            try:
                async with stdio_client(server_params, errlog=_errlog) as (
                    read_stream,
                    write_stream,
                ):
                    # Capture the newly spawned subprocess PID for force-kill cleanup.
                    # Filter out non-MCP children that race into the snapshot window:
                    # slash_worker and LSP servers (jdtls/pyright/yaml-ls) are spawned
                    # directly by the gateway without start_new_session, so their pgid
                    # equals the TUI parent PID. If they leak into _stdio_pgids, the
                    # shutdown sweep's killpg() kills the TUI parent itself.
                    # See agent/lsp/client.py for the complementary start_new_session fix.
                    new_pids = _filter_mcp_children(
                        _snapshot_child_pids() - pids_before
                    )
                    if new_pids:
                        # Capture pgid while the child is alive — once it exits we
                        # can no longer call ``os.getpgid`` on it, and the cleanup
                        # sweep needs the pgid to reach any reparented descendants
                        # (e.g. ``claude mcp serve`` spawned by a stdio wrapper).
                        new_pgids: Dict[int, int] = {}
                        for _pid in new_pids:
                            try:
                                new_pgids[_pid] = os.getpgid(_pid)
                            except (AttributeError, ProcessLookupError, OSError):
                                # AttributeError: Windows (os.getpgid is POSIX-only)
                                # ProcessLookupError: child raced and already exited
                                pass
                        with _lock:
                            for _pid in new_pids:
                                _stdio_pids[_pid] = self.name
                            _stdio_pgids.update(new_pgids)
                        # Positive identity for the machine spawn ledger (#61514):
                        # record each helper child as (pid, create_time,
                        # 'mcp-helper', spawner=this process) so startup sweeps
                        # can reap orphans left after an unclean parent exit.
                        # Best-effort — never let ledger I/O break MCP startup.
                        for _pid in new_pids:
                            try:
                                from hermes_cli.process_identity import register_child

                                register_child(_pid, "mcp-helper")
                            except Exception:
                                logger.debug(
                                    "spawn-ledger register_child failed for MCP "
                                    "helper pid %s",
                                    _pid,
                                    exc_info=True,
                                )
                    # Track the spawned children on the connection object for
                    # fast-fail of in-flight calls when the subprocess dies
                    # (#81995).
                    self._stdio_child_pids = set(new_pids)
                    async with ClientSession(
                        read_stream, write_stream, **sampling_kwargs
                    ) as session:
                        # Bound the MCP handshake. A stdio server that never
                        # completes ``initialize`` (e.g. emits a non-JSON-RPC frame
                        # and then blocks on stdin) otherwise hangs this coroutine
                        # forever on the background loop: ``connect_timeout`` only
                        # bounds the caller's ``.result()`` wait, not the coroutine
                        # itself. Because the connect never unwinds, the cleanup
                        # ``finally`` below never runs, so the spawned child and its
                        # stdio pipes/pidfd leak on every discovery retry — unbounded
                        # until the gateway hits EMFILE. Timing out here converts the
                        # hang into a normal failure, letting the ``finally`` reap the
                        # child. See #59349.
                        connect_timeout = float(
                            config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
                        )
                        self.initialize_result = await self._negotiate_session(
                            session, connect_timeout
                        )
                        self.session = session
                        self._mark_lifecycle_started()
                        await self._discover_tools()
                        self._ready.set()
                        self._ever_connected = True
                        # Session is live again: clear any breaker state from a
                        # prior outage so the first call after recovery isn't
                        # gated on a stale consecutive-failure count (#16788).
                        _reset_server_error(self.name)
                        # A completed handshake alone is NOT proof of health: a
                        # flapping transport can handshake fine and drop moments
                        # later, forever (#62212). The session must prove itself
                        # (keepalive success or a successful tool call) before the
                        # reconnect budget is cleared — see _mark_session_proven.
                        self._session_proven = False
                        # stdio transport does not use OAuth, but we still honor
                        # _reconnect_event (e.g. future manual /mcp refresh) for
                        # consistency with _run_http.
                        return await self._wait_for_lifecycle_event()
            finally:
                # Runs on clean exit, exceptions, AND asyncio cancellation.
                # If any of the spawned PIDs are still alive, the SDK's
                # teardown failed (common when the task is cancelled mid-way
                # on Linux, where setsid() children escape the parent cgroup).
                # Mark them as orphans so the next cleanup sweep can reap them.
                if new_pids:
                    from gateway.status import _pid_exists
                    _killpg = getattr(os, "killpg", None)
                    with _lock:
                        for _pid in new_pids:
                            _stdio_pids.pop(_pid, None)
                        for pid in new_pids:
                            # ``os.kill(pid, 0)`` is NOT a no-op on Windows
                            # (bpo-14484). Use the cross-platform check.
                            pid_alive = _pid_exists(pid)
                            pgroup_alive = False
                            pgid = _stdio_pgids.get(pid)
                            if not pid_alive and pgid is not None and _killpg is not None:
                                # Direct child exited but descendants may still be
                                # in its pgroup (e.g. ``claude mcp serve`` spawned
                                # by an MCP wrapper that exited first).  Probe with
                                # signal 0 — succeeds iff any pgroup member is alive.
                                try:
                                    _killpg(pgid, 0)
                                    pgroup_alive = True
                                except (ProcessLookupError, PermissionError, OSError):
                                    pgroup_alive = False
                            if pid_alive or pgroup_alive:
                                _orphan_stdio_pids.add(pid)
                                _orphan_stdio_pid_servers[pid] = self.name
                            else:
                                # Nothing left to reap — drop the pgid entry so
                                # PID-reuse can't surface stale pgroup state later.
                                _stdio_pgids.pop(pid, None)


    async def _preflight_content_type(
            self,
            url: str,
            *,
            headers: Optional[dict] = None,
            ssl_verify: bool = True,
            client_cert=None,
            timeout: float = 5.0,
        ) -> None:
            """Probe *url* for an MCP-shaped response before the SDK connects.

            A misconfigured ``mcp_servers.<name>.url`` pointed at a plain web app
            returns HTML (or some other non-MCP body). The MCP SDK then sits on
            the connection for the full ``connect_timeout`` (default 60 s) before
            surfacing an opaque ``CancelledError``. A cheap, short-timeout probe
            here catches that in ≤ ``timeout`` seconds and raises
            :class:`NonMcpEndpointError` with an actionable message.

            Detection is allow-list based: a 2xx response is rejected only when it
            carries a definite content type that is NOT one an MCP endpoint uses
            (``application/json`` / ``text/event-stream``).  When HEAD/GET returns
            a non-MCP content type (e.g. ``text/html``), a lightweight JSON-RPC
            ``initialize`` POST is attempted before giving up — some servers
            (e.g. DocuSeal) serve a web UI on GET but speak Streamable HTTP only
            via POST.

            A missing or empty content type, non-2xx status, or any
            network/transport error passes through silently — the probe is
            strictly best-effort, and the real handshake remains the source of
            truth for everything except the unambiguous "this is a web page,
            not MCP" case.

            Runs on its own httpx client OUTSIDE the SDK's anyio task group, so the
            raised error propagates as itself rather than being wrapped in an
            ``ExceptionGroup`` (which is what defeats hooks installed inside the
            SDK transport).
            """
            try:
                import httpx as _httpx
            except ImportError:
                return  # No httpx → skip probe; SDK import would have failed first.

            client_kwargs: dict = {
                "verify": ssl_verify,
                "follow_redirects": True,
                "timeout": _httpx.Timeout(timeout),
            }
            if client_cert is not None:
                client_kwargs["cert"] = client_cert

            probe_headers = dict(headers) if headers else {}
            try:
                async with _httpx.AsyncClient(**client_kwargs) as client:
                    # HEAD is cheapest; fall back to GET if the server doesn't
                    # implement it (405 Method Not Allowed / 501 Not Implemented).
                    resp = await client.head(url, headers=probe_headers)
                    if resp.status_code in (405, 501):
                        resp = await client.get(url, headers=probe_headers)

                    # Some MCP servers (e.g. DocuSeal) serve their web UI on
                    # HEAD/GET but speak Streamable HTTP only via POST.  Before
                    # rejecting the endpoint, try a lightweight JSON-RPC POST
                    # probe so we don't false-positive on POST-only servers.
                    ct = (
                        resp.headers.get("content-type", "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    if (
                        ct
                        and ct not in self._MCP_CONTENT_TYPES
                        and 200 <= resp.status_code < 300
                    ):
                        post_resp = await client.post(
                            url,
                            headers={
                                **probe_headers,
                                "Content-Type": "application/json",
                                "Accept": "application/json, text/event-stream",
                            },
                            content=(
                                '{"jsonrpc":"2.0","id":"_probe",'
                                '"method":"initialize",'
                                '"params":{"protocolVersion":"2025-03-26",'
                                '"capabilities":{},'
                                '"clientInfo":{"name":"hermes-probe",'
                                '"version":"0.1"}}}'
                            ),
                        )
                        if 200 <= post_resp.status_code < 300:
                            post_ct = (
                                post_resp.headers.get("content-type", "")
                                .split(";")[0]
                                .strip()
                                .lower()
                            )
                            if post_ct in self._MCP_CONTENT_TYPES:
                                resp = post_resp
            except _httpx.HTTPError:
                return  # DNS/connect/timeout/transport error — let the SDK try.

            # Only judge successful responses. A 4xx/5xx may be an auth challenge
            # or a transient error the real handshake handles correctly.
            if not (200 <= resp.status_code < 300):
                return

            ct_base = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if not ct_base:
                return  # No content type advertised — don't second-guess the SDK.
            if ct_base in self._MCP_CONTENT_TYPES:
                return  # Looks like a real MCP endpoint.

            raise NonMcpEndpointError(
                f"MCP server '{self.name}' at {url} returned Content-Type "
                f"'{ct_base}', not an MCP response (expected one of: "
                f"{', '.join(self._MCP_CONTENT_TYPES)}). The URL most likely "
                "points at a web page rather than an MCP endpoint — check it "
                "resolves to a Streamable HTTP / SSE endpoint "
                "(e.g. https://host/mcp, not https://host/)."
            )

    def _reconnect_or_reraise_group(self, eg: BaseExceptionGroup) -> str:
            """Map an SDK transport TaskGroup failure to a clean ``"reconnect"``.

            Streamable-HTTP / SSE transports run their stream pump inside an anyio
            TaskGroup. A transient stream drop (idle timeout, brief backend blip,
            server-side TCP close) surfaces as a ``BaseExceptionGroup`` escaping the
            transport context manager. Left unwrapped it reaches ``run()``'s error
            path, which applies exponential backoff and eventually *parks* the
            server for 300s and deregisters its tools — a multi-minute tool outage
            for what is usually a sub-second glitch while the POST path stays
            healthy (issue #66092).

            Returning ``"reconnect"`` instead lets ``run()`` rebuild the session
            immediately with no backoff, no park, and no tool deregistration.

            Re-raise (rather than mask) when the failure is not a transient drop:
            - shutdown is in progress (``shutdown()`` sets ``_shutdown_event``
              before it ever cancels the task);
            - the group carries a ``KeyboardInterrupt`` / ``SystemExit`` — fatal
              signals must propagate to the interpreter, never be converted into
              a reconnect;
            - the group carries a real ``CancelledError`` (task cancellation must
              propagate to asyncio, mirroring the ``run()`` guard for #9930);
            - we never reached a live session this attempt (``_ready`` unset) — a
              connect/handshake failure SHOULD fall through to ``run()``'s backoff
              rather than hot-loop reconnects against a broken endpoint.
            """
            if self._shutdown_event.is_set():
                raise eg
            fatal, _rest = eg.split((KeyboardInterrupt, SystemExit))
            if fatal is not None:
                raise eg
            cancelled, _rest = eg.split(asyncio.CancelledError)
            if cancelled is not None:
                raise eg
            if not self._ready.is_set():
                raise eg
            logger.debug(
                "MCP server '%s': transport TaskGroup exited after a live session "
                "(%r) — reconnecting immediately instead of backing off",
                self.name, eg,
            )
            return "reconnect"

    async def _run_http(self, config: dict):
            """Run the server using HTTP/StreamableHTTP transport."""
            _ensure_mcp_sdk()
            if not _MCP_HTTP_AVAILABLE:
                raise ImportError(
                    f"MCP server '{self.name}' requires HTTP transport but "
                    "mcp.client.streamable_http is not available. "
                    "Upgrade the mcp package to get HTTP support."
                )

            url = config["url"]
            headers = dict(config.get("headers") or {})
            # Portable Agent Plugins v1 packages set strict_redirect_headers:
            # configured headers are visible package data and MUST NOT be
            # forwarded to a different origin through a redirect (spec §7.2.1).
            # Capture the configured header names before client-generated
            # headers (identity, protocol version) are merged in.
            _strict_cfg_headers = bool(config.get("strict_redirect_headers"))
            _configured_header_names = {key.lower() for key in headers}
            # Optional per-user identity header (config-gated; static or
            # profile-derived). Explicit headers of the same name win.
            headers = _apply_identity_header(self.name, config, headers)
            # Some MCP servers require MCP-Protocol-Version on the initial
            # initialize request and reject session-less POSTs otherwise.
            # Seed it as a client-level default, but treat user overrides as
            # case-insensitive so conventional casing is preserved.
            #
            # Seeded from the HANDSHAKE version, not the latest one: this transport
            # connects via `ClientSession.initialize()`, which sends
            # LATEST_HANDSHAKE_VERSION (2025-11-25) in the body. Advertising
            # 2026-07-28 in the header routes the request onto the server's
            # per-request-envelope ladder, which then rejects the legacy body for
            # missing its required `params._meta` envelope keys. The header has to
            # agree with what the body actually speaks.
            if not any(key.lower() == "mcp-protocol-version" for key in headers):
                headers["mcp-protocol-version"] = LATEST_HANDSHAKE_VERSION
            connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
            ssl_verify = config.get("ssl_verify", True)
            client_cert = _resolve_client_cert(self.name, config)

            # OAuth 2.1 PKCE: route through the central MCPOAuthManager so the
            # same provider instance is reused across reconnects, pre-flow
            # disk-watch is active, and config-time CLI code paths share state.
            # If OAuth setup fails (e.g. non-interactive env without cached
            # tokens), re-raise so this server is reported as failed without
            # blocking other MCP servers from connecting.
            _oauth_auth = None
            if self._auth_type == "oauth":
                try:
                    from tools.mcp_oauth_manager import get_manager
                    _oauth_auth = get_manager().get_or_build_provider(
                        self.name, url, config.get("oauth"),
                    )
                except Exception as exc:
                    logger.warning("MCP OAuth setup failed for '%s': %s", self.name, exc)
                    raise

            sampling_kwargs = self._sampling.session_kwargs() if self._sampling else {}
            if self._elicitation:
                sampling_kwargs.update(self._elicitation.session_kwargs())
            if _MCP_NOTIFICATION_TYPES and _MCP_MESSAGE_HANDLER_SUPPORTED:
                sampling_kwargs["message_handler"] = self._make_message_handler()
            if _MCP_LOGGING_CALLBACK_SUPPORTED:
                sampling_kwargs["logging_callback"] = self._make_logging_callback()

            # SSE transport (for MCP servers that implement the SSE transport protocol
            # rather than Streamable HTTP). Configure with ``transport: sse`` in the
            # mcp_servers entry in config.yaml.
            if config.get("transport") == "sse":
                if _strict_cfg_headers:
                    # Portable packages never translate to SSE; if a config
                    # combines both anyway, fail closed rather than run a
                    # transport that cannot enforce the redirect boundary.
                    raise ValueError(
                        f"MCP server '{self.name}': strict_redirect_headers is "
                        "not supported on the SSE transport."
                    )
                if sse_client is None:
                    raise ImportError(
                        f"MCP server '{self.name}' requires SSE transport but "
                        "mcp.client.sse.sse_client is not available. "
                        "Upgrade the mcp package to get SSE support."
                    )
                # sse_read_timeout governs how long sse_client will wait between
                # events on the SSE stream. Using the tool_timeout (default 60s)
                # here is wrong: SSE servers commonly hold the stream idle for
                # minutes between events, so a 60s read timeout drops the
                # connection after the first slow stretch. 300s matches the
                # Streamable HTTP code path's httpx read timeout below. Original
                # observation from @amiller in PR #5981 (Router Teamwork,
                # Supermemory on Cloudflare Workers idle-disconnect at ~60s).
                _sse_kwargs: dict = {
                    "url": url,
                    "headers": headers or None,
                    "timeout": float(connect_timeout),
                    "sse_read_timeout": 300.0,
                }
                if _oauth_auth is not None:
                    # Pass OAuth auth through to sse_client so SSE MCP servers
                    # behind OAuth 2.1 PKCE work. Previously built but never
                    # forwarded — SSE OAuth would silently fail with 401s.
                    _sse_kwargs["auth"] = _oauth_auth
                if client_cert is not None or ssl_verify is not True:
                    # SSE transport doesn't expose verify/cert as kwargs, so route
                    # them through an httpx_client_factory that wraps the SDK's
                    # defaults (follow_redirects=True) and adds our TLS settings.
                    # The SDK calls the factory with (headers, auth, timeout); we
                    # forward all of those and layer verify/cert on top.
                    # The client MUST come from the SDK's own httpx module
                    # (httpx2 on mcp >= 2.0) — see sdk_httpx().
                    _httpx_mod = sdk_httpx()

                    _cert_for_factory = client_cert
                    _verify_for_factory = ssl_verify

                    def _mcp_http_client_factory(
                        headers=None, timeout=None, auth=None,
                    ):
                        kwargs: dict = {
                            "follow_redirects": True,
                            "verify": _verify_for_factory,
                        }
                        if timeout is not None:
                            kwargs["timeout"] = timeout
                        else:
                            kwargs["timeout"] = _httpx_mod.Timeout(30.0, read=300.0)
                        if headers is not None:
                            kwargs["headers"] = headers
                        if auth is not None:
                            kwargs["auth"] = auth
                        if _cert_for_factory is not None:
                            kwargs["cert"] = _cert_for_factory
                        return _httpx_mod.AsyncClient(**kwargs)

                    _sse_kwargs["httpx_client_factory"] = _mcp_http_client_factory
                try:
                    async with sse_client(**_sse_kwargs) as (read_stream, write_stream):
                        async with ClientSession(
                            read_stream, write_stream, **sampling_kwargs
                        ) as session:
                            # Bound the handshake — same orphaned-task hang as the
                            # stdio path (#59349): an endpoint that accepts the
                            # connection but never answers ``initialize`` parks this
                            # coroutine forever on the background loop.
                            self.initialize_result = await self._negotiate_session(
                                session, float(connect_timeout)
                            )
                            self.session = session
                            await self._discover_tools()
                            self._ready.set()
                            self._ever_connected = True
                            # Session is live again: clear any breaker state from a
                            # prior outage so the first call after recovery isn't
                            # gated on a stale consecutive-failure count (#16788).
                            _reset_server_error(self.name)
                            # Unproven until keepalive/tool-call success (#62212).
                            self._session_proven = False
                            reason = await self._wait_for_lifecycle_event()
                            if reason == "reconnect":
                                logger.info(
                                    "MCP server '%s': reconnect requested — "
                                    "tearing down SSE session", self.name,
                                )
                except BaseExceptionGroup as _eg:
                    # SSE transport TaskGroup dropped (idle timeout / stream blip):
                    # reconnect immediately instead of backoff/park (#66092).
                    reason = self._reconnect_or_reraise_group(_eg)
                return reason

            if _MCP_NEW_HTTP:
                # New API (mcp >= 1.24.0): build an explicit AsyncClient matching
                # the SDK's own create_mcp_http_client defaults. It has to come
                # from the SDK's httpx module (httpx2 on mcp >= 2.0), because the
                # SDK sends its own Request objects through this client — see
                # sdk_httpx().
                httpx = sdk_httpx()

                _original_url = httpx.URL(url)

                _strip_auth_on_cross_origin_redirect = _make_redirect_header_stripper(
                    _original_url,
                    strict=_strict_cfg_headers,
                    configured_header_names=_configured_header_names,
                )

                client_kwargs: dict = {
                    "follow_redirects": True,
                    "timeout": httpx.Timeout(float(connect_timeout), read=300.0),
                    "verify": ssl_verify,
                    "event_hooks": {"response": [_strip_auth_on_cross_origin_redirect]},
                }
                if headers:
                    client_kwargs["headers"] = headers
                if _oauth_auth is not None:
                    client_kwargs["auth"] = _oauth_auth
                if client_cert is not None:
                    client_kwargs["cert"] = client_cert

                # Caller owns the client lifecycle — the SDK skips cleanup when
                # http_client is provided, so we wrap in async-with.
                try:
                    async with httpx.AsyncClient(**client_kwargs) as http_client:
                        # Unpacked positionally rather than by fixed arity: mcp
                        # 1.x yields (read, write, get_session_id) and 2.x yields
                        # (read, write). This file supports both SDK generations,
                        # and get_session_id was never used here.
                        async with streamable_http_client(url, http_client=http_client) as _streams:
                            read_stream, write_stream = _streams[0], _streams[1]
                            async with ClientSession(read_stream, write_stream, **sampling_kwargs) as session:
                                # Bound the handshake (#59349) — see stdio path.
                                self.initialize_result = await self._negotiate_session(
                                    session, float(connect_timeout)
                                )
                                self.session = session
                                await self._discover_tools()
                                self._ready.set()
                                self._ever_connected = True
                                # Session is live again: clear any breaker state from
                                # a prior outage so the first call after recovery
                                # isn't gated on a stale failure count (#16788).
                                _reset_server_error(self.name)
                                # Unproven until keepalive/tool-call success (#62212).
                                self._session_proven = False
                                reason = await self._wait_for_lifecycle_event()
                                if reason == "reconnect":
                                    logger.info(
                                        "MCP server '%s': reconnect requested — "
                                        "tearing down HTTP session", self.name,
                                    )
                except BaseExceptionGroup as _eg:
                    # Streamable-HTTP transport TaskGroup dropped: reconnect
                    # immediately instead of backoff/park (#66092).
                    reason = self._reconnect_or_reraise_group(_eg)
                return reason
            else:
                # Deprecated API (mcp < 1.24.0): manages httpx client internally.
                if _strict_cfg_headers:
                    # Fail closed: without an owned httpx client we cannot hook
                    # redirects, so the v1 cross-origin header boundary cannot be
                    # enforced on this SDK version.
                    raise ImportError(
                        f"MCP server '{self.name}' requires mcp >= 1.24.0 to "
                        "enforce the portable redirect-header boundary "
                        "(strict_redirect_headers). Upgrade the mcp package."
                    )
                _http_kwargs: dict = {
                    "headers": headers,
                    "timeout": float(connect_timeout),
                    "verify": ssl_verify,
                }
                if _oauth_auth is not None:
                    _http_kwargs["auth"] = _oauth_auth
                try:
                    async with streamablehttp_client(url, **_http_kwargs) as (
                        read_stream, write_stream, _get_session_id,
                    ):
                        async with ClientSession(read_stream, write_stream, **sampling_kwargs) as session:
                            # Bound the handshake (#59349) — see stdio path.
                            self.initialize_result = await self._negotiate_session(
                                session, float(connect_timeout)
                            )
                            self.session = session
                            await self._discover_tools()
                            self._ready.set()
                            self._ever_connected = True
                            # Session is live again: clear any breaker state from a
                            # prior outage so the first call after recovery isn't
                            # gated on a stale consecutive-failure count (#16788).
                            _reset_server_error(self.name)
                            # Unproven until keepalive/tool-call success (#62212).
                            self._session_proven = False
                            reason = await self._wait_for_lifecycle_event()
                            if reason == "reconnect":
                                logger.info(
                                    "MCP server '%s': reconnect requested — "
                                    "tearing down legacy HTTP session", self.name,
                                )
                except BaseExceptionGroup as _eg:
                    # Legacy Streamable-HTTP transport TaskGroup dropped: reconnect
                    # immediately instead of backoff/park (#66092).
                    reason = self._reconnect_or_reraise_group(_eg)
                return reason

    async def _discover_tools(self):
            """Discover tools from the connected session.

            Capability-gated: prompt-only / resource-only MCP servers don't
            implement ``tools/list``, and calling it raises ``MCPError(-32601)``,
            which previously aborted the connection — those servers could never
            stay connected for their prompts/resources. Skip the call when the
            server doesn't advertise the ``tools`` capability.
            (Ported from anomalyco/opencode#31271.)
            """
            # Fresh transport connection → re-probe with the cheap ``ping`` path.
            # Clears any latch from a prior connection in case the server gained
            # ping support across the reconnect.
            self._ping_unsupported = False
            if self.session is None:
                return
            if not self._advertises_tools():
                logger.info(
                    "MCP server '%s': does not advertise 'tools' capability — "
                    "skipping tools/list (prompts/resources remain available)",
                    self.name,
                )
                self._tools = []
                self._register_discovered_tools_if_needed()
                return
            async with self._rpc_lock:
                self._list_cache_meta = {}
                self._tools = await _paginate_full_list(
                    self.session.list_tools, "tools", self.name,
                    cache_meta_out=self._list_cache_meta,
                )
            self._register_discovered_tools_if_needed()

    def _register_discovered_tools_if_needed(self) -> None:
            """Re-register tools after an owned server reconnects if needed.

            Initial registration is performed by ``_discover_and_register_server``
            after ``start()`` completes. During a later reconnect, outage handling
            may clear ``_ready`` before discovery and may deregister stale tools.
            A managed server can still be identified by its entry in ``_servers``;
            publish its freshly discovered tools before transport readiness is
            restored so a successful revival cannot come back with zero tools.
            A server retained after a recoverable initial failure is likewise
            registry-owned before its first successful session, so ownership also
            authorizes its first publication.
            """
            if self._registered_tool_names:
                return
            if not self._ready.is_set():
                with _lock:
                    if _servers.get(self.name) is not self:
                        return
            self._registered_tool_names = _register_server_tools(
                self.name, self, self._config
            )
            # A retained initial-failure server that just published tools has
            # recovered: drop its stale connect error so status surfaces stop
            # reporting it as failed.
            with _lock:
                if _servers.get(self.name) is self:
                    _server_connect_errors.pop(self.name, None)

    async def run(self, config: dict):
            """Long-lived coroutine: connect, discover tools, wait, disconnect.

            Includes automatic reconnection with exponential backoff if the
            connection drops unexpectedly (unless shutdown was requested).
            """
            self._config = config
            self.tool_timeout = _resolve_tool_timeout(config)
            self._auth_type = (config.get("auth") or "").lower().strip()
            self._idle_timeout_seconds = _get_lifecycle_seconds(config, "idle_timeout_seconds")
            self._max_lifetime_seconds = _get_lifecycle_seconds(config, "max_lifetime_seconds")

            # Bind the lazily-imported SDK before reading feature flags below
            # (_MCP_SAMPLING_TYPES / _MCP_ELICITATION_TYPES are False until the
            # SDK import actually runs).
            _ensure_mcp_sdk()

            # Set up sampling handler if enabled and SDK types are available
            sampling_config = config.get("sampling", {})
            if sampling_config.get("enabled", True) and _MCP_SAMPLING_TYPES:
                self._sampling = SamplingHandler(self.name, sampling_config)
            else:
                self._sampling = None

            # Set up elicitation handler if enabled and SDK types are available.
            # Servers use elicitation/create to ask the client for structured
            # input mid-tool-call (e.g. payment authorization). The handler
            # routes those requests through Hermes' approval system.
            elicitation_config = config.get("elicitation", {})
            if elicitation_config.get("enabled", True) and _MCP_ELICITATION_TYPES:
                self._elicitation = ElicitationHandler(self.name, elicitation_config, owner=self)
            else:
                self._elicitation = None

            # Validate: warn if both url and command are present
            if "url" in config and "command" in config:
                logger.warning(
                    "MCP server '%s' has both 'url' and 'command' in config. "
                    "Using HTTP transport ('url'). Remove 'command' to silence "
                    "this warning.",
                    self.name,
                )

            # Validate remote URL once, up front.  Raising here (rather than
            # letting it blow up inside the SDK's httpx layer on every retry)
            # means a typo in config.yaml fails fast with a clear error — and
            # critically, no reconnect-backoff burn.  (Ported from
            # anomalyco/opencode#25019.)
            if self._is_http():
                try:
                    _validate_remote_mcp_url(self.name, config.get("url"))
                except InvalidMcpUrlError as exc:
                    logger.warning("%s", exc)
                    self._error = exc
                    self._ready.set()
                    return

                # Pre-flight content-type probe (Streamable HTTP only; SSE is
                # exercised by its own client and legitimately serves
                # text/event-stream). A URL pointed at a web-app root returns
                # HTML, which makes the SDK hang for the full connect_timeout
                # before surfacing an opaque CancelledError. Probing here — once,
                # outside the SDK task group — fails fast and non-retryably with
                # an actionable message, mirroring the URL-validation path above.
                # Skip the probe when _ready is already set (reconnect after a
                # prior successful connect) — the endpoint was validated once,
                # re-probing is a redundant round-trip. Also skip for OAuth servers:
                # without a cached token the endpoint returns HTML or 401, which
                # would incorrectly block the OAuth flow before it can run.
                if config.get("transport") != "sse" and not config.get("skip_preflight") and not self._ready.is_set() and self._auth_type != "oauth":
                    try:
                        _probe_headers = dict(config.get("headers") or {})
                        await self._preflight_content_type(
                            config["url"],
                            headers=_probe_headers,
                            ssl_verify=config.get("ssl_verify", True),
                            client_cert=_resolve_client_cert(self.name, config),
                        )
                    except NonMcpEndpointError as exc:
                        logger.warning("%s", exc)
                        self._error = exc
                        self._ready.set()
                        return

            self._reconnect_retries = 0
            initial_retries = 0
            backoff = 1.0

            while True:
                try:
                    if self._is_http():
                        lifecycle_reason = await self._run_http(config)
                    else:
                        lifecycle_reason = await self._run_stdio(config)
                    # Transport returned cleanly. Two cases:
                    #  - _shutdown_event was set: exit the run loop entirely.
                    #  - _reconnect_event was set (auth recovery): loop back and
                    #    rebuild the MCP session with fresh credentials. Do NOT
                    #    touch the retry counters — this is not a failure.
                    if self._shutdown_event.is_set():
                        break
                    if lifecycle_reason == "recycle":
                        logger.info(
                            "MCP server '%s': stdio session recycled after %s; "
                            "waiting for lazy reconnect",
                            self.name, self._recycled_reason,
                        )
                        self.session = None
                        await self._wait_for_lazy_reconnect()
                        if self._shutdown_event.is_set():
                            break
                        self._reconnect_event.clear()
                        continue
                    # Per-cycle reconnect chatter — DEBUG. In the flapping case
                    # this fires on every rebuild; the WARNINGs live on the
                    # state transitions.
                    logger.debug(
                        "MCP server '%s': reconnecting (OAuth recovery or "
                        "manual refresh)",
                        self.name,
                    )
                    # A clean transport return means a session was established and
                    # then asked to rebuild (auth recovery / manual refresh /
                    # keepalive failure / transport TaskGroup drop). That alone is
                    # NOT proof of health: a flapping transport handshakes fine and
                    # drops moments later, and resetting the budget here let such
                    # servers respawn forever (#62212 — 6212 spawns in 63h).
                    # Only clear the consecutive-failure budget once the session
                    # PROVED healthy — survived >=1 full keepalive interval or
                    # served >=1 successful tool call (_mark_session_proven).
                    if self._teardown_race and not self._session_proven:
                        # The previous cycle ended because a teardown cancelled
                        # in-flight calls (keepalive/refresh race, auth recovery)
                        # — that is RECOVERY, not a transport failure. Do NOT
                        # charge the rapid-drop budget: a single race must never
                        # reach the park (#81051/#77765/#84132). Only genuinely
                        # repeated unproven drops still exhaust the budget below.
                        logger.info(
                            "MCP server '%s': reconnect after teardown race "
                            "(in-flight calls were failed); not charging the "
                            "rapid-drop budget",
                            self.name,
                        )
                        self._teardown_race = False
                        backoff = 1.0
                    elif self._session_proven:
                        self._reconnect_retries = 0
                        backoff = 1.0
                    else:
                        # Unproven session: charge the rapid-drop budget so a
                        # flapping transport still reaches the park.
                        self._reconnect_retries += 1
                        if self._reconnect_retries > _MAX_RECONNECT_RETRIES:
                            logger.warning(
                                "MCP server '%s': %d consecutive reconnects "
                                "without a healthy session (rapid-drop budget "
                                "exhausted), parking; will self-probe every %ds "
                                "until it recovers (state: degraded → parked)",
                                self.name, _MAX_RECONNECT_RETRIES,
                                _PARKED_RETRY_INTERVAL,
                            )
                            self._was_parked = True
                            self._deregister_tools()
                            self._reconnect_event.clear()
                            parked = await self._wait_for_reconnect_or_shutdown(
                                timeout=_PARKED_RETRY_INTERVAL
                            )
                            if parked == "shutdown":
                                break
                            logger.debug(
                                "MCP server '%s': attempting revival from parked "
                                "state (self-probe or explicit reconnect request); "
                                "rebuilding transport.",
                                self.name,
                            )
                            # One probe attempt per wake — see the exception-path
                            # park below.
                            self._reconnect_retries = _MAX_RECONNECT_RETRIES
                            backoff = 1.0
                    # Reset the session reference and readiness; _run_http/_run_stdio
                    # will repopulate both on successful re-entry.  Leaving
                    # _ready set here lets handler-side recovery mistake the stale
                    # pre-reconnect session for a fresh one and retry too early.
                    self._ready.clear()
                    self.session = None
                    continue
                except asyncio.CancelledError:
                    # Task was cancelled (shutdown, gateway restart, explicit
                    # task.cancel()). Don't treat this as a connection failure —
                    # CancelledError inherits from BaseException (not Exception)
                    # in Python 3.11+, so the broad ``except Exception`` below
                    # would NOT catch it; we'd silently exit the reconnect loop
                    # and the MCP server would stay dead until Hermes is fully
                    # restarted. Re-raise so the task's cancellation propagates
                    # correctly to asyncio's task machinery and ``shutdown()``'s
                    # ``await self._task`` completes. See #9930.
                    self.session = None
                    raise
                except Exception as exc:
                    self.session = None
                    # Unwrap anyio TaskGroup wrappers first: str(exc) on a
                    # BaseExceptionGroup is "unhandled errors in a TaskGroup
                    # (N sub-exceptions)" — useless in logs, and it hides the
                    # root cause from the auth/permanence classification below.
                    # Empty dead-pipe errors still get a name this way
                    # (e.g. "BrokenPipeError: ").
                    root = _unwrap_exception_group(exc)
                    failure_class = _classify_mcp_failure(root)
                    if self._is_recycled_stdio():
                        logger.warning(
                            "MCP server '%s': lazy reconnect after stdio recycle "
                            "failed, marking unavailable while retrying: %s: %s",
                            self.name, type(root).__name__, root,
                        )
                        self._recycled_reason = None

                    # If this is the first connection attempt, retry with backoff
                    # before giving up. A transient DNS/network blip at startup
                    # should not permanently kill the server. Gated on
                    # ``_ever_connected`` rather than ``_ready`` — ``_ready`` is
                    # cleared on every reconnect cycle (see below), so a server
                    # that already registered tools once and then dropped would
                    # otherwise be misclassified as never having connected and
                    # re-enter this initial-connect ladder (#94654).
                    # ``_ever_connected`` itself is set once and never cleared.
                    # (Ported from Kilo Code's MCP resilience fix.)
                    if not self._ever_connected:
                        if failure_class == "permanent":
                            # Deterministic failure (bad command, non-MCP URL,
                            # 401/403): every retry hits the same wall. Park
                            # immediately instead of burning the retry ladder
                            # and spamming N identical warnings (#65673).
                            #
                            # Auth failures park here too rather than returning.
                            # Returning ends the run task, and with it the only
                            # listener on ``_reconnect_event`` — so a 401 on the
                            # very first connect left the server unrevivable for
                            # the life of the process, even after the user
                            # re-authenticated with ``hermes mcp login``. Parking
                            # keeps the task alive so the 300s self-probe (and an
                            # explicit /mcp refresh) can pick up fresh tokens.
                            if _is_auth_error(root):
                                logger.warning(
                                    "MCP server '%s' failed initial authentication, "
                                    "parking until credentials change; re-authenticate "
                                    "with `hermes mcp login %s` "
                                    "(state: connecting → parked): %s: %s",
                                    self.name, self.name,
                                    type(root).__name__, root,
                                )
                            else:
                                logger.warning(
                                    "MCP server '%s' failed initial connection with a "
                                    "permanent error, parking without retries "
                                    "(state: connecting → parked): %s: %s",
                                    self.name, type(root).__name__, root,
                                )
                            self._error = exc
                            self._ready.set()
                            self._was_parked = True
                            self._deregister_tools()
                            self._reconnect_event.clear()
                            parked = await self._wait_for_reconnect_or_shutdown(
                                timeout=_PARKED_RETRY_INTERVAL
                            )
                            if parked == "shutdown":
                                return
                            logger.debug(
                                "MCP server '%s': attempting revival after "
                                "permanent initial failure (self-probe or explicit "
                                "reconnect request); rebuilding transport.",
                                self.name,
                            )
                            initial_retries = 0
                            self._reconnect_retries = 0
                            backoff = 1.0
                            self._error = None
                            self._ready.clear()
                            continue

                        initial_retries += 1
                        if initial_retries > _MAX_INITIAL_CONNECT_RETRIES:
                            logger.warning(
                                "MCP server '%s' failed initial connection after "
                                "%d attempts, parking until a reconnect is "
                                "requested (state: connecting → parked): %s: %s",
                                self.name, _MAX_INITIAL_CONNECT_RETRIES,
                                type(root).__name__, root,
                            )
                            self._error = exc
                            self._ready.set()
                            self._was_parked = True
                            self._deregister_tools()
                            self._reconnect_event.clear()
                            parked = await self._wait_for_reconnect_or_shutdown(
                                timeout=_PARKED_RETRY_INTERVAL
                            )
                            if parked == "shutdown":
                                return
                            logger.debug(
                                "MCP server '%s': attempting revival after initial "
                                "connection failures (self-probe or explicit "
                                "reconnect request); rebuilding transport.",
                                self.name,
                            )
                            initial_retries = 0
                            self._reconnect_retries = 0
                            backoff = 1.0
                            self._error = None
                            self._ready.clear()
                            continue

                        logger.debug(
                            "MCP server '%s' initial connection failed "
                            "(attempt %d/%d), retrying in %.0fs: %s: %s",
                            self.name, initial_retries,
                            _MAX_INITIAL_CONNECT_RETRIES, backoff,
                            type(root).__name__, root,
                        )
                        await asyncio.sleep(_jittered(backoff))
                        backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                        # Check if shutdown was requested during the sleep
                        if self._shutdown_event.is_set():
                            self._error = exc
                            self._ready.set()
                            return
                        continue

                    # If shutdown was requested, don't reconnect
                    if self._shutdown_event.is_set():
                        logger.debug(
                            "MCP server '%s' disconnected during shutdown: %s: %s",
                            self.name, type(root).__name__, root,
                        )
                        return

                    if failure_class == "permanent":
                        # Auth-lock corruption guard (#81051/#77765/#84132): an
                        # auth-classified permanent failure on a previously
                        # PROVEN session is often a transient/ambiguous state
                        # (OAuth flow lock left corrupt by a raced teardown),
                        # not truly revoked credentials. Grant ONE
                        # suspect+reconnect cycle before the park ladder: mark
                        # the connection suspect so the next call health-checks
                        # it, and rebuild the transport instead of parking.
                        if (
                            _is_auth_error(root)
                            and self._session_proven
                            and not self._permanent_grace_used
                        ):
                            self._permanent_grace_used = True
                            self.mark_suspect(
                                f"auth error on proven session: {root}"
                            )
                            logger.warning(
                                "MCP server '%s': auth error on a previously "
                                "healthy session — marking suspect and forcing "
                                "one reconnect instead of parking (state: "
                                "connected → suspect): %s: %s",
                                self.name, type(root).__name__, root,
                            )
                            self._reconnect_retries = 0
                            backoff = 1.0
                            await asyncio.sleep(_jittered(1.0))
                            if self._shutdown_event.is_set():
                                return
                            continue
                        # A previously-working server now fails deterministically
                        # (revoked credentials, URL now serving a web page, stdio
                        # binary uninstalled). Retrying can't help — park
                        # immediately without burning the retry ladder.
                        logger.warning(
                            "MCP server '%s' hit a permanent error, parking "
                            "without retries; will self-probe every %ds "
                            "(state: connected → parked): %s: %s",
                            self.name, _PARKED_RETRY_INTERVAL,
                            type(root).__name__, root,
                        )
                        self._was_parked = True
                        self._deregister_tools()
                        self._reconnect_event.clear()
                        parked = await self._wait_for_reconnect_or_shutdown(
                            timeout=_PARKED_RETRY_INTERVAL
                        )
                        if parked == "shutdown":
                            return
                        logger.debug(
                            "MCP server '%s': attempting revival from parked state "
                            "(permanent error; self-probe or explicit reconnect "
                            "request); rebuilding transport.",
                            self.name,
                        )
                        self._reconnect_retries = _MAX_RECONNECT_RETRIES
                        backoff = 1.0
                        continue

                    self._reconnect_retries += 1
                    if self._reconnect_retries > _MAX_RECONNECT_RETRIES:
                        logger.warning(
                            "MCP server '%s' failed after %d reconnection attempts, "
                            "parking; will self-probe every %ds until it recovers "
                            "(state: degraded → parked): %s: %s",
                            self.name, _MAX_RECONNECT_RETRIES,
                            _PARKED_RETRY_INTERVAL,
                            type(root).__name__, root,
                        )
                        # Do NOT return — exiting the task orphans the server:
                        # nothing would ever listen for _reconnect_event again
                        # and the server would be permanently wedged for the
                        # life of the process (#16788). Instead, drop the phantom
                        # tools from the registry and park. Because parking
                        # deregisters the tools, no tool call can reach the
                        # circuit-breaker half-open probe or _signal_reconnect —
                        # so the park is a TIMED wait: every _PARKED_RETRY_INTERVAL
                        # we wake and attempt one reconnect ourselves (#57129).
                        # An explicit _reconnect_event.set() (OAuth recovery,
                        # manual /mcp refresh) still wakes us immediately.
                        self._was_parked = True
                        self._deregister_tools()
                        self._reconnect_event.clear()
                        parked = await self._wait_for_reconnect_or_shutdown(
                            timeout=_PARKED_RETRY_INTERVAL
                        )
                        if parked == "shutdown":
                            return
                        logger.debug(
                            "MCP server '%s': attempting revival from parked state "
                            "(self-probe or explicit reconnect request); "
                            "rebuilding transport.",
                            self.name,
                        )
                        # One probe attempt per wake: budget of 1 so a still-dead
                        # server parks again for another interval instead of
                        # burning 5 rapid retries each cycle.
                        self._reconnect_retries = _MAX_RECONNECT_RETRIES
                        backoff = 1.0
                        continue

                    # Per-attempt retry chatter stays at DEBUG; state transitions
                    # (connected->degraded, degraded->parked, parked->revived)
                    # carry the WARNINGs — one line per transition, not per try.
                    logger.debug(
                        "MCP server '%s' connection lost (attempt %d/%d), "
                        "reconnecting in %.0fs: %s: %s",
                        self.name, self._reconnect_retries, _MAX_RECONNECT_RETRIES,
                        backoff, type(root).__name__, root,
                    )
                    await asyncio.sleep(_jittered(backoff))
                    backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                    # Check again after sleeping
                    if self._shutdown_event.is_set():
                        return
                finally:
                    self.session = None
                    # Children of this transport are gone (or about to be);
                    # stale PIDs must never fast-fail the NEXT transport's calls.
                    self._stdio_child_pids = set()

    async def start(self, config: dict):
            """Create the background Task and wait until ready (or failed)."""
            self._task = asyncio.ensure_future(self.run(config))
            try:
                await self._ready.wait()
            except asyncio.CancelledError:
                # The caller's connect timeout (discover_mcp_tools wraps start()
                # in asyncio.wait_for) cancels *this* coroutine, but the
                # ensure_future'd run() task is independent and would otherwise
                # keep running detached — parked on a hung transport with no
                # owner to reap it (#59349). Propagate the cancellation so the
                # transport context managers unwind and their finally blocks
                # release the child process / FDs.
                if self._task and not self._task.done():
                    self._task.cancel()
                raise
            if self._error:
                raise self._error

    async def shutdown(self):
            """Signal the Task to exit and wait for clean resource teardown."""
            self._shutdown_event.set()
            # Defensive: if _wait_for_lifecycle_event is blocking, we need ANY
            # event to unblock it. _shutdown_event alone is sufficient (the
            # helper checks shutdown first), but setting reconnect too ensures
            # there's no race where the helper misses the shutdown flag after
            # returning "reconnect".
            self._reconnect_event.set()
            if self._task and not self._task.done():
                try:
                    await asyncio.wait_for(self._task, timeout=10)
                except asyncio.TimeoutError:
                    logger.warning(
                        "MCP server '%s' shutdown timed out, cancelling task",
                        self.name,
                    )
                    self._task.cancel()
                    try:
                        await self._task
                    except asyncio.CancelledError:
                        pass
            if self._pending_refresh_tasks:
                for task in list(self._pending_refresh_tasks):
                    task.cancel()
                await asyncio.gather(*self._pending_refresh_tasks, return_exceptions=True)
                self._pending_refresh_tasks.clear()
            self._deregister_tools()
            self.session = None

    def _deregister_tools(self) -> None:
            """Drop this server's tools from the global registry (idempotent).

            Pulls the server's tool schemas out of the registry so the agent
            stops advertising them to the model. Called on shutdown AND when the
            reconnect budget is exhausted, so a dead server never leaves phantom
            tool definitions bloating the prompt cache and producing "not
            connected" errors on every turn.
            """
            from tools.registry import registry

            for tool_name in list(getattr(self, "_registered_tool_names", [])):
                registry.deregister(tool_name)
                _forget_mcp_tool_server(tool_name)
            self._registered_tool_names = []

    async def _wait_for_lazy_reconnect(self) -> None:
            """Wait while an intentionally recycled stdio server is dormant."""
            shutdown_task = asyncio.create_task(self._shutdown_event.wait())
            reconnect_task = asyncio.create_task(self._reconnect_event.wait())
            try:
                await asyncio.wait(
                    {shutdown_task, reconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (shutdown_task, reconnect_task):
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass

class MCPServerTask(_MCPServerTaskLifecycleMixin, _MCPServerTaskTransportMixin):
    """Manages a single MCP server connection in a dedicated asyncio Task.

        The entire connection lifecycle (connect, discover, serve, disconnect)
        runs inside one asyncio Task so that anyio cancel-scopes created by
        the transport client are entered and exited in the same Task context.

        Supports both stdio and HTTP/StreamableHTTP transports.
        """

    __slots__ = (
            "name", "session", "tool_timeout",
            "_task", "_ready", "_shutdown_event", "_reconnect_event",
            "_tools", "_error", "_config",
            "_sampling", "_elicitation",
            "_registered_tool_names", "_auth_type", "_refresh_lock",
            "_rpc_lock", "_pending_refresh_tasks",
            "_pending_call_context",
            "_lifecycle_started_at", "_last_tool_call_at",
            "_idle_timeout_seconds", "_max_lifetime_seconds", "_recycled_reason",
            "initialize_result", "_ping_unsupported", "_list_cache_meta",
            "_reconnect_retries", "_session_proven", "_was_parked",
            "_inflight_tasks", "_reconnecting", "_suspect_reason",
            "_teardown_race", "_permanent_grace_used", "_stdio_child_pids",
            "_ever_connected",
        )
    # Content types accepted from a real MCP Streamable-HTTP endpoint.
    _MCP_CONTENT_TYPES = ("application/json", "text/event-stream")



# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_servers: Dict[str, MCPServerTask] = {}
_server_connecting: set[str] = set()
_server_connect_errors: Dict[str, str] = {}
# Lazy MCP startup (#56832): servers whose tools were registered from the
# on-disk schema cache without spawning/connecting. Keyed by server name;
# entries are popped once a real connection is established on first use.
_lazy_server_configs: Dict[str, dict] = {}
_lazy_server_fingerprints: Dict[str, str] = {}
_lazy_server_tool_names: Dict[str, List[str]] = {}
# Discovery installs a task-local claim before calling ``_connect_server`` so
# it can retain a recoverable parked task without making standalone probe calls
# publish failed servers into module-global ownership.
_connect_server_claim: contextvars.ContextVar[
    Optional[Callable[[MCPServerTask], None]]
] = contextvars.ContextVar("mcp_connect_server_claim", default=None)

# Connection-retry cooldown (per-server isolation against restart storms).
#
# A single stdio MCP server that fails to spawn (bad PATH, ``exec: not
# found``, crash-on-start) is never recorded in ``_servers`` -- ``start()``
# raises and ``_discover_and_register_server`` aborts before the
# ``_servers[name] = server`` line. Without a cooldown, EVERY subsequent
# ``discover_mcp_tools()`` (one per agent worker session, i.e. every few
# seconds) sees the server as "not connected" and re-spawns it from
# scratch. That is the restart storm in #50394: the failing server is
# re-attempted on the shared MCP event loop on every worker session, the
# subprocesses pile up unreaped, and the churn destabilises the healthy
# co-located servers (their tools intermittently surface as
# "Unknown tool").
#
# Fix: after a failed connection attempt, stamp a monotonic
# ``retry_after`` deadline with exponential backoff. ``register_mcp_servers``
# skips a server whose cooldown has not elapsed, so a chronically failing
# server is retried on a backoff schedule instead of on every worker
# session -- isolating it from the rest of the bridge. A successful
# connection clears the state.
_server_connect_retry_after: Dict[str, float] = {}   # name -> monotonic deadline
_server_connect_failures: Dict[str, int] = {}        # name -> consecutive failures
_CONNECT_RETRY_BASE_BACKOFF_SEC = 30.0
_CONNECT_RETRY_MAX_BACKOFF_SEC = 600.0


def _record_connect_failure(server_name: str) -> None:
    """Stamp an exponential-backoff cooldown after a failed connect.

    Called (under ``_lock``) when a server fails its discovery/connect
    attempt. The cooldown grows geometrically with the consecutive
    failure count and is capped at :data:`_CONNECT_RETRY_MAX_BACKOFF_SEC`,
    so a permanently-broken server settles into infrequent retries
    rather than a tight respawn loop.
    """
    n = _server_connect_failures.get(server_name, 0) + 1
    _server_connect_failures[server_name] = n
    backoff = min(
        _CONNECT_RETRY_BASE_BACKOFF_SEC * (2 ** (n - 1)),
        _CONNECT_RETRY_MAX_BACKOFF_SEC,
    )
    _server_connect_retry_after[server_name] = time.monotonic() + backoff


def _clear_connect_failure(server_name: str) -> None:
    """Clear the connect-cooldown state after a successful connection."""
    _server_connect_failures.pop(server_name, None)
    _server_connect_retry_after.pop(server_name, None)


def _connect_cooldown_active(server_name: str) -> bool:
    """Return True if ``server_name`` is still within its retry cooldown."""
    deadline = _server_connect_retry_after.get(server_name)
    return deadline is not None and time.monotonic() < deadline

# Circuit breaker: consecutive error counts per server.  After
# _CIRCUIT_BREAKER_THRESHOLD consecutive failures, the handler returns
# a "server unreachable" message that tells the model to stop retrying,
# preventing the 90-iteration burn loop described in #10447.
#
# State machine:
#   closed    — error count below threshold; all calls go through.
#   open      — threshold reached; calls short-circuit until the
#               cooldown elapses.
#   half-open — cooldown elapsed; the next call is a probe that
#               actually hits the session. Probe success → closed.
#               Probe failure → reopens (cooldown re-armed).
#
# ``_server_breaker_opened_at`` records the monotonic timestamp when
# the breaker most recently transitioned into the open state. Use the
# ``_bump_server_error`` / ``_reset_server_error`` helpers to mutate
# this state — they keep the count and timestamp in sync.
_server_error_counts: Dict[str, int] = {}
_server_breaker_opened_at: Dict[str, float] = {}
_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN_SEC = 60.0

# ---------------------------------------------------------------------------
# Trust-tier gating state (per-server trust + per-tool readOnlyHint).
#
# ``trust: full | untrusted`` is a per-server key in the MCP server config
# (config.yaml → mcp_servers.<name>.trust). On an ``untrusted`` server,
# every WRITE-CAPABLE tool call routes through the existing dangerous-
# approval surface before the RPC fires. A tool is write-capable unless its
# discovery-time ``annotations.readOnlyHint`` is exactly ``True``
# (missing/malformed annotations fail closed to write-capable).
#
# Security model (read this before changing defaults):
# - ``readOnlyHint`` is a HINT supplied by the server itself. A hostile
#   server can lie. That is precisely why the gate is tiered per-server by
#   OPERATOR config: on an untrusted server the hint can only ever exempt
#   tools the server claims are read-only — the worst a lie buys is
#   skipping approval for calls the operator was already warned about when
#   they marked the server untrusted. It can never widen access on top of
#   the approval a write-capable tool would otherwise need.
# - Default trust for servers with NO ``trust`` key is ``full`` (gate off)
#   for backward compatibility — existing configs keep working unchanged.
#   Operators opt servers into gating explicitly with ``trust: untrusted``.
# - Any unrecognized ``trust`` value normalizes to ``untrusted``
#   (fail closed): a typo must never silently disable the gate.
#
# Classification happens at CALL TIME from data captured at DISCOVERY —
# no toolset or schema mutation, so the conversation's toolset stays
# byte-stable and prompt caching is preserved.
_server_trust_levels: Dict[str, str] = {}
_tool_read_only_hints: Dict[str, Dict[str, bool]] = {}

_TRUST_FULL = "full"
_TRUST_UNTRUSTED = "untrusted"


def _normalize_server_trust(value: Any) -> str:
    """Normalize a config ``trust`` value to ``full`` or ``untrusted``.

    Missing (None) → ``full`` (backward-compatible default, documented
    above). Any string other than the two known tiers → ``untrusted``:
    a misspelled tier must fail closed, never silently disable gating.
    """
    if value is None:
        return _TRUST_FULL
    text = str(value).strip().lower()
    if text == _TRUST_FULL:
        return _TRUST_FULL
    if text == _TRUST_UNTRUSTED:
        return _TRUST_UNTRUSTED
    logger.warning(
        "MCP trust: unrecognized trust value %r — treating as 'untrusted' "
        "(valid values: full, untrusted)", value,
    )
    return _TRUST_UNTRUSTED


def _annotation_read_only_hint(mcp_tool: Any) -> bool:
    """Return True only when the tool's annotations carry readOnlyHint=True.

    Accepts both SDK annotation objects (attribute access) and plain dicts
    (schema-cache JSON). Anything else — missing annotations, missing key,
    non-bool truthy values — is False: unknown metadata means the tool must
    be treated as write-capable.
    """
    annotations = getattr(mcp_tool, "annotations", None)
    if annotations is None:
        return False
    if isinstance(annotations, dict):
        hint = annotations.get("readOnlyHint")
    else:
        hint = getattr(annotations, "readOnlyHint", None)
    return hint is True


def _record_tool_trust_metadata(
    server_name: str, config: dict, tools: List[Any]
) -> None:
    """Capture per-server trust and per-tool readOnlyHint at discovery."""
    with _lock:
        _server_trust_levels[server_name] = _normalize_server_trust(
            (config or {}).get("trust")
        )
        hints = _tool_read_only_hints.setdefault(server_name, {})
        for tool in tools:
            name = getattr(tool, "name", None)
            if name:
                hints[name] = _annotation_read_only_hint(tool)


def _trust_gate_check(server_name: str, tool_name: str) -> Optional[str]:
    """Consult the approval path for write-capable tools on untrusted servers.

    Returns None when the call may proceed, or an error string (already
    formatted via ``tool_error``) when the call is blocked. Fail-closed:
    approval-system errors block the call.
    """
    trust = _server_trust_levels.get(server_name, _TRUST_FULL)
    if trust != _TRUST_UNTRUSTED:
        return None
    if _tool_read_only_hints.get(server_name, {}).get(tool_name) is True:
        return None

    # Lazy import mirrors the elicitation handler's pattern: tools.approval
    # routes the prompt to whichever surface owns the session (CLI, TUI,
    # Telegram, Slack, ...) and normalizes the answer.
    try:
        from tools.approval import request_elicitation_consent

        answer = request_elicitation_consent(
            (
                f"MCP tool '{tool_name}' on UNTRUSTED server "
                f"'{server_name}' wants to run. This tool is write-capable "
                f"(no readOnlyHint=true annotation) and may modify external "
                f"state."
            ),
            (
                f"Server '{server_name}' is configured 'trust: untrusted'. "
                f"Approve to run '{tool_name}' once, or deny to block it."
            ),
            surface=f"mcp-trust/{server_name}",
        )
    except Exception as exc:
        logger.error(
            "MCP trust gate: approval check failed for %s.%s: %s",
            server_name, tool_name, exc, exc_info=True,
        )
        return tool_error(
            f"MCP tool '{tool_name}' on untrusted server '{server_name}' "
            f"was blocked: the approval system was unavailable "
            f"(fail-closed)."
        )

    if answer == "accept":
        return None
    logger.info(
        "MCP trust gate: user %s '%s' on untrusted server '%s'",
        "cancelled" if answer == "cancel" else "denied",
        tool_name, server_name,
    )
    return tool_error(
        f"The user did not approve running write-capable MCP tool "
        f"'{tool_name}' on untrusted server '{server_name}'. The command "
        f"was NOT run. Do not retry without explicit user direction."
    )


def _bump_server_error(server_name: str) -> None:
    """Increment the consecutive-failure count for ``server_name``.

    When the count crosses :data:`_CIRCUIT_BREAKER_THRESHOLD`, stamp the
    breaker-open timestamp so the cooldown clock starts (or re-starts,
    for probe failures in the half-open state).
    """
    n = _server_error_counts.get(server_name, 0) + 1
    _server_error_counts[server_name] = n
    if n >= _CIRCUIT_BREAKER_THRESHOLD:
        _server_breaker_opened_at[server_name] = time.monotonic()


def _reset_server_error(server_name: str) -> None:
    """Fully close the breaker for ``server_name``.

    Clears both the failure count and the breaker-open timestamp. Call
    this on any unambiguous success signal (successful tool call,
    successful reconnect, manual /mcp refresh).
    """
    _server_error_counts[server_name] = 0
    _server_breaker_opened_at.pop(server_name, None)


def _signal_reconnect(server: Any) -> bool:
    """Ask a server task to rebuild its transport, thread-safely.

    The tool handlers run on caller threads, while the server task and its
    ``_reconnect_event`` live on the background MCP loop. Setting an
    asyncio.Event from another thread must go through
    ``loop.call_soon_threadsafe``; non-async adapters and tests without a
    running loop can use a direct ``.set()``.

    Returns True if a reconnect signal was delivered, False if the server
    has no reconnect machinery (nothing to revive).
    """
    event = getattr(server, "_reconnect_event", None)
    if event is None:
        return False
    loop = _mcp_loop
    if (
        isinstance(event, asyncio.Event)
        and loop is not None
        and loop.is_running()
    ):
        loop.call_soon_threadsafe(event.set)
    else:
        event.set()
    return True


def reconnect_mcp_server(server_name: str) -> bool:
    """Ask a currently-live MCP server to rebuild after external re-auth."""
    with _lock:
        server = _servers.get(server_name)
    if server is None:
        return False
    return _signal_reconnect(server)


def _wait_for_server_session_ready(
    srv: "MCPServerTask",
    *,
    old_session: Any = None,
    timeout: float = 15.0,
) -> bool:
    """Wait for an MCP server to expose a usable session.

    Tool handlers run in normal worker threads while the MCP transport lives on
    the module's background asyncio loop. During a reconnect there is a short
    window where ``srv.session`` is ``None`` (or still points at the stale
    session until the lifecycle coroutine has left the transport context). A
    handler that blindly retries in that window can burn circuit-breaker strikes
    and return ``not connected`` even though the reconnect is already in
    progress.

    When ``old_session`` is supplied, require the observed session object to be
    different so callers do not mistake the pre-reconnect, stale session for a
    fresh one.
    """
    # Iteration-bounded rather than deadline-bounded: several tests (and the
    # circuit-breaker cooldown logic) monkeypatch time.monotonic to a frozen
    # clock, which would make a monotonic-deadline loop spin forever.
    poll_interval = 0.25
    iterations = max(1, int(max(float(timeout), 0.0) / poll_interval))
    for i in range(iterations):
        session = getattr(srv, "session", None)
        ready = getattr(srv, "_ready", None)
        is_ready = True
        if ready is not None and hasattr(ready, "is_set"):
            try:
                is_ready = bool(ready.is_set())
            except Exception:
                is_ready = True
        if session is not None and session is not old_session and is_ready:
            return True
        if i < iterations - 1:
            time.sleep(poll_interval)
    return False


def _signal_reconnect_and_wait(
    server_name: str,
    srv: "MCPServerTask",
    *,
    op_description: str,
    timeout: float = 15.0,
) -> bool:
    """Ask a live MCP server task to rebuild its transport session.

    The important detail is clearing ``_ready`` on the MCP event loop before
    setting ``_reconnect_event``. Older code left ``_ready`` set across
    reconnects, so the caller's readiness poll could return immediately and
    retry against the same dead HTTP/stream session. That was observed as
    repeated ``Session terminated`` / ``not connected`` / circuit-breaker
    failures in long-lived gateway sessions even though a fresh CLI process
    could connect successfully.
    """
    loop = _mcp_loop
    if loop is None or not loop.is_running():
        return False

    old_session = getattr(srv, "session", None)

    def _request_reconnect() -> None:
        ready = getattr(srv, "_ready", None)
        if ready is not None and hasattr(ready, "clear"):
            ready.clear()
        reconnect_event = getattr(srv, "_reconnect_event", None)
        if reconnect_event is not None and hasattr(reconnect_event, "set"):
            reconnect_event.set()

    logger.info(
        "MCP server '%s': %s requesting transport reconnect",
        server_name, op_description,
    )
    loop.call_soon_threadsafe(_request_reconnect)
    return _wait_for_server_session_ready(
        srv,
        old_session=old_session,
        timeout=timeout,
    )

# ---------------------------------------------------------------------------
# Auth-failure detection helpers (Task 6 of MCP OAuth consolidation)
# ---------------------------------------------------------------------------

# Cached tuple of auth-related exception types. Lazy so this module
# imports cleanly when the MCP SDK OAuth module is missing.
_AUTH_ERROR_TYPES: tuple = ()
_HTTP_STATUS_ERROR_TYPES: Optional[tuple] = None


def _http_status_error_types() -> tuple:
    """``HTTPStatusError`` classes that can reach us, from both httpx flavours.

    A 401 can be raised either by the MCP SDK's own HTTP stack (``httpx2`` on
    mcp >= 2.0) or by Hermes' pinned ``httpx``, and the two define unrelated
    exception classes. Both go in the tuple so ``isinstance`` covers whichever
    layer raised.
    """
    global _HTTP_STATUS_ERROR_TYPES
    if _HTTP_STATUS_ERROR_TYPES is not None:
        return _HTTP_STATUS_ERROR_TYPES
    found: list = []
    sdk_mod = sdk_httpx()
    if sdk_mod is not None:
        found.append(sdk_mod.HTTPStatusError)
    try:
        import httpx
        if httpx.HTTPStatusError not in found:
            found.append(httpx.HTTPStatusError)
    except ImportError:
        pass
    _HTTP_STATUS_ERROR_TYPES = tuple(found)
    return _HTTP_STATUS_ERROR_TYPES
'''

EXPORTED_NAMES = ('_MCPServerTaskTransportMixin', 'MCPServerTask', '_servers', '_server_connecting', '_server_connect_errors', '_lazy_server_configs', '_lazy_server_fingerprints', '_lazy_server_tool_names', '_connect_server_claim', '_server_connect_retry_after', '_server_connect_failures', '_CONNECT_RETRY_BASE_BACKOFF_SEC', '_CONNECT_RETRY_MAX_BACKOFF_SEC', '_record_connect_failure', '_clear_connect_failure', '_connect_cooldown_active', '_server_error_counts', '_server_breaker_opened_at', '_CIRCUIT_BREAKER_THRESHOLD', '_CIRCUIT_BREAKER_COOLDOWN_SEC', '_server_trust_levels', '_tool_read_only_hints', '_TRUST_FULL', '_TRUST_UNTRUSTED', '_normalize_server_trust', '_annotation_read_only_hint', '_record_tool_trust_metadata', '_trust_gate_check', '_bump_server_error', '_reset_server_error', '_signal_reconnect', 'reconnect_mcp_server', '_wait_for_server_session_ready', '_signal_reconnect_and_wait', '_AUTH_ERROR_TYPES', '_HTTP_STATUS_ERROR_TYPES', '_http_status_error_types')
SOURCE_PATH = Path(__file__)

def __getattr__(name: str):
    """Resolve exports when this shard is imported before the facade."""
    if name not in EXPORTED_NAMES:
        raise AttributeError(name)
    # The facade owns executable globals; importing it here avoids duplicating
    # the shard loader and keeps direct shard imports cycle-safe.
    from tools import mcp_tool
    try:
        return getattr(mcp_tool, name)
    except AttributeError:
        raise AttributeError(name) from None


def install(namespace: dict[str, object]) -> None:
    filename = str(SOURCE_PATH)
    linecache.cache[filename] = (
        len(_SOURCE), None, _SOURCE.splitlines(True), filename
    )
    exec(compile(_SOURCE, filename, "exec"), namespace, namespace)
