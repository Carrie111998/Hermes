"""Contract tests: behaviour an upstream merge must not silently remove.

Every assertion here corresponds to a defect this audit actually found and
fixed. They exist because the ordinary suite did NOT catch these: a gate whose
hook never fires, a config knob read through the wrong signature, or a path
helper that builds a phantom directory all keep their unit tests green while
being completely inert in production.

These are deliberately *structural* checks (imports, signatures, emit sites,
wiring) rather than behavioural ones. A conflict resolution that takes an
upstream file wholesale is the failure mode being defended against, and that
kind of loss is invisible to behavioural tests which never run the lost code.

Run these first in CI. If one fails after an update, the update removed a
protection — do not "fix" the test, restore the behaviour.
"""

from __future__ import annotations

import inspect
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HERMES_HOME = REPO.parent
PROFILE = HERMES_HOME / "profiles" / "aletheon"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ── 1. SOUL.md must still reach the system prompt ────────────────────────────

def test_soul_md_is_still_loaded_into_the_identity_slot():
    """SOUL.md is the behavioural constitution. If it stops loading, Hermes
    silently reverts to DEFAULT_AGENT_IDENTITY and every profile rule is gone."""
    from agent import system_prompt as sp

    source = inspect.getsource(sp.build_system_prompt_parts)
    assert "load_soul_md" in source, "system prompt no longer loads SOUL.md"
    assert "DEFAULT_AGENT_IDENTITY" in source, "identity fallback removed"

    from agent import prompt_builder as pb

    assert callable(pb.load_soul_md)
    assert "SOUL.md" in inspect.getsource(pb.load_soul_md)


def test_soul_is_placed_before_injected_guidance():
    """Identity must lead the stable tier; guidance blocks are appended after."""
    from agent import system_prompt as sp

    src = inspect.getsource(sp.build_system_prompt_parts)
    soul_at = src.index("load_soul_md")
    guidance_at = src.index("HERMES_AGENT_HELP_GUIDANCE")
    assert soul_at < guidance_at, "SOUL.md must be assembled before guidance blocks"


# ── 2. the pre_llm_call kwarg contract two gates depend on ───────────────────

PRE_LLM_CALL_REQUIRED_KWARGS = (
    "session_id",            # compaction-guard keys sessions on this
    "user_message",          # feedback-gate reads the operator's text here
    "conversation_history",  # compaction-guard derives message_count from this
    "sender_id",             # feedback-gate + stop-gate authorise on this
)


def test_pre_llm_call_still_sends_the_kwargs_plugins_depend_on():
    """Two profile gates were dead for their whole lifetime because they keyed
    on kwarg names the runtime never sent. Pin the emit-site contract."""
    source = _read(REPO / "agent" / "turn_context.py")
    match = re.search(r'_invoke_hook\(\s*["\']pre_llm_call["\'](.*?)\)\n', source, re.S)
    assert match, "the pre_llm_call emit site disappeared from agent/turn_context.py"
    call = match.group(1)
    for kwarg in PRE_LLM_CALL_REQUIRED_KWARGS:
        assert f"{kwarg}=" in call, (
            f"pre_llm_call no longer sends {kwarg!r} — profile gates keyed on it "
            f"will silently stop firing"
        )


def test_exactly_one_pre_llm_call_emit_site():
    """A second emit site with different kwargs would revive the original bug
    in a form the first test cannot see."""
    # The call spans several lines, so match across newlines rather than
    # scanning line-by-line.
    pattern = re.compile(r'invoke_hook\(\s*["\']pre_llm_call["\']', re.S)
    hits = [
        f"{path.relative_to(REPO)}"
        for path in (REPO / "agent").rglob("*.py")
        for _ in pattern.finditer(_read(path))
    ]
    assert len(hits) == 1, f"expected exactly 1 pre_llm_call emit site, found {hits}"


# ── 3. cfg_get's signature — the silent-config-discard trap ──────────────────

def test_cfg_get_still_takes_cfg_plus_varargs():
    """Three plugins passed a dotted string, which binds to `cfg` and returns
    None for every lookup. If the signature ever changes to accept a dotted
    string, the plugin call sites must change with it."""
    from hermes_cli.config import cfg_get

    params = list(inspect.signature(cfg_get).parameters.values())
    assert params[0].name == "cfg"
    assert any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params), (
        "cfg_get no longer takes *keys — plugin call sites must be revisited"
    )
    assert cfg_get("plugins.x.y") is None, (
        "a dotted string now resolves; plugin call sites relying on (cfg, *keys) "
        "must be re-checked"
    )
    assert cfg_get({"a": {"b": 7}}, "a", "b") == 7


# ── 4. no profile-path double-append (the phantom receipt tree) ──────────────

@pytest.mark.parametrize("rel", [
    "plugins/execution-receipts/execution_receipts.py",
    "plugins/compaction-guard/compaction_guard.py",
    "plugins/skill-progressive-disclosure/plugin.py",
])
def test_profile_dir_helpers_do_not_blindly_append_profiles(rel):
    """In profile mode HERMES_HOME already IS the profile dir. Appending
    'profiles/<name>' again sent the receipt store and its HMAC key into a
    phantom tree nothing audits."""
    path = PROFILE / rel
    if not path.exists():
        pytest.skip(f"{rel} not present in this profile")
    src = _read(path)
    assert '"profiles" / profile' in src or "'profiles' / profile" in src, (
        f"{rel}: _profile_dir shape changed — re-verify the phantom-path guard"
    )
    assert 'parent.name == "profiles"' in src, (
        f"{rel}: lost the guard that detects HERMES_HOME already being a profile dir"
    )


