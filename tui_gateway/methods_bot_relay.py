"""Bot-relay JSON-RPC handlers — the gateway side of cross-connection A2A.

Connections ARE the peer set: every gateway the Desktop holds a socket to
(local, remote URL, SSH, Hermes Cloud, docker) must be able to find every
other connection's agents and message them. The Desktop is the relay — it
owns every socket — and these four methods are the door it uses on EACH
connected gateway:

- ``bot_relay.roster.sync``  — Desktop pushes the union roster of agents on
  the OTHER connections into this gateway's ``bot_relay/roster.json``, so
  ``message_agent`` can resolve cross-connection targets and Bot Chat
  prompts list them (capability-epoch refresh picks up changes).
- ``bot_relay.outbox.claim`` / ``renew`` / ``ack`` / ``nack`` — v2 Desktop
  couriers use bounded renewable leases and explicit fenced settlement.
- ``bot_relay.outbox.drain`` — rolling-upgrade v1 one-shot lane only.
- ``bot_relay.deliver``      — Desktop hands an envelope to the TARGET
  gateway; this method runs the same one-turn Bot Chat delivery local DMs
  use and returns the reply text.
- ``bot_relay.reply``        — Desktop writes the reply (or a delivery
  error) back on the SENDER gateway; the waiter spawned at send time picks
  it up and wakes the sending agent via the standard completion path.

Storage/validation plumbing lives in ``tools/bot_relay.py``. Handlers are
rebound onto server.py's globals at install time (see method_ctx.py) and may
reference server module globals (``_ok``, ``_err``) not imported here.
"""

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method


@method("bot_relay.capabilities")
def _(rid, params: dict) -> dict:
    """Negotiate the durable lane before any tool-capable target turn."""
    del params
    return _ok(
        rid,
        {
            "protocol_version": 2,
            "leased_outbox": True,
            "durable_inbox": True,
            "fenced_settlement": True,
        },
    )


@method("bot_relay.roster.sync")
def _(rid, params: dict) -> dict:
    """Replace this gateway's view of agents on OTHER connections.

    Params: ``agents`` — list of rows ``{profile, handle, connection_id,
    connection_label?, title?, description?}``. Rows failing validation are
    dropped, not fatal. Result: ``{count}`` (accepted rows).
    """
    try:
        import os
        from pathlib import Path

        from tools.bot_relay import write_remote_roster

        home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
        root = home.parent.parent if home.parent.name == "profiles" else home
        count = write_remote_roster(
            root,
            params.get("agents"),
            courier_namespace_id=str(params.get("courier_namespace_id") or ""),
        )
        return _ok(rid, {"count": count})
    except Exception as e:
        return _err(rid, 5090, str(e))


@method("bot_relay.outbox.drain")
def _(rid, params: dict) -> dict:
    """Claim every pending cross-connection envelope queued on this gateway.

    Claimed envelopes move to ``claimed/`` atomically, so concurrent drains
    (two Desktop windows) can't double-deliver. Result: ``{envelopes}``.
    """
    try:
        import os
        from pathlib import Path

        from tools.bot_relay import claim_pending_envelopes

        home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
        root = home.parent.parent if home.parent.name == "profiles" else home
        return _ok(rid, {"envelopes": claim_pending_envelopes(root)})
    except Exception as e:
        return _err(rid, 5091, str(e))


@method("bot_relay.outbox.claim")
def _(rid, params: dict) -> dict:
    """Claim a bounded batch of events owned by this Desktop namespace."""
    try:
        import os
        from pathlib import Path

        from tools.bot_relay import claim_leased_envelopes

        home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
        root = home.parent.parent if home.parent.name == "profiles" else home
        envelopes = claim_leased_envelopes(
            root,
            courier_namespace_id=str(params.get("courier_namespace_id") or ""),
            courier_id=str(params.get("courier_id") or ""),
            limit=params.get("limit", 4),
            lease_seconds=params.get("lease_seconds", 180),
        )
        return _ok(rid, {"envelopes": envelopes})
    except ValueError as e:
        return _err(rid, 4095, str(e))
    except Exception as e:
        return _err(rid, 5096, str(e))


@method("bot_relay.outbox.renew")
def _(rid, params: dict) -> dict:
    """Renew one live lease; token+generation are the fencing authority."""
    try:
        import os
        from pathlib import Path

        from gateway.durable_events import LeaseMismatch
        from tools.bot_relay import renew_envelope_lease

        home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
        root = home.parent.parent if home.parent.name == "profiles" else home
        result = renew_envelope_lease(
            root,
            envelope_id=str(params.get("id") or ""),
            courier_id=str(params.get("courier_id") or ""),
            lease_token=str(params.get("lease_token") or ""),
            lease_generation=params.get("lease_generation"),
            lease_seconds=params.get("lease_seconds", 180),
        )
        return _ok(rid, result)
    except (LeaseMismatch, ValueError, TypeError):
        return _err(rid, 4096, "lease_mismatch")
    except Exception as e:
        return _err(rid, 5097, str(e))


