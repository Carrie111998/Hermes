"""Sequential tool execution shard for the legacy executor namespace.

The implementation remains byte-identical; this module forwards unresolved
module globals to ``agent.tool_executor`` so existing patch seams continue to
work through the legacy owner.
"""

from agent import tool_executor as _owner


def __getattr__(name):
    return getattr(_owner, name)


def _sync_owner_globals():
    """Refresh legacy-owner globals so existing monkeypatch seams hold."""
    for name in _OWNER_GLOBAL_NAMES:
        try:
            globals()[name] = getattr(_owner, name)
        except AttributeError:
            continue


_OWNER_GLOBAL_NAMES = frozenset(
    name for name in dir(_owner)
    if not name.startswith("__")
    and name not in {"execute_tool_calls_sequential", "execute_tool_calls_segmented"}
)


def execute_tool_calls_sequential(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, *, finalize: bool = True) -> None:
    """Execute tool calls sequentially (original behavior). Used for single calls or interactive tools.

    ``finalize=False`` skips the end-of-batch aggregate budget enforcement
    and /steer injection — used when this call is one segment of a larger
    mixed batch and the segmented dispatcher owns the turn-end work.
    """
    _sync_owner_globals()
    # Resolve the context-scaled tool-output budget once per turn.
    _tool_budget = _budget_for_agent(agent)

    # Keep every runtime-tool branch on one bounded execution funnel without
    # duplicating timeout policy across the branch-specific callbacks below.
    def _run_agent_tool_execution_middleware(agent, **kwargs):
        return _run_sequential_tool_execution_middleware(agent, **kwargs)

    for i, tool_call in enumerate(assistant_message.tool_calls, 1):
        tool_call_id = _pairing_tool_call_id(tool_call)
        if getattr(agent, "_incremental_persistence_failed", False):
            return
        # SAFETY: check interrupt BEFORE starting each tool.
        # If the user sent "stop" during a previous tool's execution,
        # do NOT start any more tools -- skip them all immediately.
        if agent._interrupt_requested:
            remaining_calls = assistant_message.tool_calls[i-1:]
            if remaining_calls:
                agent._vprint(f"{agent.log_prefix}⚡ Interrupt: skipping {len(remaining_calls)} tool call(s)", force=True)
            for skipped_tc in remaining_calls:
                skipped_name = skipped_tc.function.name
                cancelled_result = (
                    f"[Tool execution cancelled — {skipped_name} was skipped "
                    "due to user interrupt]"
                )
                messages.append(make_tool_result_message(
                    skipped_name,
                    cancelled_result,
                    _pairing_tool_call_id(skipped_tc),
                    effect_disposition="none",
                ))
                _emit_terminal_post_tool_call(
                    agent,
                    function_name=skipped_name,
                    function_args={},
                    result=cancelled_result,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(skipped_tc, "id", "") or "",
                    status="cancelled",
                    error_type="user_interrupt",
                    error_message="Tool execution skipped due to user interrupt",
                )
                if not _flush_session_db_after_tool_progress(
                    agent,
                    messages,
                    stage=f"cancelled tool result {skipped_name}",
                ):
                    return
            break

        function_name = tool_call.function.name

        function_args, malformed_args_result = _parse_tool_arguments(
            tool_call.function.arguments
        )
        if malformed_args_result is not None:
            _emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=function_args,
                result=malformed_args_result,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                status="error",
                error_type="invalid_tool_arguments",
                error_message="Tool arguments must be a valid JSON object",
            )
            messages.append(
                make_tool_result_message(
                    function_name,
                    malformed_args_result,
                    tool_call_id,
                )
            )
            if not _flush_session_db_after_tool_progress(
                agent,
                messages,
                stage=f"invalid tool arguments {function_name}",
            ):
                return
            continue

        # Tool Search unwrap — see execute_tool_calls_concurrent for full
        # rationale, including the scope gate (the unwrap dispatches the
        # underlying tool directly, so session toolset scope is enforced here).
        _ts_scope_block: Optional[str] = None
        try:
            from tools import tool_search as _ts
            if function_name == _ts.TOOL_CALL_NAME:
                _underlying, _underlying_args, _err = _ts.resolve_underlying_call(function_args)
                if not _err and _underlying:
                    if _underlying in _tool_search_scoped_names(agent):
                        # Probe-validate before unwrapping (ironclaw#5149):
                        # missing required args return the parameter schema
                        # instead of dispatching into an opaque failure.
                        _probe_err = _ts.validate_deferred_call_args(_underlying, _underlying_args)
                        if _probe_err is not None:
                            # This path wraps _block_msg in {"error": ...} —
                            # flatten the probe payload to one plain string.
                            try:
                                _probe = json.loads(_probe_err)
                                _ts_scope_block = (
                                    f"{_probe.get('error', '')} Parameters schema: "
                                    f"{json.dumps(_probe.get('parameters', {}), ensure_ascii=False)}. "
                                    f"{_probe.get('hint', '')}"
                                ).strip()
                            except Exception:
                                _ts_scope_block = _probe_err
                        else:
                            function_name = _underlying
                            function_args = _underlying_args
                    else:
                        _ts_scope_block = (
                            f"'{_underlying}' is not available in this session. "
                            "Use tool_search to find tools you can call."
                        )
        except Exception:
            pass

        middleware_trace: list[dict[str, Any]] = []
        _execution_blocked = False
        _execution_dispatched = False

        tool_start_time = time.time()

        if function_name == "todo":
            def _execute(next_args: dict) -> Any:
                from tools.todo_tool import todo_tool as _todo_tool
                return _todo_tool(
                    todos=next_args.get("todos"),
                    merge=next_args.get("merge", False),
                    store=agent._todo_store,
                )
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('todo', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "message_agent":
            # Bot Mode teammate DM (tools/bot_mode_dm.py) — injected, not
            # registered: only a canonical Bot Chat session carries the
            # schema, and the tool re-gates on the session title itself.
            def _execute(next_args: dict) -> Any:
                from tools.bot_mode_dm import message_agent_tool as _message_agent_tool
                return _message_agent_tool(
                    target=next_args.get("target", ""),
                    message=next_args.get("message", ""),
                    task_id=effective_task_id,
                    agent=agent,
                )
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('message_agent', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "session_search":
            def _execute(next_args: dict) -> Any:
                session_db = agent._get_session_db_for_recall()
                if not session_db:
                    from hermes_state import format_session_db_unavailable
                    return json.dumps({"success": False, "error": format_session_db_unavailable()})
                from tools.session_search_tool import session_search as _session_search
                return _session_search(
                    query=next_args.get("query", ""),
                    role_filter=next_args.get("role_filter"),
                    limit=next_args.get("limit", 3),
                    session_id=next_args.get("session_id"),
                    around_message_id=next_args.get("around_message_id"),
                    window=next_args.get("window", 5),
                    sort=next_args.get("sort"),
                    detail=next_args.get("detail", "adaptive"),
                    db=session_db,
                    current_session_id=agent.session_id,
                )
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('session_search', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "memory":
            def _execute(next_args: dict) -> Any:
                target = next_args.get("target", "memory")
                operations = next_args.get("operations")
                from tools.memory_tool import memory_tool as _memory_tool
                result = _memory_tool(
                    action=next_args.get("action"),
                    target=target,
                    content=next_args.get("content"),
                    old_text=next_args.get("old_text"),
                    operations=operations,
                    store=agent._memory_store,
                )
                # Mirror successful built-in memory writes to external
                # providers. All gating/op-expansion lives behind the manager
                # interface (MemoryManager.notify_memory_tool_write).
                if agent._memory_manager:
                    agent._memory_manager.notify_memory_tool_write(
                        result,
                        next_args,
                        build_metadata=lambda: agent._build_memory_write_metadata(
                            task_id=effective_task_id,
                            tool_call_id=tool_call_id,
                        ),
                    )
                return result
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('memory', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "clarify":
            def _execute(next_args: dict) -> Any:
                from tools.clarify_tool import clarify_tool as _clarify_tool
                return _clarify_tool(
                    question=next_args.get("question", ""),
                    choices=next_args.get("choices"),
                    multi_select=next_args.get("multi_select", False),
                    questions=next_args.get("questions"),
                    callback=agent.clarify_callback,
                )
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('clarify', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "read_terminal":
            def _execute(next_args: dict) -> Any:
                from tools.read_terminal_tool import read_terminal_tool as _read_terminal_tool
                return _read_terminal_tool(
                    start_line=next_args.get("start_line"),
                    count=next_args.get("count"),
                    callback=getattr(agent, "read_terminal_callback", None),
                )
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('read_terminal', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "desktop_preview":
            def _execute(next_args: dict) -> Any:
                if (next_args.get("action") or "").strip() == "read":
                    from tools.read_preview_tool import read_preview_tool as _read_preview_tool
                    return _read_preview_tool(
                        start=next_args.get("start"),
                        count=next_args.get("count"),
                        callback=getattr(agent, "read_preview_callback", None),
                    )
                from tools.preview_tool import _handle_preview
                return _handle_preview(next_args)
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('desktop_preview', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "read_preview":
            def _execute(next_args: dict) -> Any:
                from tools.read_preview_tool import read_preview_tool as _read_preview_tool
                return _read_preview_tool(
                    start=next_args.get("start"),
                    count=next_args.get("count"),
                    callback=getattr(agent, "read_preview_callback", None),
                )
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('read_preview', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "drive_preview":
            def _execute(next_args: dict) -> Any:
                from tools.drive_preview_tool import drive_preview_tool as _drive_preview_tool
                return _drive_preview_tool(
                    action=next_args.get("action", ""),
                    ref=next_args.get("ref"),
                    selector=next_args.get("selector"),
                    text=next_args.get("text"),
                    key=next_args.get("key"),
                    submit=next_args.get("submit"),
                    amount=next_args.get("amount"),
                    to=next_args.get("to"),
                    limit=next_args.get("max"),
                    callback=getattr(agent, "drive_preview_callback", None),
                )
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('drive_preview', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "annotate_preview":
            def _execute(next_args: dict) -> Any:
                from tools.annotate_preview_tool import annotate_preview_tool as _annotate_preview_tool
                return _annotate_preview_tool(
                    action=next_args.get("action", "add"),
                    ref=next_args.get("ref"),
                    selector=next_args.get("selector"),
                    label=next_args.get("label"),
                    callback=getattr(agent, "drive_preview_callback", None),
                )
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('annotate_preview', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "read_window_below":
            def _execute(next_args: dict) -> Any:
                from tools.read_window_tool import read_window_below_tool as _read_window_below_tool
                return _read_window_below_tool(
                    callback=getattr(agent, "read_window_below_callback", None),
                )
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('read_window_below', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "tour":
            def _execute(next_args: dict) -> Any:
                from tools.tour_tool import tour_tool as _tour_tool
                return _tour_tool(
                    action=next_args.get("action", ""),
                    surface=next_args.get("surface"),
                    selector=next_args.get("selector"),
                    title=next_args.get("title"),
                    text=next_args.get("text"),
                    side=next_args.get("side"),
                    steps=next_args.get("steps"),
                    step_index=next_args.get("step_index"),
                    callback=getattr(agent, "tour_callback", None),
                )
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('tour', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "setup_mcp":
            def _execute(next_args: dict) -> Any:
                from tools.setup_mcp_tool import setup_mcp_tool as _setup_mcp_tool
                return _setup_mcp_tool(
                    server=next_args.get("server", ""),
                    action=next_args.get("action", "install"),
                    reason=next_args.get("reason", ""),
                    callback=getattr(agent, "setup_mcp_callback", None),
                )
            function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            ))
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('setup_mcp', function_args, tool_duration, result=sanitize_tool_result_for_sink(function_result))}")
        elif function_name == "delegate_task":
            _action_arg = str(function_args.get("action") or "").strip().lower()
            tasks_arg = function_args.get("tasks")
            if _action_arg in ("list", "steer", "stop"):
                spinner_label = f"🔀 subagent {_action_arg}"
            elif tasks_arg and isinstance(tasks_arg, list):
                spinner_label = f"🔀 delegating {len(tasks_arg)} tasks · (/agents to monitor)"
            else:
                goal_preview = (function_args.get("goal") or "")[:30]
                spinner_label = (
                    f"🔀 {goal_preview} · (/agents to monitor)"
                    if goal_preview
                    else "🔀 delegating · (/agents to monitor)"
                )
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                spinner = KawaiiSpinner(f"{face} {spinner_label}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            agent._delegate_spinner = spinner
            _delegate_result = None
            try:
                def _execute(next_args: dict) -> Any:
                    return agent._dispatch_delegate_task(next_args)
                function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=tool_call_id,
                    execute=_execute,
                    scope_block=_ts_scope_block,
                    display_index=i,
                ))
                _delegate_result = function_result
            finally:
                agent._delegate_spinner = None
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(
                    'delegate_task', function_args, tool_duration,
                    result=sanitize_tool_result_for_sink(_delegate_result),
                )
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        elif agent._context_engine_tool_names and function_name in agent._context_engine_tool_names:
            # Context engine tools (lcm_grep, lcm_describe, lcm_expand, etc.)
            spinner = None
            if agent._should_emit_quiet_tool_messages():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                emoji = _get_tool_emoji(function_name)
                display_args = _redact_tool_args_for_display(function_name, function_args) or function_args
                preview = _build_tool_label(function_name, display_args) or function_name
                spinner = KawaiiSpinner(f"{face} {emoji} {preview}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            _ce_result = None
            try:
                def _execute(next_args: dict) -> Any:
                    return agent.context_compressor.handle_tool_call(function_name, next_args, messages=messages)
                function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=tool_call_id,
                    execute=_execute,
                    scope_block=_ts_scope_block,
                    display_index=i,
                ))
                _ce_result = function_result
            except Exception as tool_error:
                function_result = json.dumps({"error": f"Context engine tool '{function_name}' failed: {tool_error}"})
                logger.error(
                    "context_engine.handle_tool_call raised for %s: %s",
                    function_name,
                    sanitize_tool_result_for_sink(tool_error),
                )
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(
                    function_name, function_args, tool_duration,
                    result=sanitize_tool_result_for_sink(_ce_result),
                )
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        elif agent._memory_manager and agent._memory_manager.has_tool(function_name):
            # Memory provider tools (hindsight_retain, honcho_search, etc.)
            # These are not in the tool registry — route through MemoryManager.
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                emoji = _get_tool_emoji(function_name)
                display_args = _redact_tool_args_for_display(function_name, function_args) or function_args
                preview = _build_tool_label(function_name, display_args) or function_name
                spinner = KawaiiSpinner(f"{face} {emoji} {preview}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            _mem_result = None
            try:
                def _execute(next_args: dict) -> Any:
                    return agent._memory_manager.handle_tool_call(function_name, next_args)
                function_result, function_args, middleware_trace, _execution_blocked, _execution_dispatched = _managed_values(_run_agent_tool_execution_middleware(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=tool_call_id,
                    execute=_execute,
                    scope_block=_ts_scope_block,
                    display_index=i,
                ))
                _mem_result = function_result
            except Exception as tool_error:
                function_result = json.dumps({"error": f"Memory tool '{function_name}' failed: {tool_error}"})
                logger.error(
                    "memory_manager.handle_tool_call raised for %s: %s",
                    function_name,
                    sanitize_tool_result_for_sink(tool_error),
                )
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(
                    function_name, function_args, tool_duration,
                    result=sanitize_tool_result_for_sink(_mem_result),
                )
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        elif agent.quiet_mode:
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                emoji = _get_tool_emoji(function_name)
                display_args = _redact_tool_args_for_display(function_name, function_args) or function_args
                preview = _build_tool_label(function_name, display_args) or function_name
                spinner = KawaiiSpinner(f"{face} {emoji} {preview}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            _spinner_result = None
            try:
                def _execute(next_args: dict) -> Any:
                    from model_tools import suppress_post_tool_call_hook

                    with suppress_post_tool_call_hook():
                        return _ra().handle_function_call(
                            function_name,
                            next_args,
                            effective_task_id,
                            tool_call_id=tool_call_id,
                            session_id=agent.session_id or "",
                            turn_id=getattr(agent, "_current_turn_id", "") or "",
                            api_request_id=getattr(agent, "_current_api_request_id", "")
                            or "",
                            enabled_tools=(
                                list(agent.valid_tool_names)
                                if agent.valid_tool_names
                                else None
                            ),
                            skip_pre_tool_call_hook=True,
                            skip_tool_request_middleware=True,
                            skip_tool_execution_middleware=True,
                            tool_request_middleware_trace=list(middleware_trace),
                            enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                            disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                        )

                (
                    function_result,
                    function_args,
                    middleware_trace,
                    _execution_blocked,
                    _execution_dispatched,
                ) = _managed_values(
                    _run_agent_tool_execution_middleware(
                        agent,
                        function_name=function_name,
                        function_args=function_args,
                        effective_task_id=effective_task_id,
                        tool_call_id=tool_call_id,
                        execute=_execute,
                        scope_block=_ts_scope_block,
                        display_index=i,
                        middleware_trace=middleware_trace,
                    )
                )
                _spinner_result = function_result
            except KeyboardInterrupt:
                function_result = _emit_cancelled_terminal_post_tool_call(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=tool_call_id,
                    start_time=tool_start_time,
                    middleware_trace=list(middleware_trace),
                )
                _spinner_result = function_result
                try:
                    agent.interrupt("keyboard interrupt")
                except Exception:
                    pass
                # Emit a tool result for THIS call and every remaining call in
                # the batch before re-raising, so the assistant tool-call turn
                # is never left without matching tool results (alternation).
                _append_cancelled_tool_results(
                    messages,
                    assistant_message.tool_calls[i - 1:],
                    reason="keyboard interrupt",
                )
                raise
            except Exception as tool_error:
                function_result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error(
                    "handle_function_call raised for %s: %s",
                    function_name,
                    sanitize_tool_result_for_sink(tool_error),
                )
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(
                    function_name, function_args, tool_duration,
                    result=sanitize_tool_result_for_sink(_spinner_result),
                )
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        else:
            try:
                def _execute(next_args: dict) -> Any:
                    from model_tools import suppress_post_tool_call_hook

                    with suppress_post_tool_call_hook():
                        return _ra().handle_function_call(
                            function_name,
                            next_args,
                            effective_task_id,
                            tool_call_id=tool_call_id,
                            session_id=agent.session_id or "",
                            turn_id=getattr(agent, "_current_turn_id", "") or "",
                            api_request_id=getattr(agent, "_current_api_request_id", "")
                            or "",
                            enabled_tools=(
                                list(agent.valid_tool_names)
                                if agent.valid_tool_names
                                else None
                            ),
                            skip_pre_tool_call_hook=True,
                            skip_tool_request_middleware=True,
                            skip_tool_execution_middleware=True,
                            tool_request_middleware_trace=list(middleware_trace),
                            enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                            disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                        )

                (
                    function_result,
                    function_args,
                    middleware_trace,
                    _execution_blocked,
                    _execution_dispatched,
                ) = _managed_values(
                    _run_agent_tool_execution_middleware(
                        agent,
                        function_name=function_name,
                        function_args=function_args,
                        effective_task_id=effective_task_id,
                        tool_call_id=tool_call_id,
                        execute=_execute,
                        scope_block=_ts_scope_block,
                        display_index=i,
                        middleware_trace=middleware_trace,
                    )
                )
            except KeyboardInterrupt:
                _emit_cancelled_terminal_post_tool_call(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=tool_call_id,
                    start_time=tool_start_time,
                    middleware_trace=list(middleware_trace),
                )
                try:
                    agent.interrupt("keyboard interrupt")
                except Exception:
                    pass
                # Emit a tool result for THIS call and every remaining call in
                # the batch before re-raising (see interactive branch above).
                _append_cancelled_tool_results(
                    messages,
                    assistant_message.tool_calls[i - 1:],
                    reason="keyboard interrupt",
                )
                raise
            except Exception as tool_error:
                function_result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error(
                    "handle_function_call raised for %s: %s",
                    function_name,
                    sanitize_tool_result_for_sink(tool_error),
                )
            tool_duration = time.time() - tool_start_time

        _execution_timed_out = isinstance(
            function_result, (_ToolTimeoutResult, _ToolCancelledResult)
        )
        if isinstance(function_result, str):
            result_preview = sanitize_tool_result_for_sink(function_result) if agent.verbose_logging else (
                sanitize_tool_result_for_sink(function_result)[:200] if len(function_result) > 200 else sanitize_tool_result_for_sink(function_result)
            )
            _result_len = len(function_result)
        else:
            # Multimodal/custom results are also log sinks; never pass their
            # raw object representation to the error logger.
            result_preview = sanitize_tool_result_for_sink(
                _multimodal_text_summary(function_result)
            )
            _result_len = len(result_preview)

        # Log tool errors to the persistent error log so [error] tags
        # in the UI always have a corresponding detailed entry on disk.
        _is_error_result, _ = _detect_tool_failure(function_name, function_result)
        # The agent-runtime tools above (todo, session_search, memory,
        # context-engine, memory-manager, clarify, delegate_task) are
        # dispatched inline — they never reach handle_function_call, so the
        # executor is the one that has to fire post_tool_call. For
        # Every dispatch suppresses the inner handle_function_call observer so
        # the executor owns one terminal event for this tool_call_id. This also
        # prevents an abandoned timeout worker from reporting late success.
        _executor_must_emit_post_hook = (
            not _execution_blocked
            and not _execution_timed_out
        )
        if _executor_must_emit_post_hook:
            _emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=function_args,
                result=sanitize_tool_result_for_sink(function_result),
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                duration_ms=int(tool_duration * 1000),
                middleware_trace=list(middleware_trace),
            )
        if not _execution_blocked:
            function_result = agent._append_guardrail_observation(
                function_name,
                function_args,
                function_result,
                failed=_is_error_result,
                tool_call_id=tool_call_id,
            )
            result_preview = sanitize_tool_result_for_sink(function_result) if agent.verbose_logging else (
                sanitize_tool_result_for_sink(function_result)[:200] if len(function_result) > 200 else sanitize_tool_result_for_sink(function_result)
            )
        if _is_error_result:
            logger.warning("Tool %s returned error (%.2fs): %s", function_name, tool_duration, result_preview)
        else:
            logger.info("tool %s completed (%.2fs, %d chars)", function_name, tool_duration, _result_len)

        # Track file-mutation outcome for the turn-end verifier.  See
        # the concurrent path for the rationale; both paths must feed
        # the same state so the footer reflects every tool call in the
        # turn, not just the parallel ones.
        if not _execution_blocked:
            try:
                agent._record_file_mutation_result(
                    function_name, function_args, function_result, _is_error_result,
                )
            except Exception as _ver_err:
                logging.debug("file-mutation verifier record failed: %s", _ver_err)

        agent._current_tool = None
        _status_suffix = " (error)" if _is_error_result else ""
        agent._touch_activity(f"tool completed: {function_name} ({tool_duration:.1f}s){_status_suffix}")

        if agent.verbose_logging:
            logging.debug("Tool %s completed in %.2fs", function_name, tool_duration)
            _log_result = sanitize_tool_result_for_sink(_multimodal_text_summary(function_result))
            logging.debug("Tool result (%d chars): %s", len(_log_result), _log_result)

        display_function_result = sanitize_tool_result_for_sink(function_result)
        function_result = maybe_persist_tool_result(
            content=function_result,
            tool_name=function_name,
            tool_use_id=tool_call_id,
            env=get_active_env(effective_task_id),
            config=_tool_budget,
        ) if not _is_multimodal_tool_result(function_result) else function_result
        _record_persisted_path_for_stub(agent, tool_call_id, function_result)

        # Discover subdirectory context files from tool arguments
        subdir_hints = agent._subdirectory_hints.check_tool_call(function_name, function_args)
        if subdir_hints:
            if _is_multimodal_tool_result(function_result):
                _append_subdir_hint_to_multimodal(function_result, subdir_hints)
            else:
                function_result += subdir_hints

        # Unwrap _multimodal dicts to an OpenAI-style content list
        # (see parallel path for rationale). String results pass through.
        _tool_content = agent._tool_result_content_for_active_model(function_name, function_result)
        tool_message = make_tool_result_message(
            function_name,
            _tool_content,
            tool_call_id,
            effect_disposition="unknown" if _execution_timed_out else None,
        )
        messages.append(tool_message)
        risk_metadata = tool_message.get("_tool_output_risk")
        if not _flush_session_db_after_tool_progress(
            agent,
            messages,
            stage=f"tool result {function_name}",
        ):
            return

        # UI completion/progress events are projections of the canonical tool
        # row, never a competing in-memory authority.
        if not _execution_blocked and agent.tool_progress_callback:
            try:
                agent.tool_progress_callback(
                    "tool.completed", function_name, None, None,
                    duration=tool_duration, is_error=_is_error_result,
                    result=display_function_result,
                )
            except Exception as cb_err:
                logging.debug("Tool progress callback error: %s", cb_err)

        if not _execution_blocked and agent.tool_complete_callback:
            try:
                display_args = (
                    _redact_tool_args_for_display(function_name, function_args)
                    or function_args
                )
                agent.tool_complete_callback(
                    tool_call_id,
                    function_name,
                    display_args,
                    display_function_result,
                )
            except Exception as cb_err:
                logging.debug("Tool complete callback error: %s", cb_err)

        if (
            risk_metadata is not None
            and risk_metadata.get("risk") != "low"
            and agent.tool_progress_callback
        ):
            try:
                agent.tool_progress_callback(
                    "tool.output_risk",
                    function_name,
                    None,
                    None,
                    tool_call_id=tool_call_id,
                    risk_metadata=risk_metadata,
                )
            except Exception as cb_err:
                logging.debug("Tool output risk callback error: %s", cb_err)

        if not agent.quiet_mode and getattr(agent, "tool_progress_mode", "all") != "off":
            if agent.verbose_logging:
                print(f"  ✅ Tool {i} completed in {tool_duration:.2f}s")
                print(agent._wrap_verbose(
                    "Result: ", sanitize_tool_result_for_sink(function_result)
                ))
            else:
                _fr_str = sanitize_tool_result_for_sink(function_result)
                response_preview = _fr_str[:agent.log_prefix_chars] + "..." if len(_fr_str) > agent.log_prefix_chars else _fr_str
                print(f"  ✅ Tool {i} completed in {tool_duration:.2f}s - {response_preview}")

        if agent._interrupt_requested and i < len(assistant_message.tool_calls):
            remaining = len(assistant_message.tool_calls) - i
            agent._vprint(f"{agent.log_prefix}⚡ Interrupt: skipping {remaining} remaining tool call(s)", force=True)
            for skipped_tc in assistant_message.tool_calls[i:]:
                skipped_name = skipped_tc.function.name
                messages.append(make_tool_result_message(
                    skipped_name,
                    f"[Tool execution skipped — {skipped_name} was not started. User sent a new message]",
                    _pairing_tool_call_id(skipped_tc),
                    effect_disposition="none",
                ))
                if not _flush_session_db_after_tool_progress(
                    agent,
                    messages,
                    stage=f"skipped tool result {skipped_name}",
                ):
                    return
            break

    # ── Per-turn aggregate budget enforcement ─────────────────────────
    # Keep /steer pending until the final post-budget drain below.  The model
    # only receives this batch after all calls finish, and an early drain can
    # be discarded when aggregate budget enforcement replaces a tool result.
    num_tools_seq = len(assistant_message.tool_calls)
    if finalize and num_tools_seq > 0:
        enforce_turn_budget(messages[-num_tools_seq:], env=get_active_env(effective_task_id), config=_tool_budget)

    # ── /steer injection ──────────────────────────────────────────────
    # See _execute_tool_calls_parallel for the rationale. Same hook,
    # applied to sequential execution as well.
    if finalize and num_tools_seq > 0:
        agent._apply_pending_steer_to_tool_results(messages, num_tools_seq)


def execute_tool_calls_segmented(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, segments=None) -> None:
    """Execute a mixed tool-call batch as ordered parallel/sequential segments.

    ``segments`` is the ``(kind, calls)`` plan from
    ``_plan_tool_batch_segments``: maximal contiguous runs of parallel-safe
    calls execute on the concurrent path, barrier calls on the sequential
    path, strictly in the model's original call order. Because segments are
    contiguous, every tool result is still appended one-per-call in emission
    order and no call ever starts before an earlier barrier finishes —
    identical ordering and side-effect boundaries to fully-sequential
    execution, with I/O parallelism recovered inside the safe runs.

    Turn-end work (aggregate budget enforcement + /steer injection) is done
    once here for the WHOLE batch; the per-segment executor calls run with
    ``finalize=False`` so a multi-segment turn cannot multiply the budget or
    truncate a steer marker.

    Interrupt semantics: each segment executor already checks
    ``agent._interrupt_requested`` up front and appends a cancelled/skipped
    result per call, so an interrupt during segment *k* drains segments
    *k+1..n* without executing them while preserving one result per
    tool_call_id.
    """
    _sync_owner_globals()
    from types import SimpleNamespace

    if segments is None:
        _active_env = get_active_env(effective_task_id)
        _exec_cwd = Path(_active_env.cwd) if _active_env is not None and _active_env.cwd else None
        segments = _plan_tool_batch_segments(assistant_message.tool_calls, execution_cwd=_exec_cwd)

    for kind, calls in segments:
        if getattr(agent, "_incremental_persistence_failed", False):
            return
        segment_message = SimpleNamespace(tool_calls=list(calls))
        if kind == "parallel":
            execute_tool_calls_concurrent(
                agent, segment_message, messages, effective_task_id, api_call_count,
                finalize=False,
            )
        else:
            execute_tool_calls_sequential(
                agent, segment_message, messages, effective_task_id, api_call_count,
                finalize=False,
            )

        if getattr(agent, "_incremental_persistence_failed", False):
            return

    # ── Whole-turn finalize (budget + /steer) ─────────────────────────
    total_tools = len(assistant_message.tool_calls)
    if total_tools > 0:
        _tool_budget = _budget_for_agent(agent)
        enforce_turn_budget(
            messages[-total_tools:],
            env=get_active_env(effective_task_id),
            config=_tool_budget,
        )
        agent._apply_pending_steer_to_tool_results(messages, total_tools)