# ── 5. git output decoding — the fail-open path checks ───────────────────────

def test_worker_bridge_git_runner_decodes_utf8_explicitly():
    """text=True without encoding= decodes git's UTF-8 with the Windows ANSI
    codepage, corrupting non-ASCII paths so forbidden_paths stops matching and
    escape detection cannot resolve — both fail OPEN."""
    path = PROFILE / "plugins" / "bob" / "bob_core" / "bridge" / "workspace.py"
    if not path.exists():
        pytest.skip("bob worker-bridge not present")
    src = _read(path)
    run_fn = src[src.index("def _run("):]
    run_fn = run_fn[:run_fn.index("\ndef ")]
    assert 'encoding="utf-8"' in run_fn, "_run lost its explicit utf-8 decoding"
    assert "errors=" in run_fn, "_run lost its decode-error policy"


# ── 6. windows lock must not read a mandatory-locked byte ────────────────────

def test_alert_queue_lock_does_not_read_byte_zero_before_locking():
    """Windows byte-range locks are mandatory: reading byte 0 while another
    holder has it locked raises PermissionError instead of blocking, which
    killed the alert sync thread and dropped alerts."""
    path = PROFILE / "plugins" / "worker-alert-gate" / "alert_core.py"
    if not path.exists():
        pytest.skip("worker-alert-gate not present")
    src = _read(path)
    body = src[src.index("def _queue_lock("):]
    body = body[:body.index("\ndef ")]
    # Anchor on the ACQUISITION, not the bare name: the function's own comment
    # mentions "msvcrt.locking", and matching that truncated the searched
    # region to the docstring and made this assertion vacuous.
    acquire = re.search(r"msvcrt\.locking\(\s*lock_file\.fileno\(\)\s*,\s*msvcrt\.LK_LOCK", body)
    assert acquire, "could not locate the LK_LOCK acquisition in _queue_lock"
    pre_lock = body[:acquire.start()]
    assert ".read(" not in pre_lock, (
        "_queue_lock reads the lock byte before acquiring the lock — "
        "Windows byte-range locks are mandatory, so this is the PermissionError "
        "that killed the alert sync thread and dropped alerts"
    )


# ── 7. delegation must work on the interpreter actually running ──────────────

def test_daemon_pool_submits_on_this_interpreter():
    """tools/daemon_pool.py mirrors a private stdlib function whose signature
    changed in 3.14. Every delegate_task dies if the mirror is wrong."""
    from concurrent.futures.thread import _threads_queues

    from tools.daemon_pool import DaemonThreadPoolExecutor

    pool = DaemonThreadPoolExecutor(max_workers=2)
    try:
        assert [f.result(timeout=30) for f in [pool.submit(lambda i=i: i * 2) for i in range(4)]] == [0, 2, 4, 6]
        threads = list(pool._threads)
        assert threads, "no worker threads were created"
        assert all(t.daemon for t in threads), "workers are not daemon — they will block exit"
        assert all(t not in _threads_queues for t in threads), (
            "workers re-registered in _threads_queues — the atexit hook will join them"
        )
    finally:
        pool.shutdown(wait=True)


# ── 8. the runtime guard stays wired ─────────────────────────────────────────

def test_runtime_guard_is_importable_and_wired():
    from hermes_cli import runtime_guard

    assert callable(runtime_guard.enforce)
    main_src = _read(REPO / "hermes_cli" / "main.py")
    assert "runtime_guard" in main_src, "CLI no longer imports the runtime guard"
    assert re.search(r"_enforce_runtime\s*\(", main_src), "runtime guard is never invoked"


def test_supported_python_range_is_enforced_not_just_declared():
    from hermes_cli.runtime_guard import python_supported

    assert not python_supported((3, 14)), "3.14 must be rejected"
    assert not python_supported((3, 10)), "3.10 must be rejected"
    assert python_supported((3, 13)), "3.13 must be accepted"


# ── 9. vulnerable SQLite must remain detectable ──────────────────────────────

def test_sqlite_wal_reset_predicate_still_exists_and_is_correct():
    """Upstream's own predicate. If a merge drops it, every WAL-corruption
    guard downstream silently becomes a no-op."""
    from hermes_state import is_sqlite_wal_reset_vulnerable

    assert is_sqlite_wal_reset_vulnerable((3, 50, 4)) is True
    assert is_sqlite_wal_reset_vulnerable((3, 51, 2)) is True
    assert is_sqlite_wal_reset_vulnerable((3, 51, 3)) is False
    assert is_sqlite_wal_reset_vulnerable((3, 50, 7)) is False, "3.50.7 backport"
    assert is_sqlite_wal_reset_vulnerable((3, 53, 1)) is False


def test_this_runtime_is_supported_and_not_wal_vulnerable():
    """The interpreter running the suite must be one we actually support."""
    from hermes_cli.runtime_guard import python_supported
    from hermes_state import is_sqlite_wal_reset_vulnerable

    assert python_supported(), f"suite running on unsupported Python {sys.version.split()[0]}"
    assert not is_sqlite_wal_reset_vulnerable(), (
        f"suite running against WAL-vulnerable SQLite {sqlite3.sqlite_version}"
    )


# ── 10. plugin hooks must still be dispatched at all ─────────────────────────

def test_plugin_hook_dispatch_still_exists():
    """A registry with no dispatcher makes every gate in the profile inert."""
    from hermes_cli import plugins as plugin_mod

    assert hasattr(plugin_mod, "invoke_hook"), "hermes_cli.plugins.invoke_hook is gone"
    assert callable(plugin_mod.invoke_hook)