@method("bot_relay.outbox.ack")
def _(rid, params: dict) -> dict:
    """Atomically commit one immutable terminal outcome and clear its lease."""
    try:
        import os
        from pathlib import Path

        from gateway.durable_events import LeaseMismatch
        from tools.bot_relay import ack_envelope

        home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
        root = home.parent.parent if home.parent.name == "profiles" else home
        result = ack_envelope(
            root,
            envelope_id=str(params.get("id") or ""),
            courier_id=str(params.get("courier_id") or ""),
            lease_token=str(params.get("lease_token") or ""),
            lease_generation=params.get("lease_generation"),
            reply=str(params.get("reply") or ""),
            error=str(params.get("error") or ""),
            claimed_outcome_digest=str(params.get("outcome_digest") or ""),
        )
        return _ok(rid, result)
    except (LeaseMismatch, ValueError, TypeError):
        return _err(rid, 4096, "lease_mismatch")
    except Exception as e:
        return _err(rid, 5098, str(e))


@method("bot_relay.outbox.nack")
def _(rid, params: dict) -> dict:
    """Release for retry or commit a typed terminal delivery failure."""
    try:
        import os
        from pathlib import Path

        from gateway.durable_events import LeaseMismatch
        from tools.bot_relay import nack_envelope

        home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
        root = home.parent.parent if home.parent.name == "profiles" else home
        retryable = params.get("retryable")
        if not isinstance(retryable, bool):
            return _err(rid, 4096, "lease_mismatch")
        result = nack_envelope(
            root,
            envelope_id=str(params.get("id") or ""),
            courier_id=str(params.get("courier_id") or ""),
            lease_token=str(params.get("lease_token") or ""),
            lease_generation=params.get("lease_generation"),
            error=str(params.get("error") or "delivery failed"),
            retryable=retryable,
            retry_after_seconds=params.get("retry_after_seconds", 5),
        )
        return _ok(rid, result)
    except (LeaseMismatch, ValueError, TypeError):
        return _err(rid, 4096, "lease_mismatch")
    except Exception as e:
        return _err(rid, 5099, str(e))


@method("bot_relay.deliver")
def _(rid, params: dict) -> dict:
    """Deliver a relayed DM into a profile's Bot Chat ON THIS GATEWAY.

    Params: ``profile`` (target on this install), ``message`` (already
    attribution-prefixed by the sender gateway). Runs the same one-turn
    ``hermes -p <profile> chat -c "Bot Chat"`` transport local DMs use and
    returns ``{reply}`` — the target agent's response text. Blocking by
    design (the Desktop calls it from its relay worker, off any UI path;
    the RPC pool keeps it off the WS reader thread).
    """
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    profile = str(params.get("profile") or "").strip()
    event_id = str(params.get("id") or "").strip()
    is_v2 = bool(event_id)
    body = str(params.get("body") or "").strip()
    message = str(params.get("message") or "").strip()
    execution_token = ""
    if not profile or not (body if is_v2 else message):
        return _err(rid, 4090, "profile and message required")
    from gateway.durable_events import InboxConflict

    try:
        from tools.bot_mode_dm import MESSAGE_MAX_CHARS
        from tools.bot_relay import (
            acquire_turn_lock,
            begin_recipient_delivery,
            finish_recipient_delivery,
            local_delivery_command,
        )

        def finish_receipt(status: str, *, reply: str = "", error: str = "") -> dict:
            """Return only the canonical durable recipient result."""
            result = finish_recipient_delivery(
                root,
                event_id=event_id,
                target_profile=resolved,
                execution_token=execution_token,
                status=status,
                reply=reply,
                error=error,
            )
            canonical = result.get("result") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict)
                or result.get("action") != "cached"
                or result.get("status") not in {"succeeded", "failed", "cancelled", "indeterminate"}
                or not isinstance(canonical, dict)
            ):
                raise RuntimeError("recipient finalization returned no terminal durable result")
            canonical_status = str(canonical.get("status") or "")
            if canonical_status not in {"completed", "failed", "indeterminate"}:
                raise RuntimeError("recipient finalization returned a nonterminal result")
            return _ok(
                rid,
                {
                    "protocol_version": 2,
                    "durable_receipt": True,
                    "event_id": event_id,
                    "status": canonical_status,
                    "reply": str(canonical.get("reply") or ""),
                    "error": str(canonical.get("error") or ""),
                },
            )

        def finish_unknown(
            error: str = "delivery outcome is indeterminate; durable reconciliation required"
        ) -> dict:
            try:
                return finish_receipt("indeterminate", error=error)
            except Exception:
                return _err(
                    rid,
                    5094,
                    "recipient finalization unavailable; execution outcome is indeterminate",
                )

        if len(body if is_v2 else message) > MESSAGE_MAX_CHARS + (0 if is_v2 else 200):
            return _err(rid, 4091, "message too long")

        home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
        root = home.parent.parent if home.parent.name == "profiles" else home
        known = {"default"}
        profiles_dir = root / "profiles"
        if profiles_dir.is_dir():
            known.update(c.name for c in profiles_dir.iterdir() if c.is_dir())
        resolved = "default" if profile.lower() == "hermes" else profile
        if resolved not in known:
            return _err(rid, 4092, f"no profile '{profile}' on this gateway")

        if is_v2:
            requested_install = str(params.get("target_install_id") or "").strip()
            if requested_install:
                try:
                    from hermes_cli.web_server import get_install_id

                    actual_install = str(get_install_id() or "")
                except Exception:
                    return _err(rid, 5094, "target install identity unavailable")
                if not actual_install:
                    return _err(rid, 5094, "target install identity unavailable")
                if actual_install != requested_install:
                    return _err(rid, 4097, "target install identity mismatch")

            admission = begin_recipient_delivery(
                root,
                event_id=event_id,
                target_profile=resolved,
                body=body,
                from_profile=str(params.get("from_profile") or "").strip(),
                from_handle=str(params.get("from_handle") or "").strip().lstrip("@"),
                source_install_id=str(params.get("source_install_id") or ""),
                target_install_id=requested_install,
                courier_namespace_id=str(params.get("courier_namespace_id") or ""),
            )
            action = str(admission.get("action") or admission.get("status") or "")
            if action in {"cached", "completed", "failed", "indeterminate"}:
                cached = admission.get("result") or admission.get("outcome") or {}
                if not isinstance(cached, dict):
                    cached = {}
                return _ok(
                    rid,
                    {
                        "protocol_version": 2,
                        "durable_receipt": True,
                        "deduplicated": True,
                        "event_id": event_id,
                        "status": cached.get("status") or action,
                        "reply": str(cached.get("reply") or ""),
                        "error": str(cached.get("error") or ""),
                    },
                )
            if action in {"processing", "in_progress"}:
                try:
                    retry_after = max(
                        1.0,
                        min(float(admission.get("retry_after_seconds") or 5), 300.0),
                    )
                except (TypeError, ValueError):
                    retry_after = 5.0
                return _ok(
                    rid,
                    {
                        "protocol_version": 2,
                        "durable_receipt": True,
                        "deduplicated": True,
                        "event_id": event_id,
                        "status": "processing",
                        "retry_after_seconds": retry_after,
                    },
                )
            if action != "execute" or not admission.get("execution_token"):
                return _err(rid, 5094, "recipient admission failed")
            execution_token = str(admission["execution_token"])
            message = str(admission["message"])

        fd, tmp = tempfile.mkstemp(prefix="hermes-relay-dm-", suffix=".txt", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(message)
            # Per-profile turn lock (#93091): serialize with any other
            # delivery turn into this profile (relay or local message_agent).
            # The lock covers only the turn execution window. Worst-case
            # handler hold is lock wait (bot_mode.turn_wait_seconds, default
            # 120s) + the 600s turn timeout below — doubled when the retry
            # policy grants one bounded re-run — so clients calling
            # bot_relay.deliver must tolerate ~1320s before assuming failure.
            with acquire_turn_lock(root, resolved):
                # File-backed capture bounds gateway memory even if a broken
                # CLI floods stdout/stderr.  The persisted relay reply is
                # separately capped before commit.
                with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_f, tempfile.TemporaryFile(
                    mode="w+", encoding="utf-8"
                ) as stderr_f:
                    proc = subprocess.run(
                        local_delivery_command(resolved, tmp),
                        stdout=stdout_f,
                        stderr=stderr_f,
                        text=True,
                        timeout=600,
                    )
                    if proc.returncode != 0:
                        # Retry session policy (#93091 item 5): transient
                        # classes re-run the SAME session once;
                        # context_overflow also re-runs the same session — the
                        # retried turn's pre-API compaction pass
                        # (agent/conversation_loop.py) compacts the
                        # over-threshold Bot Chat transcript first, which is
                        # the sanctioned compression lever (no fresh session
                        # is ever minted). Auth/quota/config classes never
                        # retry.
                        from tools.bot_failure_reasons import (
                            RETRY_NONE,
                            classify_agent_error,
                            retry_action,
                        )

                        stdout_f.seek(0)
                        stderr_f.seek(0)
                        first_stdout = stdout_f.read(200_001)
                        first_stderr = stderr_f.read(2_001)
                        # Lightweight behavior tests and third-party wrappers
                        # may carry captured strings on the result even when
                        # file handles were supplied — honor them here too so
                        # the retry decision sees the same detail the final
                        # read does.
                        if isinstance(getattr(proc, "stdout", None), str):
                            first_stdout = proc.stdout[:200_001]
                        if isinstance(getattr(proc, "stderr", None), str):
                            first_stderr = proc.stderr[:2_001]
                        first_detail = (first_stderr or first_stdout or "").strip()[-500:]
                        if retry_action(classify_agent_error(first_detail)) != RETRY_NONE:
                            stdout_f.truncate(0)
                            stderr_f.truncate(0)
                            stdout_f.seek(0)
                            stderr_f.seek(0)
                            proc = subprocess.run(
                                local_delivery_command(resolved, tmp),
                                stdout=stdout_f,
                                stderr=stderr_f,
                                text=True,
                                timeout=600,
                            )
                    stdout_f.seek(0)
                    stderr_f.seek(0)
                    stdout = stdout_f.read(200_001)
                    stderr = stderr_f.read(2_001)
                    # Lightweight behavior tests and third-party wrappers may
                    # return a CompletedProcess-like object carrying captured
                    # strings even when file handles were supplied.
                    if isinstance(getattr(proc, "stdout", None), str):
                        stdout = proc.stdout[:200_001]
                    if isinstance(getattr(proc, "stderr", None), str):
                        stderr = proc.stderr[:2_001]
        finally:
            # If the payload write raised, the fdopen context never closed
            # the fd — close it explicitly so the unlink below is not blocked
            # by an open handle on Windows.
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if proc.returncode != 0:
            from tools.bot_failure_reasons import classify_agent_error

            raw_detail = (stderr or stdout or "").strip()[-500:]
            detail = " ".join(
                "".join(ch if ch.isprintable() else " " for ch in raw_detail).split()
            )
            if is_v2:
                return finish_receipt(
                    "failed",
                    error=f"delivery turn failed: {detail or proc.returncode}",
                )
            return _err(
                rid,
                5092,
                f"delivery turn failed: {detail or proc.returncode}",
                data={"reason": classify_agent_error(detail)},
            )
        reply = stdout.strip()
        if len(stdout) > 200_000:
            if is_v2:
                return finish_receipt(
                    "failed",
                    error="delivery reply exceeded relay limit",
                )
            return _err(rid, 5092, "delivery reply exceeded relay limit")
        if is_v2:
            return finish_receipt("completed", reply=reply)
        return _ok(rid, {"reply": reply})
    except subprocess.TimeoutExpired:
        if is_v2:
            return finish_unknown(
                "delivery turn timed out; target side effects are indeterminate"
            )
        return _err(rid, 5093, "delivery turn timed out")
    except InboxConflict as e:
        return _err(rid, 4098, str(e))
    except ValueError as e:
        if is_v2 and execution_token:
            return finish_unknown()
        return _err(rid, 4098, str(e))
    except Exception as e:
        # 'target_busy' extends the #93091 item-1 structured refusal enum.
        if getattr(e, "reason", "") == "target_busy":
            return _err(rid, 5096, str(e))
        if is_v2 and execution_token:
            return finish_unknown()
        return _err(rid, 5094, str(e))


@method("bot_relay.reply")
def _(rid, params: dict) -> dict:
    """Write a relayed reply (or delivery error) for a sender-side waiter.

    Params: ``id`` (envelope id), ``reply`` and/or ``error``, optional
    ``reason`` (typed failure code, see ``tools.bot_failure_reasons``).
    """
    envelope_id = str(params.get("id") or "").strip()
    if not envelope_id:
        return _err(rid, 4093, "id required")
    try:
        import os
        from pathlib import Path

        from tools.bot_relay import write_reply

        home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
        root = home.parent.parent if home.parent.name == "profiles" else home
        write_reply(
            root,
            envelope_id,
            reply=str(params.get("reply") or ""),
            error=str(params.get("error") or ""),
            reason=str(params.get("reason") or ""),
        )
        return _ok(rid, {"ok": True})
    except ValueError as e:
        return _err(rid, 4094, str(e))
    except Exception as e:
        return _err(rid, 5095, str(e))


def register(server) -> None:
    _registry.install(server)
