"""Shared fixtures for the hermes-agent test suite.

Hermetic-test invariants enforced here (see AGENTS.md for rationale):

1. **No credential env vars.** All provider/credential-shaped env vars
   (ending in _API_KEY, _TOKEN, _SECRET, _PASSWORD, _CREDENTIALS, etc.)
   are unset before every test. Local developer keys cannot leak in.
2. **Isolated HERMES_HOME.** HERMES_HOME points to a per-test tempdir so
   code reading ``~/.hermes/*`` via ``get_hermes_home()`` can't see the
   real one. (We do NOT also redirect HOME — that broke subprocesses in
   CI. Code using ``Path.home() / ".hermes"`` instead of the canonical
   ``get_hermes_home()`` is a bug to fix at the callsite.)
3. **Deterministic runtime.** TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0.
4. **No HERMES_SESSION_* inheritance** — the agent's current gateway
   session must not leak into tests.

These invariants make the local test run match CI closely. Gaps that
remain (CPU count, xdist worker count) are addressed by the canonical
test runner at ``scripts/run_tests.sh``.
"""

import asyncio
import gc
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Per-test timeouts must fail ONE test, not take the whole run down.
# pytest-timeout's ``thread`` method (pinned in pyproject.toml because
# signal.SIGALRM is Unix-only) answers a timeout with ``os._exit(1)``: no
# summary line, and a failure set that depends on where the process died.
# Re-exporting this hook implementation here registers it for the whole suite.
# See tests/_nonfatal_timeout.py.
from tests._nonfatal_timeout import pytest_timeout_set_timer  # noqa: E402,F401

# ``sys.path`` as the RUNNER handed it to us, snapshotted before pytest has
# imported a single test module. Everything here was put there deliberately by
# the harness -- PYTHONPATH, the venv, plugin dirs -- so it is the baseline the
# live-checkout leak is measured against, exactly as ``_no_live_checkout_on_sys_path``
# below measures per-test deltas rather than absolutes.
#
# This box's mandated long-run wrapper (``~/.hermes/ops/pytest-run.cmd``) and
# BOTH cron gates (``hermes_repo_test_gate.py``, ``nightly_gate.py``) set
# ``PYTHONPATH=~/.hermes/ops`` so ``-p pytest_fd_guard`` can be imported. That
# directory is inside the live ``~/.hermes`` and outside this checkout, so an
# absolute assertion flags the guard itself and every guarded run goes red.
SYS_PATH_AT_IMPORT = tuple(sys.path)


# ── HERMES_HOME must be pinned at IMPORT time, not per test ────────────────
# The autouse ``_hermetic_environment`` fixture below redirects HERMES_HOME for
# every test, but a fixture is function-scoped: it cannot run until collection
# has already imported every test module. Several production modules do real
# work to the Hermes home *at import*, so by the time the first fixture fires,
# the damage is done and is process-wide:
#
#   * ``tools/approval.py`` ends with a module-level ``load_permanent_allowlist()``
#     -> ``load_config()`` -> ``ensure_hermes_home()``, which mkdir+chmods the
#     whole live tree (``cron/ sessions/ logs/ logs/curator/ memories/ pairing/
#     hooks/ image_cache/ audio_cache/ skills/``) and the ``~/.hermes`` root.
#   * ``hermes_cli/main.py`` calls ``setup_logging()`` at module level, which
#     attaches a RotatingFileHandler to the ROOT logger pointed at the real
#     ``logs/agent.log`` and sets ``hermes_logging._logging_initialized``. Every
#     later call is then a no-op, so *every log record the rest of the session
#     emits* — from any test — lands in the developer's production log. That is
#     how ``agent.log`` filled with test turns against fabricated providers, and
#     how a ``Shutdown watchdog fired after 0s`` CRITICAL line carrying a
#     ``pytest-of-<user>`` tmpdir path got into ``agent.log.3``/``errors.log.2``,
#     falsifying an absence proof drawn from grepping those logs.
#   * ``hermes_cli/banner.py``'s background update-check thread writes
#     ``profiles/<p>/.update_check``.
#
# Pinning the env var here — before pytest imports a single test module — makes
# all of them resolve into a throwaway directory instead. The per-test fixture
# still narrows HERMES_HOME to ``tmp_path`` for isolation between tests; this
# only closes the window that opens before any test exists.
#
# Verified with an audit hook (``tests/_live_root_audit.py``): 89 writes into
# the real home during collection alone, every one attributed to
# ``<import/collection>`` rather than to any test.
# The developer's real Hermes tree. NOTE this repo lives *inside* it
# (~/.hermes/agent-src/...), so anything comparing against it must exclude
# PROJECT_ROOT or it will reject the checkout under test.
_REAL_HERMES_ROOT = Path(os.path.expanduser("~")).resolve() / ".hermes"

_SESSION_HERMES_HOME = None


def _pin_hermes_home_before_collection():
    """Point HERMES_HOME at a throwaway dir unless it is already redirected."""
    global _SESSION_HERMES_HOME

    real_root = _REAL_HERMES_ROOT
    current = os.environ.get("HERMES_HOME")
    if current:
        try:
            resolved = Path(current).resolve()
        except OSError:
            resolved = None
        # Respect a HERMES_HOME the caller already redirected (CI, docker,
        # a deliberate profile run). Only override one that still resolves
        # into the developer's live tree -- or, of course, an unset one.
        if resolved is not None and resolved != real_root and real_root not in resolved.parents:
            return

    session_home = Path(tempfile.mkdtemp(prefix="hermes-collection-home-"))
    # Same layout ensure_hermes_home() would create, so import-time code that
    # expects these to exist finds them without touching the real tree.
    for subdir in (
        "cron", "sessions", "logs", "logs/curator", "memories",
        "pairing", "hooks", "image_cache", "audio_cache", "skills",
    ):
        (session_home / subdir).mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(session_home)
    _SESSION_HERMES_HOME = session_home


_pin_hermes_home_before_collection()


# ── importorskip husk guard ────────────────────────────────────────────────
# ``pytest.importorskip`` treats "the import worked" as "the package is
# usable". A distribution whose FILES were deleted but whose DIRECTORIES
# survive is imported by Python as an implicit PEP-420 namespace package: the
# import SUCCEEDS, with ``__file__ = None`` and nothing inside. pytest cannot
# see this — it explicitly suppresses the very ImportWarning that would reveal
# it ("ignore ImportWarnings that might happen because of existing directories
# with the same name we're trying to import but without a __init__.py",
# _pytest/outcomes.py) — and ``minversion`` is the only built-in escape.
#
# Cost of not guarding it, measured on 2026-08-17: a gutted numpy left 8 empty
# dirs in site-packages with no dist-info. ``importorskip("numpy")`` passed, so
# 8 tests that should have SKIPPED failed instead — and worse, the husk then sat
# in ``sys.modules`` where ``_pytest.python_api._as_numpy_array`` finds it
# ("and numpy is already imported") and calls ``np.isscalar``, so every
# ``pytest.approx`` for the rest of the session raised. That took down 14 more
# tests across four files with no numpy in them: 22 failures, one broken install.
#
# Wrapped once here — like ``sqlite3.connect`` below — so every call site in the
# suite is covered without editing any of them.
_real_importorskip = pytest.importorskip


def _is_empty_namespace_package(mod: object) -> bool:
    """True for a directory-only husk: a namespace package exposing nothing.

    A *legitimate* PEP-420 namespace package also has ``__file__ is None``, so
    that test alone would produce false positives. Require in addition that the
    module exposes no public attribute at all — a working package always
    exposes something, and a husk never does.
    """
    if getattr(mod, "__file__", None) is not None:
        return False
    if getattr(mod, "__path__", None) is None:
        return False
    return not [name for name in dir(mod) if not name.startswith("_")]


def _importorskip_rejecting_husks(modname, *args, **kwargs):
    mod = _real_importorskip(modname, *args, **kwargs)
    if _is_empty_namespace_package(mod):
        # Drop it from sys.modules, or pytest.approx (and anything else that
        # feature-detects via sys.modules) keeps finding the husk all session.
        for name in [modname, *[m for m in sys.modules if m.startswith(f"{modname}.")]]:
            sys.modules.pop(name, None)
        pytest.skip(
            f"{modname!r} resolved to an empty namespace package at "
            f"{getattr(mod, '__path__', None)} — the distribution is partially "
            "uninstalled, not installed. Reinstall it, or delete the leftover "
            "directory so the import fails cleanly.",
            allow_module_level=True,
        )
    return mod


pytest.importorskip = _importorskip_rejecting_husks


# ── sqlite-connection tripwire (feeds pytest_runtest_teardown) ──────────────
# ``sqlite3.connect`` is wrapped once, here, so the teardown hook below knows
# whether a test could have left a cycle-held connection behind — see that
# hook's docstring for why the collect exists and what it costs.
#
# Wrapping the module attribute is enough: no module in this repo does
# ``from sqlite3 import connect`` (checked 2026-08-13), and the third-party
# drivers that matter (aiosqlite, sqlalchemy) look the attribute up at call
# time too. A caller that captured the original function *before* this
# conftest was imported would go uncounted — conftest import happens before any
# test module is imported, so that window is closed in practice. If a new
# import style appears, the failure mode is the pre-2026-07 one (a %TEMP% dir
# survives), not a wrong test result.
_sqlite_opened_since_collect = 0
_collect_armed = False


# ── live-state-db guard (rides the same wrapper) ────────────────────────────
# ``~/.hermes/state.db`` is the LIVE gateway/session database for this machine.
# A test that opens it can corrupt real sessions, and — because sqlite takes
# locks — can wedge the running gateway. Nothing in the suite is supposed to
# reach it: ``_hermetic_environment`` redirects HERMES_HOME to a per-test
# tempdir, and every production path resolves through
# ``hermes_state.py:229`` (``get_hermes_home() / "state.db"``). So an open of
# the live file means the isolation FAILED — a leaked env var, a module that
# cached the path at import time before the fixture ran, or a hardcoded path.
#
# That failure is silent today: the test passes, having read or written real
# data. This turns it into a loud, immediate RuntimeError.
#
# It rides the existing ``sqlite3.connect`` wrapper rather than adding a second
# one, for the reason stated above: one wrapper covers every call site in the
# suite without editing any of them, including connections opened at module
# import / collection time, which no fixture can see.
#
# The canonical root is ``~/.hermes`` and is NEVER profile-scoped (CLAUDE.md,
# "Notification Layer"), so it is derived from the home directory rather than
# from HERMES_HOME — which the fixtures deliberately move.
_LIVE_STATE_DB_BYPASS_MARK = "live_state_db_bypass"
_allow_live_state_db = [False]


def _normalize_db_target(value) -> str | None:
    """Collapse a ``sqlite3.connect`` first argument onto a comparison key.

    Returns None for anything that cannot name the live file (``:memory:``,
    bytes that will not decode, a path that cannot be resolved). Comparison is
    ``normcase(realpath(...))`` so a drive-case difference, a forward/backslash
    mix, a relative path, or a symlink/junction cannot walk around the guard.
    """
    try:
        raw = os.fspath(value)
    except TypeError:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode()
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str) or not raw or raw == ":memory:":
        return None
    if raw.startswith("file:"):
        # URI form (``uri=True``): file:/C:/x/state.db?mode=ro&cache=shared
        raw = raw[len("file:") :].split("?", 1)[0]
        if "%" in raw:
            from urllib.parse import unquote

            raw = unquote(raw)
        # file:///C:/... -> /C:/... ; strip the slash before a drive letter.
        if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
        if not raw:
            return None
    try:
        return os.path.normcase(os.path.realpath(raw))
    except (OSError, ValueError):
        return None


def _live_state_db_targets() -> frozenset:
    """The live state.db and its sidecars, as normalized comparison keys."""
    root = os.path.join(os.path.expanduser("~"), ".hermes")
    names = ("state.db", "state.db-wal", "state.db-shm")
    keys = (_normalize_db_target(os.path.join(root, n)) for n in names)
    return frozenset(k for k in keys if k)


_LIVE_STATE_DB_TARGETS = _live_state_db_targets()


def _reject_live_state_db(target) -> None:
    if _allow_live_state_db[0]:
        return
    key = _normalize_db_target(target)
    if key is None or key not in _LIVE_STATE_DB_TARGETS:
        return
    raise RuntimeError(
        "live-state-db guard: a test tried to open the LIVE machine database "
        f"{target!r}.\n"
        "That file is the running gateway's session store -- opening it can "
        "corrupt real sessions and can wedge the live gateway on a sqlite "
        "lock.\n"
        "This means HERMES_HOME isolation did not reach this code path. Usual "
        "causes: the module cached the db path at import time (before the "
        "autouse _hermetic_environment fixture ran), a hardcoded "
        "os.path.expanduser('~')/.hermes path, or an env var the test set "
        "itself.\n"
        "Fix the path resolution so it goes through get_hermes_home() at CALL "
        "time, or point the test at tmp_path. Only if a real live-DB read is "
        "genuinely the thing under test, mark it with "
        f"@pytest.mark.{_LIVE_STATE_DB_BYPASS_MARK}."
    )


def _install_sqlite_open_tripwire() -> None:
    import sqlite3

    if getattr(sqlite3.connect, "_hermes_counted", False):
        return
    _real_connect = sqlite3.connect

    def _counting_connect(*args, **kwargs):
        global _sqlite_opened_since_collect
        # Refuse BEFORE counting: a rejected open never happened, so it must
        # not arm the teardown gc pass.
        _reject_live_state_db(args[0] if args else kwargs.get("database"))
        _sqlite_opened_since_collect += 1
        return _real_connect(*args, **kwargs)

    _counting_connect._hermes_counted = True
    sqlite3.connect = _counting_connect
    sqlite3.dbapi2.connect = _counting_connect


_install_sqlite_open_tripwire()


# ── Per-file process isolation ──────────────────────────────────────────────
# Tests run via ``scripts/run_tests_parallel.py``, which spawns a fresh
# ``python -m pytest <file>`` subprocess per test file. Cross-file state
# leakage (module-level dicts, ContextVars, caches) is impossible: each
# file gets a clean Python interpreter. Intra-file ordering is the test
# author's responsibility — if test A in foo.py mutates state that test B
# in foo.py reads, that's a real bug to fix in the file (it would also
# bite anyone running ``pytest tests/foo.py`` directly).
#
# This replaces the historic _reset_module_state autouse fixture (manual
# state clearing) and the brief experiment with subprocess-per-test
# isolation (too slow at ~17k tests).
#
# See ``scripts/run_tests_parallel.py`` for the runner.


# ── Credential env-var filter ──────────────────────────────────────────────
#
# Any env var in the current process matching ONE of these patterns is
# unset for every test. Developers' local keys cannot leak into assertions
# about "auto-detect provider when key present".

_CREDENTIAL_SUFFIXES = (
    "_API_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIALS",
    "_ACCESS_KEY",
    "_SECRET_ACCESS_KEY",
    "_PRIVATE_KEY",
    "_OAUTH_TOKEN",
    "_WEBHOOK_SECRET",
    "_ENCRYPT_KEY",
    "_APP_SECRET",
    "_CLIENT_SECRET",
    "_CORP_SECRET",
    "_AES_KEY",
)

# Explicit names (for ones that don't fit the suffix pattern)
_CREDENTIAL_NAMES = frozenset({
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ANTHROPIC_TOKEN",
    "FAL_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "NOUS_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "GLM_API_KEY",
    "ZAI_API_KEY",
    "MINIMAX_API_KEY",
    "OLLAMA_API_KEY",
    "OPENVIKING_API_KEY",
    "COPILOT_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "BROWSERBASE_API_KEY",
    "FIRECRAWL_API_KEY",
    "PARALLEL_API_KEY",
    "EXA_API_KEY",
    "TAVILY_API_KEY",
    "WANDB_API_KEY",
    "ELEVENLABS_API_KEY",
    "HONCHO_API_KEY",
    "MEM0_API_KEY",
    "SUPERMEMORY_API_KEY",
    "RETAINDB_API_KEY",
    "HINDSIGHT_API_KEY",
    "HINDSIGHT_LLM_API_KEY",
    "DAYTONA_API_KEY",
    "TWILIO_AUTH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "MATTERMOST_TOKEN",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_PASSWORD",
    "MATRIX_RECOVERY_KEY",
    "HASS_TOKEN",
    "EMAIL_PASSWORD",
    "BLUEBUBBLES_PASSWORD",
    "FEISHU_APP_SECRET",
    "FEISHU_ENCRYPT_KEY",
    "FEISHU_VERIFICATION_TOKEN",
    "DINGTALK_CLIENT_SECRET",
    "QQ_CLIENT_SECRET",
    "QQ_STT_API_KEY",
    "WECOM_SECRET",
    "WECOM_CALLBACK_CORP_SECRET",
    "WECOM_CALLBACK_TOKEN",
    "WECOM_CALLBACK_ENCODING_AES_KEY",
    "WEIXIN_TOKEN",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "TERMINAL_SSH_KEY",
    "SUDO_PASSWORD",
    "GATEWAY_PROXY_KEY",
    "API_SERVER_KEY",
    "TOOL_GATEWAY_USER_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "WEBHOOK_SECRET",
    "VOICE_TOOLS_OPENAI_KEY",
    "BROWSER_USE_API_KEY",
    "CUSTOM_API_KEY",
    "GATEWAY_PROXY_URL",
    "GEMINI_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENROUTER_BASE_URL",
    "OLLAMA_BASE_URL",
    "GROQ_BASE_URL",
    "XAI_BASE_URL",
    "ANTHROPIC_BASE_URL",
})


def _looks_like_credential(name: str) -> bool:
    """True if env var name matches a credential-shaped pattern."""
    if name in _CREDENTIAL_NAMES:
        return True
    return any(name.endswith(suf) for suf in _CREDENTIAL_SUFFIXES)


# HERMES_* vars that change test behavior by being set. Unset all of these
# unconditionally — individual tests that need them set do so explicitly.
_HERMES_BEHAVIORAL_VARS = frozenset({
    "HERMES_YOLO_MODE",
    "HERMES_INTERACTIVE",
    "HERMES_QUIET",
    "HERMES_TOOL_PROGRESS",
    "HERMES_TOOL_PROGRESS_MODE",
    "HERMES_MAX_ITERATIONS",
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_NAME",
    "HERMES_SESSION_THREAD_ID",
    "HERMES_SESSION_SOURCE",
    "HERMES_SESSION_KEY",
    "HERMES_GATEWAY_SESSION",
    "HERMES_CRON_SESSION",
    "_HERMES_GATEWAY",
    "HERMES_PLATFORM",
    "HERMES_MODEL",
    "HERMES_INFERENCE_MODEL",
    "HERMES_INFERENCE_PROVIDER",
    "HERMES_TUI_PROVIDER",
    "HERMES_MANAGED",
    "HERMES_MANAGED_DIR",
    "HERMES_DEV",
    "HERMES_CONTAINER",
    "HERMES_EPHEMERAL_SYSTEM_PROMPT",
    "HERMES_TIMEZONE",
    "HERMES_REDACT_SECRETS",
    "HERMES_BACKGROUND_NOTIFICATIONS",
    "HERMES_EXEC_ASK",
    "HERMES_HOME_MODE",
    "HERMES_AGENT_USE_LEGACY_SESSION_KEYS",
    # Desktop cron-ticker mode override (0=never tick, 1=always tick); an
    # ambient value would flip the defer-to-gateway guard tests' auto mode.
    "HERMES_DESKTOP_CRON",
    # Kanban path/board pins must never leak from a developer shell or
    # dispatched worker into tests; otherwise tests can write fake tasks to
    # the real ~/.hermes/kanban.db instead of the per-test HERMES_HOME.
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_LOGS_ROOT",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_DISPATCH_IN_GATEWAY",
    "HERMES_TENANT",
    # Honcho host selection changes which nested config block wins. A local
    # shell override leaked "myhost" into the full suite and flipped 20
    # otherwise-unrelated config tests away from the default "hermes" host.
    "HERMES_HONCHO_HOST",
    # Dashboard OAuth auth gate (PR #30156). When set, the bundled
    # dashboard-auth `nous` plugin auto-registers itself on plugin discovery,
    # which is triggered by any `/api/status` call. That leaks a provider
    # into the dashboard_auth registry across tests in the same worker and
    # makes assertions like `auth_providers == []` flaky. CI never sets
    # these, so production tests must not see them either.
    "HERMES_DASHBOARD_OAUTH_CLIENT_ID",
    "HERMES_DASHBOARD_PORTAL_URL",
    "TERMINAL_CWD",
    "TERMINAL_ENV",
    "TERMINAL_CONTAINER_CPU",
    "TERMINAL_CONTAINER_DISK",
    "TERMINAL_CONTAINER_MEMORY",
    "TERMINAL_CONTAINER_PERSISTENT",
    "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES",
    "TERMINAL_DOCKER_ORPHAN_REAPER",
    "TERMINAL_DOCKER_RUN_AS_HOST_USER",
    "BROWSER_CDP_URL",
    "CAMOFOX_URL",
    # Platform allowlists — not credentials, but if set from any source
    # (user shell, earlier leaky test, CI env), they change gateway auth
    # behavior and flake button-authorization tests.
    "TELEGRAM_ALLOWED_USERS",
    "DISCORD_ALLOWED_USERS",
    "WHATSAPP_ALLOWED_USERS",
    "SLACK_ALLOWED_USERS",
    "SIGNAL_ALLOWED_USERS",
    "SIGNAL_GROUP_ALLOWED_USERS",
    "EMAIL_ALLOWED_USERS",
    "SMS_ALLOWED_USERS",
    "MATTERMOST_ALLOWED_USERS",
    "MATRIX_ALLOWED_USERS",
    "DINGTALK_ALLOWED_USERS",
    "FEISHU_ALLOWED_USERS",
    "WECOM_ALLOWED_USERS",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_ALLOW_ALL_USERS",
    "TELEGRAM_ALLOW_ALL_USERS",
    "DISCORD_ALLOW_ALL_USERS",
    "WHATSAPP_ALLOW_ALL_USERS",
    "SLACK_ALLOW_ALL_USERS",
    "SIGNAL_ALLOW_ALL_USERS",
    "EMAIL_ALLOW_ALL_USERS",
    "SMS_ALLOW_ALL_USERS",
    # Gateway home channels are set by /sethome in real profiles. Tests that
    # exercise dashboard notification toggles must opt in explicitly or they
    # can accidentally subscribe against a developer's real home channel.
    "TELEGRAM_HOME_CHANNEL",
    "TELEGRAM_HOME_CHANNEL_THREAD_ID",
    "TELEGRAM_HOME_CHANNEL_NAME",
    "TELEGRAM_CRON_THREAD_ID",
    "DISCORD_HOME_CHANNEL",
    "DISCORD_HOME_CHANNEL_THREAD_ID",
    "DISCORD_HOME_CHANNEL_NAME",
    "SLACK_HOME_CHANNEL",
    "SLACK_HOME_CHANNEL_THREAD_ID",
    "SLACK_HOME_CHANNEL_NAME",
    "WHATSAPP_HOME_CHANNEL",
    "WHATSAPP_HOME_CHANNEL_THREAD_ID",
    "WHATSAPP_HOME_CHANNEL_NAME",
    "SIGNAL_HOME_CHANNEL",
    "SIGNAL_HOME_CHANNEL_THREAD_ID",
    "SIGNAL_HOME_CHANNEL_NAME",
    "EMAIL_HOME_CHANNEL",
    "EMAIL_HOME_CHANNEL_THREAD_ID",
    "EMAIL_HOME_CHANNEL_NAME",
    "SMS_HOME_CHANNEL",
    "SMS_HOME_CHANNEL_THREAD_ID",
    "SMS_HOME_CHANNEL_NAME",
    "MATTERMOST_HOME_CHANNEL",
    "MATTERMOST_HOME_CHANNEL_THREAD_ID",
    "MATTERMOST_HOME_CHANNEL_NAME",
    "MATRIX_HOME_CHANNEL",
    "MATRIX_HOME_CHANNEL_THREAD_ID",
    "MATRIX_HOME_CHANNEL_NAME",
    "DINGTALK_HOME_CHANNEL",
    "DINGTALK_HOME_CHANNEL_THREAD_ID",
    "DINGTALK_HOME_CHANNEL_NAME",
    "FEISHU_HOME_CHANNEL",
    "FEISHU_HOME_CHANNEL_THREAD_ID",
    "FEISHU_HOME_CHANNEL_NAME",
    "WECOM_HOME_CHANNEL",
    "WECOM_HOME_CHANNEL_THREAD_ID",
    "WECOM_HOME_CHANNEL_NAME",
    # API server bind/auth settings are common in local gateway profiles and
    # change adapter defaults plus load_gateway_config() enablement. Tests that
    # need them set opt in explicitly with monkeypatch.
    "API_SERVER_ENABLED",
    "API_SERVER_HOST",
    "API_SERVER_PORT",
    "API_SERVER_KEY",
    "API_SERVER_CORS_ORIGINS",
    "API_SERVER_MODEL_NAME",
    # Platform gating — set by load_gateway_config() as a side effect when
    # a config.yaml is present, so individual test bodies that call the
    # loader leak these values into later tests in the same process.
    # Force-clear on every test setup so the leak can't happen.
    "SLACK_REQUIRE_MENTION",
    "SLACK_STRICT_MENTION",
    "SLACK_FREE_RESPONSE_CHANNELS",
    "SLACK_ALLOW_BOTS",
    "SLACK_REACTIONS",
    "DISCORD_REQUIRE_MENTION",
    "DISCORD_FREE_RESPONSE_CHANNELS",
    "TELEGRAM_REQUIRE_MENTION",
    "WHATSAPP_REQUIRE_MENTION",
    "DINGTALK_REQUIRE_MENTION",
    "MATRIX_REQUIRE_MENTION",
})


@pytest.fixture(autouse=True)
def _hermetic_environment(tmp_path, monkeypatch):
    """Blank out all credential/behavioral env vars so local and CI match.

    Also redirects HERMES_HOME to a per-test tempdir so code that reads
    ``~/.hermes/*`` can't touch the real one, and pins TZ/LANG so
    datetime/locale-sensitive tests are deterministic.

    HOME is deliberately *not* redirected here (see step 3). A test that needs
    its home isolated must do it itself via
    ``tests._home_isolation.redirect_home`` — setting ``$HOME`` alone does not
    isolate on Windows.
    """
    # 1. Blank every credential-shaped env var that's currently set.
    for name in list(os.environ.keys()):
        if _looks_like_credential(name):
            monkeypatch.delenv(name, raising=False)

    # 2. Blank behavioral HERMES_* vars that could change test semantics.
    for name in _HERMES_BEHAVIORAL_VARS:
        monkeypatch.delenv(name, raising=False)

    # Honcho's fallback host/config resolution legitimately reads the user's
    # global ~/.honcho/config.json. Keep HOME stable (subprocess tests depend
    # on it), but pin the host so ordinary tests cannot inherit a developer's
    # defaultHost and silently select the wrong nested config block. Tests of
    # custom host resolution override/delete this explicitly.
    monkeypatch.setenv("HERMES_HONCHO_HOST", "hermes")

    # 3. Redirect HERMES_HOME to a per-test tempdir. Code that reads
    #    ``~/.hermes/*`` via ``get_hermes_home()`` now gets the tempdir.
    #
    #    NOTE: We do NOT also redirect HOME. Doing so broke CI because
    #    some tests (and their transitive deps) spawn subprocesses that
    #    inherit HOME and expect it to be stable. If a test genuinely
    #    needs HOME isolated, it should do so in its own fixture via
    #    ``tests._home_isolation.redirect_home`` -- setting ``$HOME`` on
    #    its own is NOT isolation on Windows, where ``Path.home()`` and
    #    ``os.path.expanduser`` read ``USERPROFILE`` and ignore ``HOME``
    #    (which is typically unset there), so the real home still wins.
    #    Any code in the codebase reading ``~/.hermes/*`` via
    #    ``Path.home() / ".hermes"`` instead of ``get_hermes_home()``
    #    is a bug to fix at the callsite.
    fake_hermes_home = tmp_path / "hermes_test"
    fake_hermes_home.mkdir()
    (fake_hermes_home / "sessions").mkdir()
    (fake_hermes_home / "cron").mkdir()
    (fake_hermes_home / "memories").mkdir()
    (fake_hermes_home / "skills").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(fake_hermes_home))

    # 3b. Gateway scope locks are MACHINE-local, not HERMES_HOME-local:
    #     gateway.status._get_lock_dir() resolves
    #     $HERMES_GATEWAY_LOCK_DIR -> $XDG_STATE_HOME/hermes/gateway-locks ->
    #     ~/.local/state/hermes/gateway-locks. The last of those is a
    #     Path.home() call, so redirecting HERMES_HOME does nothing for it and
    #     any test that drives a real platform connect() takes a lock in the
    #     developer's live state dir -- and can collide with a running gateway.
    #     Fifteen-odd tests already set this by hand (tests/gateway/test_status.py,
    #     test_platform_lock_takeover.py, tests/hermes_cli/test_gateway_restart_helpers.py);
    #     tests/gateway/test_qqbot.py did not, and an audit hook caught it
    #     writing ~/.local/state/hermes/gateway-locks/qqbot-appid-*.lock.
    #     Defaulting it here covers the ones nobody remembered. Tests that set
    #     it themselves still win -- their monkeypatch runs after this fixture.
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "gateway_locks"))

    # 4. Deterministic locale / timezone / hashseed. CI runs in UTC with
    #    C.UTF-8 locale; local dev often doesn't. Pin everything.
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("PYTHONHASHSEED", "0")

    # 4b. Disable AWS IMDS lookups. Without this, any test that ends up
    #     calling has_aws_credentials() / resolve_aws_auth_env_var()
    #     (e.g. provider auto-detect, status command, cron run_job) burns
    #     ~2s waiting for the metadata service at 169.254.169.254 to time
    #     out. Tests don't run on EC2 — IMDS is always unreachable here.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_METADATA_SERVICE_TIMEOUT", "1")
    monkeypatch.setenv("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")
    # Tirith auto-installs from GitHub when enabled and missing. Unit tests
    # should never perform that implicit network/bootstrap path; Tirith-specific
    # tests opt back in by patching the security config directly.
    monkeypatch.setenv("TIRITH_ENABLED", "false")

    # 5. Reset plugin singleton so tests don't leak plugins from
    #    ~/.hermes/plugins/ (which, per step 3, is now empty — but the
    #    singleton might still be cached from a previous test).
    try:
        import hermes_cli.plugins as _plugins_mod
        monkeypatch.setattr(_plugins_mod, "_plugin_manager", None)
    except Exception:
        pass
    # Tests should not inherit the agent's current gateway/messaging surface.
    # Individual tests that need gateway behavior set these explicitly.
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    # Avoid making real calls during tests if this key is set in the env files
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # API server platform adapter reads these from the environment when the
    # config dict is empty, so leaking them causes ~50 test_api_server.py
    # failures in the full suite (but not in isolation, which is the tell).
    # Tests that want specific values set them explicitly via monkeypatch.
    monkeypatch.delenv("API_SERVER_HOST", raising=False)
    monkeypatch.delenv("API_SERVER_PORT", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_CORS_ORIGINS", raising=False)
    # Explicitly clear provider-specific base URL overrides that don't match
    # the generic credential-shaped env-var filter above.
    monkeypatch.delenv("GMI_API_KEY", raising=False)
    monkeypatch.delenv("GMI_BASE_URL", raising=False)


# Backward-compat alias — old tests reference this fixture name. Keep it
# as a no-op wrapper so imports don't break.
@pytest.fixture(autouse=True)
def _isolate_hermes_home(_hermetic_environment):
    """Alias preserved for any test that yields this name explicitly."""
    return None


# ── Module-level state reset — replaced by per-file process isolation ──────
#
# Python modules are singletons per process, and pytest-xdist workers are
# long-lived. Module-level dicts/sets (tool registries, approval state,
# interrupt flags) and ContextVars persist across tests in the same worker,
# causing tests that pass alone to fail when run with siblings.

@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear module-level mutable state and ContextVars between tests.

    Keeps state from leaking across tests on the same xdist worker. Modules
    that don't exist yet (test collection before production import) are
    skipped silently — production import later creates fresh empty state.

    Every block below therefore looks the module up in ``sys.modules`` instead
    of importing it. A module that was never imported has no leaked state to
    clear, so the ``KeyError`` is the "skipped silently" path above; importing
    it here would *create* the very state this fixture exists to clear, and
    charge the module's full import cost to fixture setup. That matters
    because ``--timeout=30`` (pyproject addopts) covers setup: the old
    ``from agent import auxiliary_client`` pulled ~950 modules (the whole
    ``openai`` type tree) and burned 22s before the first test's body ran.
    Pinned by tests/test_conftest_import_cost.py.
    """
    # --- logging — quiet/one-shot paths mutate process-global logger state ---
    logging.disable(logging.NOTSET)
    for _logger_name in ("tools", "run_agent", "trajectory_compressor", "cron", "hermes_cli"):
        _logger = logging.getLogger(_logger_name)
        _logger.disabled = False
        _logger.setLevel(logging.NOTSET)
        _logger.propagate = True

    # --- tools.approval — the single biggest source of cross-test pollution ---
    try:
        _approval_mod = sys.modules["tools.approval"]
        _approval_mod._session_approved.clear()
        _approval_mod._session_yolo.clear()
        _approval_mod._permanent_approved.clear()
        _approval_mod._pending.clear()
        _approval_mod._gateway_queues.clear()
        _approval_mod._gateway_notify_cbs.clear()
        # ContextVar: reset to empty string so get_current_session_key()
        # falls through to the env var / default path, matching a fresh
        # process.
        _approval_mod._approval_session_key.set("")
    except Exception:
        pass

    # --- tools.interrupt — per-thread interrupt flag set ---
    try:
        _interrupt_mod = sys.modules["tools.interrupt"]
        with _interrupt_mod._lock:
            _interrupt_mod._interrupted_threads.clear()
    except Exception:
        pass

    # --- gateway.session_context — ContextVars representing the active
    #     gateway session. If set in one test and not reset, the next
    #     test's get_session_env() reads stale values.
    try:
        _sc_mod = sys.modules["gateway.session_context"]
        for _cv in (
            _sc_mod._SESSION_PLATFORM,
            _sc_mod._SESSION_CHAT_ID,
            _sc_mod._SESSION_CHAT_NAME,
            _sc_mod._SESSION_THREAD_ID,
            _sc_mod._SESSION_USER_ID,
            _sc_mod._SESSION_USER_NAME,
            _sc_mod._SESSION_KEY,
            _sc_mod._CRON_AUTO_DELIVER_PLATFORM,
            _sc_mod._CRON_AUTO_DELIVER_CHAT_ID,
            _sc_mod._CRON_AUTO_DELIVER_THREAD_ID,
        ):
            _cv.set(_sc_mod._UNSET)
    except Exception:
        pass

    # --- tools.env_passthrough — ContextVar<set[str]> with no default ---
    try:
        _envp_mod = sys.modules["tools.env_passthrough"]
        _envp_mod._allowed_env_vars_var.set(set())
    except Exception:
        pass

    # --- tools.terminal_tool — active environment/cwd cache ---
    # File tools prefer a live terminal cwd when one is cached for the task.
    # Clear terminal environments between tests so a prior terminal call can't
    # override TERMINAL_CWD in path-resolution tests.
    try:
        _term_mod = sys.modules["tools.terminal_tool"]
        _envs_to_cleanup = []
        with _term_mod._env_lock:
            _envs_to_cleanup = list(_term_mod._active_environments.values())
            _term_mod._active_environments.clear()
            _term_mod._last_activity.clear()
            _term_mod._creation_locks.clear()
        for _env in _envs_to_cleanup:
            try:
                _env.cleanup()
            except Exception:
                pass
    except Exception:
        pass

    # --- tools.credential_files — ContextVar<dict> ---
    try:
        _credf_mod = sys.modules["tools.credential_files"]
        _credf_mod._registered_files_var.set({})
    except Exception:
        pass

    # --- agent.auxiliary_client — runtime main provider/model override and
    #     payment-error health cache. Both are process-global in production;
    #     reset them per test so one worker's fallback/402 test does not make
    #     later auxiliary-client tests skip otherwise-available providers.
    try:
        _aux_mod = sys.modules["agent.auxiliary_client"]
        _aux_mod.clear_runtime_main()
        _aux_mod._reset_aux_unhealthy_cache()
    except Exception:
        pass

    # --- agent.model_metadata — eleven module-level caches behind TTLs of
    #     3600s / 300s / 30s. Nothing cleared these between tests, so a
    #     metadata or capability lookup in one file decided the answer for
    #     every later file in the same process. Measured 2026-08-13: this is
    #     the mechanism behind the ~128 order/timing-dependent failures in
    #     tests/agent, which pass individually and fail in the hour-long run.
    #     The suite's own helper reset only two of them.
    #
    #     Enumerate from the module, do not trust this list: the first version
    #     of this block said "nine" and missed _LOCAL_CTX_PROBE_CACHE and
    #     _TOOLS_TOKENS_CACHE. Re-derive with:
    #       grep -nE "^_[A-Za-z_]+.*= *(\{\}|dict\(\))" agent/model_metadata.py
    try:
        _mm_mod = sys.modules["agent.model_metadata"]
        for _name in (
            "_model_metadata_cache",
            "_novita_metadata_cache",
            "_endpoint_model_metadata_cache",
            "_endpoint_model_metadata_cache_time",
            "_endpoint_probe_path_cache",
            "_codex_oauth_context_cache",
            "_LOCAL_CTX_PROBE_CACHE",
            "_TOOLS_TOKENS_CACHE",
        ):
            _cache = getattr(_mm_mod, _name, None)
            if _cache is not None:
                _cache.clear()
        # Scalar timestamps: zero rather than clear, so the next lookup reads
        # as "never fetched" instead of "fetched at an hour ago".
        for _name in ("_model_metadata_cache_time", "_novita_metadata_cache_time"):
            if hasattr(_mm_mod, _name):
                setattr(_mm_mod, _name, 0)
        if hasattr(_mm_mod, "_codex_oauth_context_cache_time"):
            _mm_mod._codex_oauth_context_cache_time = 0.0
    except Exception:
        pass

    # --- agent.bedrock_adapter — three module-level caches, none of which
    #     anything reset between tests.
    #
    #     _bedrock_runtime_client_cache demonstrably leaks: several tests in
    #     tests/agent/test_bedrock_adapter.py write entries directly and one
    #     asserts an entry SURVIVES (`_bedrock_runtime_client_cache
    #     ["us-west-2"] == "live-client"`), so the junk is still there for
    #     every later test in the process — and that file sorts before
    #     test_model_metadata.py.
    #
    #     Symptom this is aimed at:
    #     test_bedrock_provider_returns_static_table_before_probe expects the
    #     static 200000 and gets 1300000 in a full run, which is
    #     _BEDROCK_PROBE_TIERS[0] — a *padding target*, so the probe really ran
    #     and "succeeded" instead of failing to the static table as it does in
    #     isolation. A leaked client object is the plausible route.
    #
    #     Stated carefully on purpose: clearing agent.model_metadata's caches
    #     did NOT fix it, so that mechanism is ruled out, but which of these
    #     three caches is responsible has not been isolated. Resetting all
    #     three is correct hygiene either way — no test should inherit another
    #     file's cached AWS client.
    #     Re-derive with:
    #       grep -nE "^_[A-Za-z_]+.*= *(\{\}|dict\(\))" agent/bedrock_adapter.py
    try:
        _br_mod = sys.modules["agent.bedrock_adapter"]
        _br_mod.reset_discovery_cache()
        for _name in (
            "_bedrock_runtime_client_cache",
            "_bedrock_control_client_cache",
        ):
            _cache = getattr(_br_mod, _name, None)
            if _cache is not None:
                _cache.clear()
    except Exception:
        pass

    # --- tools.file_tools — per-task read history + file-ops cache ---
    try:
        _ft_mod = sys.modules["tools.file_tools"]
        with _ft_mod._read_tracker_lock:
            _ft_mod._read_tracker.clear()
        with _ft_mod._file_ops_lock:
            _ft_mod._file_ops_cache.clear()
    except Exception:
        pass

    # --- jobflow_dispatch.quarantine_control — its canonical process cache is
    #     path-bound by design. HERMES_HOME changes for every test, so retaining
    #     the previous test's capability correctly reports disappearance but
    #     makes the suite order-dependent rather than exercising a fresh process.
    try:
        _qc_mod = sys.modules["jobflow_dispatch.quarantine_control"]
        _qc_mod._DEFAULT_CONTROL_STORE = None
        _qc_mod._DEFAULT_CONTROL_STORE_KEY = None
    except Exception:
        pass

    yield


# ── hermes_logging file handlers — module-global, tmp_path-rooted ──────────

def _live_hermes_logging():
    """Return ``hermes_logging`` if it is imported AND is the real module.

    Looked up in ``sys.modules`` rather than imported, for the same reason as
    every block in ``_reset_module_state`` above: a module that was never
    imported has no handlers to reset, and importing it here would charge its
    cost (concurrent-log-handler, portalocker) to every test's fixture setup.

    Several tests stub the name with a ``types.SimpleNamespace`` via
    ``monkeypatch.setitem(sys.modules, "hermes_logging", ...)``; the attribute
    check filters those out so we never call a stub.
    """
    mod = sys.modules.get("hermes_logging")
    return mod if hasattr(mod, "_reset_queued_handlers") else None


@pytest.fixture(autouse=True)
def _reset_hermes_file_logging():
    """Tear down hermes_logging's rotating file handlers around every test.

    ``hermes_logging`` holds its file handlers in a module-global list
    (``_queued_file_handlers``) that a background ``QueueListener`` thread
    dispatches to. That global is correct in production — a process has one
    HERMES_HOME for its whole life — but every test here gets a *different*
    HERMES_HOME under its own ``tmp_path`` (see ``_hermetic_environment``),
    and ``tmp_path_retention_policy = "failed"`` deletes that directory the
    moment the test passes. A handler registered by one test therefore
    outlives its log directory, and the next test in the process that emits a
    record makes the listener thread write into a path that no longer exists::

        --- Logging error ---
        concurrent_log_handler ... FileNotFoundError: [Errno 2] No such file
        or directory: '...\\pytest-NNN\\test_xxx0\\hermes_test\\logs\\.__errors.lock'

    Because it is raised on the listener thread, that spew is attributed to no
    test and fails nothing — it just corrupts the output of whatever ran next.

    Resetting on *teardown* also lets pytest actually reclaim the tempdir: on
    Windows ``concurrent-log-handler`` keeps its ``.__<name>.lock`` file open
    for the handler's lifetime, and an open handle blocks the directory
    removal that the "failed" retention policy is there to perform.

    ``_logging_initialized`` is cleared alongside the handlers because
    ``setup_logging()`` checks it *before* registering agent.log/errors.log:
    left set with an empty handler list, the next test's ``setup_logging()``
    call would silently attach nothing. The two are reset together in
    tests/test_hermes_logging.py's own fixture for the same reason.

    Pinned by tests/test_logging_handler_isolation.py.
    """
    def _reset(mod):
        if mod is None:
            return
        mod._reset_queued_handlers()
        mod._logging_initialized = False

    # Capture at setup: a test may replace sys.modules["hermes_logging"] with a
    # stub in its body, and monkeypatch's undo runs after this fixture's
    # teardown, so the lookup below could otherwise come back empty.
    captured = _live_hermes_logging()
    _reset(captured)
    yield
    _reset(_live_hermes_logging() or captured)


@pytest.fixture()
def tmp_dir(tmp_path):
    """Provide a temporary directory that is cleaned up automatically."""
    return tmp_path


@pytest.fixture()
def mock_config():
    """Return a minimal hermes config dict suitable for unit tests."""
    return {
        "model": "test/mock-model",
        "toolsets": ["terminal", "file"],
        "max_turns": 10,
        "terminal": {
            "backend": "local",
            "cwd": "/tmp",
            "timeout": 30,
        },
        "compression": {"enabled": False},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "command_allowlist": [],
    }


# ── Per-test timeout — handled by the isolation plugin ─────────────────────
#
# The subprocess-per-test plugin enforces the configured ``isolate_timeout``
# ini key by terminating the child if it overruns. The old SIGALRM-based
# fixture (POSIX-only, didn't work on Windows) is gone.


@pytest.fixture(autouse=True)
def _ensure_current_event_loop(request):
    """Provide a default event loop for sync tests that call get_event_loop().

    Python 3.11+ no longer guarantees a current loop for plain synchronous tests.
    A number of gateway tests still use asyncio.get_event_loop().run_until_complete(...).
    Ensure they always have a usable loop without interfering with pytest-asyncio's
    own loop management for @pytest.mark.asyncio tests.

    On Python 3.12+, ``asyncio.get_event_loop_policy().get_event_loop()`` with no
    *running* loop emits DeprecationWarning; skip that path and install a fresh
    loop via ``new_event_loop()`` instead.
    """
    if request.node.get_closest_marker("asyncio") is not None:
        yield
        return

    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        pass

    if loop is None and sys.version_info < (3, 12):
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            loop = None

    created = loop is None or loop.is_closed()
    if created:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        yield
    finally:
        if created and loop is not None:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)


# ── Live-system guard ──────────────────────────────────────────────────────
#
# Several test files exercise the gateway-restart / kill code paths
# (``cmd_update``, ``kill_gateway_processes``, ``stop_profile_gateway``).
# When a single test forgets to mock either ``os.kill`` or the global
# ``find_gateway_pids`` helper, the real call leaks out of the hermetic
# environment and finds the developer's live ``hermes-gateway`` process
# via ``psutil`` — sending it SIGTERM mid-test. The shutdown forensics in
# PR #23285 caught this happening 5+ times in 3 days, every time
# correlated with a ``tests/hermes_cli/`` pytest run starting up.
#
# This fixture makes the leak impossible by intercepting the two
# primitives that actually do damage:
#
#  • ``os.kill`` rejects any PID outside the test process subtree with
#    a hard ``RuntimeError`` so the offending test gets a stack trace
#    instead of silently murdering the real gateway.
#  • ``subprocess.run`` / ``subprocess.Popen`` / ``call`` / ``check_call`` /
#    ``check_output`` reject any ``systemctl ... <verb> hermes-gateway``
#    invocation that would mutate the live unit. Read-only systemctl
#    calls (``status``, ``show``, ``list-units``) still pass through.
#  • The same wrappers reject ``pip``/``uv pip``/``ensurepip``
#    install/uninstall commands aimed at the LIVE venv, so a test can
#    never pip-install into the environment the gateway runs from.
#    Installs redirected via ``--target``/``--prefix``/``--root``/
#    ``--python``, or run against another interpreter, pass through.
#  • The same wrappers reject a DIRECT gateway launch/stop —
#    ``hermes gateway run``, ``pythonw -m hermes_cli.main gateway run``,
#    ``python -m gateway.run``. This is the systemd guard's blind spot:
#    it keys on ``systemctl`` being present, so every detached-spawn path
#    (the only one Windows ever uses) went unchecked. A test that stubs
#    part of the launch surface and misses the branch the code actually
#    takes would silently leave a background gateway behind on the
#    developer's machine — see _is_gateway_lifecycle_cmd for the history.
#
# "Discovery without delivery is harmless" — the stance this comment held from
# 2026-05-10 — remains TRUE for signal safety: the ``os.kill`` guard above
# catches the actual signal call and the ``systemctl`` guard catches the systemd
# path, so a scanned PID is never signalled. It is NOT true for cost or
# determinism, which is why ``_pid_scan_guard`` below now defaults the host
# process-table scans to empty. Reversal approved by Diego 2026-08-15 on
# these measurements:
#
#   • Cost. ``_scan_gateway_pids`` returned the developer's LIVE gateway PID
#     [47164] in ~2–3 s per call standalone (2026-08-15), and 10–16 s per sweep
#     inside a loaded suite (2026-08-17: 34.03 s/3 sweeps in test_cron.py,
#     19.91 s/2 in test_gateway_windows.py, 15.62 s/1 in test_status.py).
#     ``addopts`` pins a 30 s per-test timeout that also covers fixture setup,
#     so one test reaching the scan twice (``gateway_windows.stop()`` does) can
#     consume the whole budget. Not a projection: while measuring this guard, an
#     UNGUARDED baseline run of test_cron.py was killed by pytest-timeout inside
#     ``subprocess.communicate`` under the real sweep, never reaching
#     ``sessionfinish``. Re-measuring this A/B therefore needs a raised
#     ``--timeout`` on BOTH arms or the baseline cannot finish at all.
#   • Determinism. The sweep finds whatever gateway happens to be running on the
#     machine, so an unstubbed test passes or fails on host state. ``HERMES_HOME``
#     is already tempdir-redirected by ``_hermetic_environment`` and
#     ``_get_service_pids`` is inert off Linux, so this sweep was the LAST way
#     the host leaked into a PID-path result.
#
# Only the sweep is stubbed, not ``find_gateway_pids``: that function's
# composition logic (PID-file merge, service PIDs, exclude/ancestor handling,
# restart-manager gating) stays under real coverage and simply sees an empty
# contribution from the process table, which is the correct hermetic default.
#
# ``hermes_cli.main._find_stale_dashboard_pids`` is the same hazard one notch
# sharper, and is guarded the same way (2026-08-17). It walks the host process
# table IN-PROCESS — ``psutil.process_iter(["pid","cmdline"])``, since Windows
# 11 dropped ``wmic`` — so there is no subprocess to intercept and the sweep
# probe written for the gateway scanner was structurally blind to it:
#
#   • Cost. 1011 host processes walked, 1.1–4.3 s per call standalone, and
#     1.5552 s / 670 processes inside the confirmed offender
#     (tests/hermes_cli/test_update_zip_symlink_reject.py::
#     test_update_via_zip_accepts_normal_member, which reaches it via
#     ``_update_via_zip`` -> ``_kill_stale_dashboard_processes``).
#   • Determinism AND delivery. Unlike the gateway sweep, this one is wired
#     straight to a killer: ``_kill_stale_dashboard_processes`` runs
#     ``subprocess.run(["taskkill", "/PID", pid, "/F"])`` for every PID it
#     returns. In the offender that was neutralised only by the test's own
#     broad ``patch("subprocess.run")`` — which ALSO stopped
#     ``_is_foreign_pid_kill`` below from ever evaluating the argv. Removing
#     that patch and calling the reaper for real (2026-08-17) produced
#     "⟲ Stopping 3 dashboard process(es)" against the developer's live
#     dashboards, blocked at ``taskkill /PID 1784 /F`` by this file's guard
#     alone. Neither test named "dashboard" or the scanner, so grep could not
#     have found either — see the canaries in
#     tests/test_live_system_guard_self_test.py.
#
# Design: docs/superpowers/specs/2026-08-17-gateway-pid-scan-test-guard-design.md

_LIVE_SYSTEM_GUARD_BYPASS_MARK = "live_system_guard_bypass"
_REAL_GATEWAY_PID_SCAN_MARK = "real_gateway_pid_scan"
_REAL_DASHBOARD_PID_SCAN_MARK = "real_dashboard_pid_scan"

# ── PID-scan guards: arm at import, decide at call ─────────────────────────
#
# Each guarded scanner must be stubbed before any unmarked test can call it,
# WITHOUT importing its module anywhere in a test process. Both halves of that
# are hard constraints, and together they rule out the two obvious
# implementations (see the spec above for the full analysis):
#
#   • Importing the module in the autouse fixture costs 3.6 s and 463 modules
#     for ``hermes_cli.gateway`` (measured 2026-08-17) and pulls
#     ``agent.model_metadata`` into every trivial test — exactly what
#     tests/test_conftest_import_cost.py forbids. Note that test snapshots
#     ``sys.modules`` at ``pytest_sessionfinish``, so importing once at conftest
#     load instead of per-test fails it too. ``hermes_cli.main`` is cheaper
#     (2.1 s / 252 modules) and pulls none of that test's FORBIDDEN_PREFIXES,
#     so its *stated* assertion would pass — but the same test runs its child
#     pytest with ``-p import_probe``, and main's import-time
#     ``_apply_profile_override()`` reads ``-p`` as ``--profile`` and
#     ``sys.exit(1)``s, so the child aborts and ``returncode == 0`` fails. The
#     no-import rule holds for both modules, for two different reasons.
#   • A ``sys.modules.get`` lookup at fixture setup imports nothing, but leaves
#     the guard UNARMED whenever the module is not yet imported at that point.
#     That is the common case, and the runner gives each file its own process:
#     for ``hermes_cli.gateway`` 17 test files import it inside a function body
#     versus 15 at module scope; for ``hermes_cli.main`` it is 176 versus 136.
#
# So: patch at IMPORT time via a ``sys.meta_path`` finder, and choose real-vs-
# stub at CALL time from a flag the autouse fixture flips. The split is what
# makes the opt-out marker work when a marked test imports the module fresh
# inside its own body — an install-time decision would have to commit to one
# behaviour for the rest of the process.
# ``tests/test_windows_subprocess_no_window_flags.py::
# test_gateway_pid_scan_hides_wmic_and_powershell_windows`` is exactly that
# shape, so this is load-bearing rather than theoretical.
#
# Precedent, same mechanism, already in production on this branch:
# ``cli.py`` (~line 852) installs a meta_path finder that defers patching
# ``openai._base_client`` until first import, for the same reason — an eager
# import cost it did not want to pay. Divergence: that finder disarms and
# removes itself after firing because it patches a class once; this one stays
# armed so ``importlib.reload`` re-applies the guard, which is also why it
# delegates by walking ``sys.meta_path`` (skipping itself) instead of calling
# ``importlib.util.find_spec`` and needing a disarm to dodge recursion.
#
# Overhead measured 2026-08-17: 646 ns per ``find_spec`` call, 310 calls on the
# heaviest import chain in the repo = 0.2 ms per process.

class _PidScanSpec:
    """One host process-table scanner that unmarked tests get ``[]`` from.

    Adding a third is a one-line entry in ``_PID_SCAN_SPECS`` plus a marker in
    ``pytest_configure`` and ``pyproject.toml`` — the finder, the installer and
    the fixture are all spec-driven.
    """

    __slots__ = ("module_name", "attr", "marker", "allow_real")

    def __init__(self, module_name, attr, marker):
        self.module_name = module_name
        self.attr = attr
        self.marker = marker
        # Flipped per test by ``_pid_scan_guard`` and read by the wrapper when
        # it is CALLED — the split that lets a marked test import the module
        # fresh inside its own body and still get the real scanner.
        self.allow_real = False


_PID_SCAN_SPECS = (
    _PidScanSpec(
        "hermes_cli.gateway", "_scan_gateway_pids", _REAL_GATEWAY_PID_SCAN_MARK
    ),
    _PidScanSpec(
        "hermes_cli.main",
        "_find_stale_dashboard_pids",
        _REAL_DASHBOARD_PID_SCAN_MARK,
    ),
)
_PID_SCAN_SPECS_BY_MODULE = {spec.module_name: spec for spec in _PID_SCAN_SPECS}


def _install_pid_scan_guard(module, guarded):
    """Replace ``module.<guarded.attr>`` with the call-time dispatcher.

    Idempotent: a second call (``importlib.reload``, double registration) sees
    its own wrapper and returns. Exceptions are swallowed — a conftest bug must
    never break imports for the whole suite. The self-test canaries in
    tests/test_live_system_guard_self_test.py are what convert a silent failure
    here into one loud red test.
    """
    try:
        real = getattr(module, guarded.attr, None)
        if real is None or getattr(real, "_hermes_pid_scan_guard", False):
            return

        def _guarded_scan(*args, **kwargs):
            if guarded.allow_real:
                return real(*args, **kwargs)
            # A plain ``[]``, never the scanner's own richer return type:
            # ``_find_stale_dashboard_pids`` really returns a ``_DashboardPids``
            # carrying ``scan_ok``, and ``main._scan_ok()`` defaults a plain
            # list to True — so this reads as a SUCCESSFUL empty scan rather
            # than "couldn't look". That is what the many existing tests
            # patching these functions with ``return_value=[]`` already assume;
            # a failed-scan stub would make the reaper print a warning instead
            # of returning silently.
            return []

        # Deliberately NOT functools.wraps: copying ``__name__`` would disguise
        # the wrapper as the real scanner in tracebacks and in any identity
        # check. A distinct, self-describing name instead; the attributes below
        # are the supported way to recognise it.
        _guarded_scan.__name__ = f"_guarded_{guarded.attr}"
        _guarded_scan._hermes_pid_scan_guard = True
        _guarded_scan._hermes_pid_scan_real = real
        setattr(module, guarded.attr, _guarded_scan)
    except Exception:
        pass


class _PidScanGuardFinder:
    """Patch a guarded module the moment it is first imported."""

    def find_spec(self, fullname, path=None, target=None):
        # First statement is a dict lookup that returns None, so every other
        # import in the process pays one cheap call and nothing else.
        guarded = _PID_SCAN_SPECS_BY_MODULE.get(fullname)
        if guarded is None:
            return None
        try:
            for finder in sys.meta_path:
                if finder is self:
                    continue
                spec = finder.find_spec(fullname, path, target)
                if spec is None or spec.loader is None:
                    continue
                _orig_exec = spec.loader.exec_module

                def _patched_exec(module, _orig_exec=_orig_exec, _guarded=guarded):
                    _orig_exec(module)
                    _install_pid_scan_guard(module, _guarded)

                # Set on the loader INSTANCE, which FileFinder builds fresh per
                # spec, so this never leaks to another module's import.
                spec.loader.exec_module = _patched_exec
                return spec
        except Exception:
            # Fall through to the normal import, unguarded. The canary catches it.
            return None
        return None


# ── local-server probe guard: arm at import, decide at call ────────────────
#
# ``agent.model_metadata`` reaches the NETWORK to work out what model server is
# answering at ``base_url``. ``detect_local_server_type`` walks a five-endpoint
# waterfall (/api/v1/models, /api/tags, /v1/props, /props, /version) inside an
# ``httpx.Client(timeout=2.0)``, and ``_query_local_context_length`` opens its
# own ``timeout=3.0`` client on top of that.
#
# Where a closed port refuses instantly this is free, and therefore invisible.
# It is not free everywhere. Measured on this developer's box 2026-08-17: a
# closed local port returns its RST after ~2.02 s, not immediately (listening
# ports answer in ~0.001 s; 4000/11434/1234/8000/8080/54321 all take ~2.02 s,
# via 127.0.0.1 as well as localhost — so it is NOT the IPv6 dual-stack penalty
# ``_localhost_to_ipv4`` already guards). Every probe therefore burns its full
# ceiling: ~10 s per ``detect_local_server_type`` call, which puts a single test
# over the 30 s pytest-timeout cap.
#
# The failure that produced is worth stating, because it does not look like a
# network problem from the outside: pytest-timeout kills the process mid-file,
# so no summary line is printed, so the parallel runner buckets the file under
# "no tests ran (collection/import error …)". On 2026-08-17 that was 10 files —
# every one of which collected and ran fine, and none of which had an import
# error. ``tests/run_agent/test_streaming.py`` collected 53 tests and ran 172 s
# before it was killed.
#
# Stubbing to ``None`` is not a lie. ``None`` is exactly what both functions
# return on a machine with no local model server, which is what CI is and what
# the affected tests all assume — e.g.
# ``tests/run_agent/test_invalid_context_length_warning.py`` patches
# ``get_model_context_length`` intending to be offline, and simply misses this
# second path.
#
# Same arm-at-import / decide-at-call split as the gateway PID-scan guard
# above, for the same two reasons: ``agent.model_metadata`` is one of the
# expensive imports that guard's note measures (3.6 s / 463 modules), so the
# fixture must not import it; and test files import it inside function bodies
# as often as at module scope, so a ``sys.modules`` lookup at fixture setup
# would leave the common case unarmed.

_REAL_LOCAL_SERVER_PROBE_MARK = "real_local_server_probe"

_LOCAL_SERVER_PROBE_TARGET_MODULE = "agent.model_metadata"

# attr -> FACTORY for what the stub returns. Every value is in-contract "could
# not determine", which is the honest answer for a host with no local server.
#
# Factories, not constants, because ``fetch_endpoint_model_metadata`` returns a
# dict: handing every caller the same object would let one test's mutation
# leak into the next.
#
# This list was derived by TRACING, not by reading — a plugin wrapping
# ``socket.socket.connect`` during
# tests/run_agent/test_invalid_context_length_warning.py. Stubbing only the
# first two left 20.3 s of connects across 11 attempts still happening, via
# ``fetch_endpoint_model_metadata`` (model_metadata.py:1050) and
# ``_query_ollama_api_show_uncached`` (:1653). If a new probe is added to
# ``agent.model_metadata``, re-run that trace rather than assuming this list is
# still complete.
_LOCAL_SERVER_PROBE_ATTRS = {
    "detect_local_server_type": lambda: None,       # Optional[str]
    "_query_local_context_length": lambda: None,    # Optional[int]
    "_query_ollama_api_show": lambda: None,         # Optional[int]
    "fetch_endpoint_model_metadata": lambda: {},    # Dict[str, Dict[str, Any]]
}

# Single-element list for the same reason as ``_PID_SCAN_ALLOW_REAL``: the
# closures below mutate the same cell the fixture writes.
# ── provider-auth probe guard ──────────────────────────────────────────────
#
# Same disease, different organ, and a worse prognosis: these reach the PUBLIC
# INTERNET, not localhost.
#
#   hermes_cli/auth.py:653      detect_zai_endpoint
#   hermes_cli/copilot_auth.py  exchange_copilot_token
#
# ``detect_zai_endpoint`` httpx.post()s a real ``{"messages":[{"role":"user",
# "content":"ping"}]}`` completion to every entry of ZAI_ENDPOINTS x
# probe_models at 8 s each; ``exchange_copilot_token`` GETs
# api.github.com/copilot_internal/v2/token with whatever raw token it was
# handed. A connect trace over tests/hermes_cli/test_api_key_providers.py
# (2026-08-17, unmarked, no network stubs) recorded 19 outbound TLS connections
# to 128.14.14.140/141, 122.10.144.213, 156.59.101.94 and 140.82.113.6.
#
# That makes an ordinary test run depend on someone else's uptime, leak the
# fact of the run to two third parties, and — with a real key in the
# environment — spend the developer's quota. The latency is only the symptom
# people notice.
#
# Marker is SEPARATE from real_local_server_probe on purpose: a test that wants
# the real local-server waterfall has no business also re-enabling live calls
# to z.ai and GitHub.

_REAL_PROVIDER_AUTH_PROBE_MARK = "real_provider_auth_probe"


def _copilot_exchange_unavailable(*_args, **_kwargs):
    """Stub for ``exchange_copilot_token``.

    Raises rather than returning a sentinel because that IS the function's
    documented contract — "Raises ``ValueError`` on failure" — and its return
    type ``tuple[str, float, Optional[str]]`` has no in-band "no token" value.
    """
    raise ValueError(
        "exchange_copilot_token is stubbed in tests (it calls api.github.com); "
        "mark the test @pytest.mark.real_provider_auth_probe to opt out"
    )


class _NetworkProbeGuard:
    """One arm-at-import / decide-at-call guard over a set of module attributes.

    Parameterised because there is more than one family of network probe to
    neutralise and they need INDEPENDENT opt-out markers. Same mechanism as the
    gateway PID-scan guard above, and for the same reason — the target modules
    are expensive to import, so the fixture must not touch them.

    Doubles as its own ``sys.meta_path`` finder: ``find_spec`` returns None
    after a single dict lookup for every module it does not target, so
    unrelated imports keep paying only the ~646 ns/call that guard measured.
    """

    def __init__(self, marker, targets, tag):
        self.marker = marker
        self.targets = targets      # {module name: {attr: zero-arg stub}}
        self.tag = tag              # attribute stamped on each wrapper
        self.allow_real = [False]   # list so the closures share the cell

    def install(self, module_name, module):
        """Replace each probing attr with a call-time dispatcher.

        Idempotent per attr, and per-attr fault isolated — one attr disappearing
        in a refactor must not leave the others unguarded. Exceptions are
        swallowed so a conftest bug cannot break imports for the whole suite;
        the canaries in tests/test_live_system_guard_self_test.py are what turn
        a silent failure here into one loud red test.
        """
        allow_real, tag = self.allow_real, self.tag
        for attr, stub in self.targets.get(module_name, {}).items():
            try:
                real = getattr(module, attr, None)
                if real is None or getattr(real, tag, False):
                    continue

                def _guarded(*args, _real=real, _stub=stub, **kwargs):
                    if allow_real[0]:
                        return _real(*args, **kwargs)
                    return _stub()

                # Deliberately NOT functools.wraps — see the PID-scan guard.
                setattr(_guarded, tag, True)
                _guarded._hermes_probe_real = real
                setattr(module, attr, _guarded)
            except Exception:
                continue

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.targets:
            return None
        try:
            for finder in sys.meta_path:
                if finder is self:
                    continue
                spec = finder.find_spec(fullname, path, target)
                if spec is None or spec.loader is None:
                    continue
                _orig_exec = spec.loader.exec_module

                def _patched_exec(module, _orig_exec=_orig_exec, _name=fullname):
                    _orig_exec(module)
                    self.install(_name, module)

                spec.loader.exec_module = _patched_exec
                return spec
        except Exception:
            return None
        return None

    def arm(self):
        """Patch what is already imported, then catch every later first-import."""
        for name in self.targets:
            already = sys.modules.get(name)
            if already is not None:
                self.install(name, already)
        sys.meta_path.insert(0, self)


_LOCAL_SERVER_PROBE_GUARD = _NetworkProbeGuard(
    marker=_REAL_LOCAL_SERVER_PROBE_MARK,
    targets={_LOCAL_SERVER_PROBE_TARGET_MODULE: _LOCAL_SERVER_PROBE_ATTRS},
    tag="_hermes_local_server_probe_guard",
)

_PROVIDER_AUTH_PROBE_GUARD = _NetworkProbeGuard(
    marker=_REAL_PROVIDER_AUTH_PROBE_MARK,
    targets={
        "hermes_cli.auth": {
            # Optional[Dict[str, str]]; None == "no endpoint accepted this key".
            "detect_zai_endpoint": lambda: None,
        },
        "hermes_cli.copilot_auth": {
            "exchange_copilot_token": _copilot_exchange_unavailable,
        },
    },
    tag="_hermes_provider_auth_probe_guard",
)

_NETWORK_PROBE_GUARDS = (_LOCAL_SERVER_PROBE_GUARD, _PROVIDER_AUTH_PROBE_GUARD)


@pytest.fixture(autouse=True)
def _live_state_db_guard(request):
    """Arm/disarm the live-``state.db`` refusal in the sqlite3.connect wrapper.

    The refusal itself lives in ``_reject_live_state_db`` (module level, so it
    also covers opens during collection/import, which no fixture can reach).
    This fixture only lifts it for a test explicitly marked
    ``@pytest.mark.live_state_db_bypass``.

    Restoration is unconditional in the ``finally`` so a raising test cannot
    leave the guard disarmed for the rest of the file -- the failure mode that
    would silently un-protect every later test in the same process.
    """
    bypass = request.node.get_closest_marker(_LIVE_STATE_DB_BYPASS_MARK) is not None
    previous = _allow_live_state_db[0]
    _allow_live_state_db[0] = bypass
    try:
        yield
    finally:
        _allow_live_state_db[0] = previous


def pytest_configure(config):  # noqa: D401 — pytest hook
    """Register markers used by hermetic conftest."""
    config.addinivalue_line(
        "markers",
        f"{_LIVE_SYSTEM_GUARD_BYPASS_MARK}: bypass the live-system guard "
        "(only for tests that genuinely need real os.kill / subprocess "
        "behaviour — e.g. PTY tests that signal their own child).",
    )
    config.addinivalue_line(
        "markers",
        f"{_REAL_LOCAL_SERVER_PROBE_MARK}: opt out of the autouse stub that "
        "neutralises agent.model_metadata's four network probes (for tests of "
        "the probes themselves, which stub httpx beneath them).",
    )
    config.addinivalue_line(
        "markers",
        f"{_REAL_PROVIDER_AUTH_PROBE_MARK}: opt out of the autouse stub that "
        "neutralises hermes_cli.auth.detect_zai_endpoint and "
        "hermes_cli.copilot_auth.exchange_copilot_token (for tests of those "
        "probes, which stub the HTTP layer beneath them).",
    )
    config.addinivalue_line(
        "markers",
        f"{_REAL_GATEWAY_PID_SCAN_MARK}: opt out of the autouse stub that "
        "defaults hermes_cli.gateway._scan_gateway_pids to [] (for tests of "
        "the scanner itself, which stub the process-table source beneath it).",
    )
    config.addinivalue_line(
        "markers",
        f"{_REAL_DASHBOARD_PID_SCAN_MARK}: opt out of the autouse stub that "
        "defaults hermes_cli.main._find_stale_dashboard_pids to [] (for tests "
        "of the scanner itself, which stub psutil.process_iter beneath it).",
    )
    config.addinivalue_line(
        "markers",
        f"{_LIVE_STATE_DB_BYPASS_MARK}: allow this test to open the LIVE "
        "~/.hermes/state.db. Almost never correct -- an open of the live file "
        "normally means HERMES_HOME isolation failed for that code path. Use "
        "only when reading the real machine database IS the thing under test.",
    )

    # Arm the PID-scan guards. Patch now if a module is somehow already
    # imported (a plugin, an earlier conftest), then install the finder so any
    # later first-import is patched at exec_module time.
    for _guarded in _PID_SCAN_SPECS:
        _already_imported = sys.modules.get(_guarded.module_name)
        if _already_imported is not None:
            _install_pid_scan_guard(_already_imported, _guarded)
    sys.meta_path.insert(0, _PidScanGuardFinder())

    # Same two-step for every network-probe guard.
    for _guard in _NETWORK_PROBE_GUARDS:
        _guard.arm()

    # The pyproject addopts pin ``--timeout-method=signal`` relies on
    # ``signal.SIGALRM``, which does not exist on Windows — pytest-timeout
    # raises AttributeError at timer setup and the whole run aborts before any
    # test executes. Fall back to the thread-based timer on Windows so the
    # suite runs natively there (POSIX keeps the more reliable signal method).
    if sys.platform == "win32" and getattr(config.option, "timeout_method", None) == "signal":
        config.option.timeout_method = "thread"


@pytest.fixture(autouse=True)
def _live_system_guard(request, monkeypatch):
    """Block real os.kill / systemctl / process-killer commands during tests.

    *PID scans* themselves are handled separately by ``_pid_scan_guard``
    below — this fixture only stops a scanned PID from being signalled, it
    does not stop the scan itself.

    See block comment above for the why. Tests that genuinely need
    real signal delivery (e.g. PTY tests that SIGINT their own child)
    can opt out with ``@pytest.mark.live_system_guard_bypass``.

    Coverage (every primitive that can deliver a signal to or otherwise
    terminate a foreign process):
      • os.kill, os.killpg (POSIX)
      • subprocess.run / Popen / call / check_call / check_output
      • subprocess.getoutput / getstatusoutput
      • os.system / os.popen
      • pty.spawn
      • asyncio.create_subprocess_exec / create_subprocess_shell
    The same subprocess interception also blocks pip/uv/ensurepip
    commands that would install or remove packages in the LIVE venv the
    developer and the gateway run from (see ``_is_package_install``),
    Node package managers (see ``_is_node_package_install``), and
    ``curl … | sh`` remote installers (see ``_is_remote_installer_pipe``).
    Subprocess inspection looks at the WHOLE command string (not just
    tokens[0]), so ``bash -c "systemctl restart hermes-gateway"``,
    ``sudo systemctl ...``, ``env systemctl ...``, ``setsid systemctl ...``
    are all caught. ``pkill``/``killall``/``taskkill`` invocations
    targeting hermes/python patterns are also blocked.
    """
    if request.node.get_closest_marker(_LIVE_SYSTEM_GUARD_BYPASS_MARK):
        yield
        return

    import os as _os
    import re as _re
    import shlex as _shlex
    import subprocess as _subprocess

    test_pid = _os.getpid()
    try:
        import psutil as _psutil
    except Exception:
        _psutil = None

    # Snapshot of the test process's children, allowlisted alongside the
    # live psutil walk below. Taken LAZILY, on the first guarded kill:
    # ``Process.children(recursive=True)`` builds a ppid map of every process
    # on the box (~61ms per call on this Windows host, measured 2026-08-13)
    # and the overwhelming majority of tests never signal anything — that was
    # 6.0s of a 98-test file, charged to every test in the repo.
    #
    # Deferring only *widens* the snapshot to children the test itself spawned
    # before the kill, and those are already allowlisted by the parents() walk
    # in ``_is_own_subtree``, so the guard's answer is unchanged: a foreign PID
    # is still refused. (Both spellings share the pre-existing stale-PID
    # window — a dead child's PID recycled by a foreign process reads as ours.)
    _initial_children: set[int] = set()
    _snapshot_taken = False

    def _own_children() -> set[int]:
        nonlocal _snapshot_taken
        if not _snapshot_taken:
            _snapshot_taken = True
            if _psutil is not None:
                try:
                    _initial_children.update(
                        c.pid
                        for c in _psutil.Process(test_pid).children(recursive=True)
                    )
                except Exception:
                    pass
        return _initial_children

    def _is_own_subtree(pid: int) -> bool:
        # PID 0 means "our own process group"; -1 means "every process we
        # can signal". Both are dangerous when paired with SIGTERM/SIGKILL,
        # but pid 0 is technically scoped to our group so allow it; pid -1
        # is treated as foreign (refuse).
        if pid == 0:
            return True
        if pid < 0:
            return False
        if pid == test_pid or pid in _own_children():
            return True
        if _psutil is None:
            return False
        try:
            walker = _psutil.Process(pid)
        except Exception:
            # Stale PID — kill would be a no-op anyway, allow it.
            return True
        try:
            for parent in walker.parents():
                if parent.pid == test_pid:
                    return True
        except Exception:
            return False
        return False

    real_kill = _os.kill

    def _guarded_kill(pid, sig, *args, **kwargs):
        # Signal 0 is a pure liveness probe — it cannot terminate anything.
        # psutil.pid_exists() uses os.kill(pid, 0) on POSIX, and probing a
        # just-killed grandchild that was reparented to init (zombie with a
        # foreign parent chain) must not trip the guard. Flaked in CI on
        # test_entire_tree_is_sigkilled_not_just_parent.
        if int(sig) == 0:
            return real_kill(pid, sig, *args, **kwargs)
        if _is_own_subtree(int(pid)):
            return real_kill(pid, sig, *args, **kwargs)
        raise RuntimeError(
            f"tests/conftest.py live-system guard: blocked os.kill("
            f"{pid}, {sig}) — PID is outside the test process subtree. "
            "If this fired in CI it means the test reached a real "
            "kill_gateway_processes / stop_profile_gateway / cmd_update "
            "code path without mocking find_gateway_pids and os.kill. "
            "Mock both, or mark the test with "
            "@pytest.mark.live_system_guard_bypass if real signal "
            "delivery is genuinely required."
        )

    monkeypatch.setattr(_os, "kill", _guarded_kill)

    # ``os.killpg`` is the same risk class — sends a signal to every
    # process in a group. The gateway is a session leader (its own
    # PGID == its PID), so killpg(gateway_pid, SIGTERM) is a one-shot
    # kill of the live process. Allow it only when the target PGID is
    # the test process's own group.
    if hasattr(_os, "killpg"):
        real_killpg = _os.killpg
        own_pgid = _os.getpgrp()

        def _guarded_killpg(pgid, sig, *args, **kwargs):
            # Signal 0 is a pure liveness probe — never destructive.
            if int(sig) == 0:
                return real_killpg(pgid, sig, *args, **kwargs)
            if int(pgid) == own_pgid or _is_own_subtree(int(pgid)):
                return real_killpg(pgid, sig, *args, **kwargs)
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"os.killpg({pgid}, {sig}) — PGID is outside the test "
                "process group. See _live_system_guard for the why."
            )

        monkeypatch.setattr(_os, "killpg", _guarded_killpg)

    # ── psutil.Process.terminate / kill / send_signal ──────────────
    # psutil NEVER routes through Python's ``os.kill``: it calls
    # TerminateProcess (Windows) or kill(2) (POSIX) from its C extension,
    # so every guard in this fixture was blind to it. Proven 2026-08-13 —
    # ``psutil.Process(4).terminate()`` under the guard reached the real
    # Windows TerminateProcess and was stopped only by the OS's own ACL on
    # PID 4. Against an ordinary user-owned process (the developer's
    # dashboard, the live gateway) nothing would have stopped it.
    # Same ``_is_own_subtree`` allowlist and same bypass marker as os.kill.
    if _psutil is not None:
        def _wrap_psutil_kill(method_name):
            real_method = getattr(_psutil.Process, method_name)

            def _guarded_psutil(self, *args, **kwargs):
                # send_signal(0) is a pure liveness probe — never destructive.
                if method_name == "send_signal" and args:
                    try:
                        if int(args[0]) == 0:
                            return real_method(self, *args, **kwargs)
                    except (TypeError, ValueError):
                        pass
                try:
                    target = int(self.pid)
                except Exception:
                    target = -1
                if _is_own_subtree(target):
                    return real_method(self, *args, **kwargs)
                raise RuntimeError(
                    f"tests/conftest.py live-system guard: blocked "
                    f"psutil.Process({target}).{method_name}() — PID is "
                    "outside the test process subtree. psutil bypasses the "
                    "os.kill guard entirely (it terminates from C), so this "
                    "is the only thing standing between a real "
                    "psutil.process_iter() walk and the developer's live "
                    "dashboard / gateway. The host scanners "
                    "(hermes_cli.main._find_stale_dashboard_pids, "
                    "hermes_cli.gateway._scan_gateway_pids) are already "
                    "stubbed to [] by _pid_scan_guard, so reaching here means "
                    "either this test opted out with a real_*_pid_scan marker "
                    "without stubbing the process table beneath it, or the "
                    "code under test walks psutil itself — stub that walk, or "
                    "mark the test with @pytest.mark.live_system_guard_bypass "
                    "if real termination is genuinely required."
                )

            _guarded_psutil.__name__ = f"_guarded_psutil_{method_name}"
            return _guarded_psutil

        for _psutil_method in ("terminate", "kill", "send_signal"):
            if hasattr(_psutil.Process, _psutil_method):
                monkeypatch.setattr(
                    _psutil.Process,
                    _psutil_method,
                    _wrap_psutil_kill(_psutil_method),
                )

    # ── Subprocess command-string inspection (whole-line) ──────────
    _HERMES_TOKENS = (
        "hermes-gateway",
        "hermes.service",
        "hermes_cli.main gateway",
        "hermes_cli/main.py gateway",
        "gateway/run.py",
        "hermes gateway",
    )
    _MUTATING_VERBS = (
        "restart", "start", "stop", "kill", "reload",
        "reset-failed", "enable", "disable", "mask", "unmask",
        "daemon-reload", "try-restart", "reload-or-restart",
    )
    _PROCESS_KILLERS = ("pkill", "killall", "taskkill", "skill", "fuser")
    # Killers that take a PID rather than a name pattern. ``kill`` is here
    # and NOT in _PROCESS_KILLERS on purpose: the name-pattern rule below
    # keys on a hermes/gateway/python token, and "kill" is a common enough
    # word in an argv that pairing it with that rule would misfire. In the
    # PID rule it is safe — a PID either resolves to a live foreign process
    # or it does not.
    _PID_TARGETED_KILLERS = _PROCESS_KILLERS + ("kill",)
    # Verbs that, applied to the ``gateway`` subcommand, start or tear down a
    # REAL gateway process. ``status`` / ``logs`` / ``health`` / ``install``
    # are deliberately absent — they are read-only or config-only and several
    # tests exercise them.
    _GATEWAY_LIFECYCLE_VERBS = (
        "run", "start", "restart", "replace", "stop", "kill",
    )

    # Package-installer tokens. ``pipx``/``uv tool`` deliberately absent:
    # they install into their own isolated dirs, not the active venv.
    _PIP_HEAD = _re.compile(r"^pip[0-9.]*$")
    _INSTALL_VERBS = ("install", "uninstall")
    # Flags that redirect the install away from the running interpreter's
    # environment, so it cannot mutate the developer's / gateway's venv.
    # ``--user`` is deliberately NOT here: it mutates the real user site.
    _INSTALL_REDIRECT_FLAGS = (
        "--target", "--prefix", "--root", "--python", "--dry-run",
    )

    # Node package managers. Same hazard class as pip, different ecosystem:
    # a real ``npm install`` rewrites node_modules under the live checkout and
    # reaches the registry, and on Windows it is the *grandchild* of npm.cmd
    # that does the work — which is why it can wedge a whole test file rather
    # than merely fail it. No test in this suite runs one for real.
    _NODE_PM_HEADS = ("npm", "npx", "yarn", "pnpm", "bun", "corepack")
    _NODE_PM_VERBS = (
        "install", "uninstall", "ci", "i", "add", "remove",
        "run", "exec", "rebuild", "update",
    )
    # ``curl … | sh``-style one-liners: fetch a remote script, pipe it into a
    # shell. Provider-supplied memory-backend setup snippets take this shape.
    # ``irm``/``invoke-restmethod`` are here because PowerShell's canonical
    # download-and-execute one-liner uses them, not ``iwr`` — their absence
    # was half of why the cua-driver installer walked straight past this guard
    # (see _is_powershell_remote_exec for the other half).
    _REMOTE_FETCH_HEADS = (
        "curl", "wget", "iwr", "invoke-webrequest", "irm", "invoke-restmethod",
    )
    _REMOTE_SHELL_HEADS = ("sh", "bash", "zsh", "dash", "iex", "invoke-expression")

    # PowerShell download-and-execute: the Windows twin of ``curl … | sh``.
    _PS_HEADS = ("powershell", "pwsh")
    _PS_FETCH_CMDLETS = (
        "irm", "invoke-restmethod", "iwr", "invoke-webrequest",
        "downloadstring", "downloadfile", "start-bitstransfer",
    )
    _PS_EXEC_CMDLETS = ("iex", "invoke-expression")
    _PS_B64_RE = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")

    def _exe_head(tok: str) -> str:
        head = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        # ``.cmd``/``.bat``/``.ps1`` matter as much as ``.exe`` here: on
        # Windows ``find_node_executable("npm")`` resolves to ``npm.cmd``, so
        # a suffix-blind head would miss every real npm invocation.
        for _suffix in (".exe", ".cmd", ".bat", ".ps1"):
            if head.endswith(_suffix):
                return head[: -len(_suffix)]
        return head

    def _is_foreign_interpreter(tok: str) -> bool:
        """True if *tok* is a python executable other than the one we run."""
        if not _exe_head(tok).startswith("python"):
            return False
        try:
            return _os.path.normcase(_os.path.realpath(tok)) != _os.path.normcase(
                _os.path.realpath(sys.executable)
            )
        except Exception:
            return False

    def _cmd_tokens(cmd) -> list:
        """Tokenise *cmd* without destroying Windows paths.

        A list/tuple argv is already tokenised — joining it into a string and
        re-splitting with ``shlex`` (posix mode) silently eats the backslashes,
        so ``C:\\…\\node\\npm.cmd`` collapses to ``C:…nodenpm.cmd`` and no
        basename check can match it. That is not hypothetical: it is why the
        first version of this guard failed to fire on the very npm install it
        was written to catch. Only genuine command *strings* get shlex, and
        those have separators normalised first.
        """
        if isinstance(cmd, (list, tuple)):
            return [str(t) for t in cmd]
        cmd_str = _cmd_to_string(cmd).replace("\\", "/")
        try:
            return _shlex.split(cmd_str)
        except ValueError:
            return cmd_str.split()

    def _cmd_words(cmd) -> list:
        """``_cmd_tokens`` plus the words *inside* each argv element.

        A shell-wrapped command carries the whole real command in ONE argv
        element — ``["bash", "-c", "systemctl --user restart hermes-gateway"]``
        — so a predicate that only looks at argv elements never sees
        ``systemctl`` or ``restart`` as tokens. The old join+``shlex`` route
        happened to split those apart, which is why the shell-wrapped cases
        were caught; it also ate Windows backslashes, which is why the
        absolute-path cases were not. Splitting each element on whitespace
        keeps both: separators inside a token are untouched, so
        ``C:\\Windows\\System32\\taskkill.exe`` survives whole (a path with a
        space still keeps its basename in the final word).

        Surrounding quotes are stripped because ``shlex`` used to remove
        them, and a quoted element (``'"restart"'``) must not stop matching
        just because the tokeniser changed — the same reason
        ``_is_gateway_lifecycle_cmd`` strips them.
        """
        words = (w.strip("\"'") for tok in _cmd_tokens(cmd) for w in tok.split())
        return [w for w in words if w]

    def _is_package_install(cmd) -> bool:
        """True if *cmd* would install/remove packages in the LIVE venv.

        A test run must never mutate the developer's / gateway's venv. The
        live offender was ``tools/lazy_deps.py::_venv_pip_install``: any
        test that enables a platform (e.g. by setting ``FEISHU_APP_ID``)
        reaches ``gateway/config.py::_apply_env_overrides`` ->
        ``entry.check_fn()``, which — per its own comment there — "lazy-
        INSTALLS the platform SDK (pip) as a side effect". One
        ``pytest tests/gateway`` run installed lark_oapi 1.6.8 (101 MB,
        21,169 files) into ``~/.hermes/agent-src/.venv`` and timed out
        mid-install.

        Installs redirected somewhere disposable are still allowed: a
        ``--target``/``--prefix``/``--root``/``--python`` install, or one
        run against a different interpreter (a throwaway venv built by the
        test), cannot touch the running environment.

        Tokenisation goes through ``_cmd_tokens``, NOT a join + posix
        ``shlex.split``: the latter eats the backslashes in a Windows argv, so
        ``[r"C:\\…\\venv\\Scripts\\python.exe", "-m", "ensurepip", …]``
        collapsed to a single ``C:…venvScriptspython.exe`` token whose
        basename no longer starts with ``python``. ``_is_foreign_interpreter``
        then answered False and the throwaway-venv exemption — the one this
        docstring and the RuntimeError both advertise — never fired. That is
        why ``venv.create(tmp_path / "venv", with_pip=True)`` (CPython shells
        out to ``<newvenv>/Scripts/python.exe -m ensurepip``) was blocked on
        Windows while the identical call was correctly allowed on POSIX. Same
        defect class ``_cmd_tokens`` was introduced for in the Node guard.
        """
        tokens = _cmd_tokens(cmd)
        if not tokens:
            return False

        heads = [_exe_head(t) for t in tokens]
        # ``ensurepip`` bootstraps pip INTO the venv — a mutation with no
        # "install" verb of its own.
        if "ensurepip" not in heads:
            if not any(_PIP_HEAD.match(h) for h in heads):
                return False
            if not any(t in _INSTALL_VERBS for t in tokens):
                return False

        for tok in tokens:
            if tok.split("=", 1)[0] in _INSTALL_REDIRECT_FLAGS:
                return False
        return not _is_foreign_interpreter(tokens[0])

    def _is_node_package_install(cmd) -> bool:
        """True if *cmd* would really run a Node package manager.

        This is the tripwire for a defect class the pip guard above could not
        see. Production call sites were converted from ``subprocess.run`` to
        ``hermes_cli._subprocess_compat.run_text_capture`` (because npm.cmd
        puts the real work in a grandchild that inherits the capture pipes and
        so defeats ``timeout``). Tests that patched ``subprocess.run`` stopped
        intercepting anything and silently reached a REAL ``npm install`` —
        which merely *fails* an assertion here, but in
        tests/gateway/test_whatsapp_connect.py wedged the run until
        pytest-timeout killed the process, reporting the whole file to the
        nightly gate as "no tests ran".

        Blocking at ``subprocess.Popen`` catches it regardless of which
        helper the call site uses, so re-pointing a mock can never regress
        this way again.
        """
        tokens = _cmd_tokens(cmd)
        if not tokens:
            return False
        heads = [_exe_head(t) for t in tokens]
        if heads[0] in _NODE_PM_HEADS:
            return True
        # Shell-wrapped forms (``cmd /c npm ci``, ``sh -c "npm install"``)
        # only count alongside a package-manager verb, so that a command
        # merely *mentioning* npm in an argument is not blocked.
        return any(h in _NODE_PM_HEADS for h in heads) and any(
            t in _NODE_PM_VERBS for t in tokens
        )

    def _is_remote_installer_pipe(cmd) -> bool:
        """True if *cmd* downloads a script and pipes it into a shell.

        ``curl -fsSL https://…/install.sh | sh`` executes whatever the remote
        host serves, on the developer's machine, from a test run.
        """
        cmd_str = _cmd_to_string(cmd)
        low = cmd_str.lower()
        if "://" not in low or "|" not in low:
            return False
        segments = low.split("|")
        if not any(
            seg.split() and _exe_head(seg.split()[0]) in _REMOTE_FETCH_HEADS
            for seg in segments
        ):
            return False
        return any(
            seg.split() and _exe_head(seg.split()[0]) in _REMOTE_SHELL_HEADS
            for seg in segments[1:]
        )

    def _ps_decoded_payloads(tokens):
        """Yield decoded ``-EncodedCommand`` payloads (base64 UTF-16LE).

        Without this the guard is one flag away from being bypassed: the exact
        same one-liner survives verbatim as
        ``powershell -EncodedCommand <base64>``. Every token is tried rather
        than parsing the flag name, because PowerShell accepts any unambiguous
        prefix (``-e``, ``-en``, ``-enc``, …); a token that is not valid base64
        UTF-16LE simply yields nothing. Only reached once the head is already
        known to be PowerShell, so this costs nothing on other spawns.
        """
        import base64 as _b64

        for tok in tokens:
            if not _PS_B64_RE.match(tok):
                continue
            try:
                raw = _b64.b64decode(tok, validate=True)
            except Exception:
                continue
            yield raw.decode("utf-16-le", errors="ignore").lower()

    def _is_powershell_remote_exec(cmd) -> bool:
        """True if *cmd* is PowerShell fetching a remote script and running it.

        ``_is_remote_installer_pipe`` cannot see this shape. It splits the
        command on ``|`` and requires a fetch verb to HEAD a segment, but in
        ``powershell -Command "irm … | iex"`` the whole pipeline sits inside a
        single argv element, so segment 0 is headed by ``powershell`` and the
        fetch verb heads nothing. ``iex (irm …)`` has no pipe at all.

        This is the gap the 2026-08-16 incident fell through: a test stub
        pointed at a not-yet-called seam let
        ``hermes_cli/tools_config.py``'s real
        ``powershell -Command "irm …/install.ps1 | iex"`` run against the
        network and hang the session, with this guard sitting directly
        underneath it saying nothing.

        Requires ALL THREE of a URL, a fetch cmdlet and an exec cmdlet — so the
        in-repo PowerShell callers that merely query CIM or format JSON
        (``hermes_cli/claw.py``, ``session_bridge/mcp_server.py``,
        ``tools/environments/local.py``) keep working. A command that only
        mentions a URL is not blocked.
        """
        tokens = _cmd_words(cmd)
        if not tokens or _exe_head(tokens[0]) not in _PS_HEADS:
            return False
        payloads = [" ".join(tokens[1:]).lower()]
        payloads.extend(_ps_decoded_payloads(tokens[1:]))
        for text in payloads:
            if "://" not in text:
                continue
            if not any(f in text for f in _PS_FETCH_CMDLETS):
                continue
            if any(re.search(rf"\b{e}\b", text) for e in _PS_EXEC_CMDLETS):
                return True
        return False

    def _cmd_to_string(cmd) -> str:
        if cmd is None:
            return ""
        if isinstance(cmd, (bytes, bytearray)):
            try:
                return bytes(cmd).decode(errors="replace")
            except Exception:
                return ""
        if isinstance(cmd, str):
            return cmd
        if isinstance(cmd, (list, tuple)):
            try:
                return " ".join(str(t) for t in cmd)
            except Exception:
                return ""
        return str(cmd)

    def _matches_hermes_gateway(cmd_str: str) -> bool:
        low = cmd_str.lower()
        return any(tok in low for tok in _HERMES_TOKENS)

    def _is_blocked_systemctl(cmd) -> bool:
        cmd_str = _cmd_to_string(cmd)
        if "systemctl" not in cmd_str:
            return False
        if not _matches_hermes_gateway(cmd_str):
            return False
        # ``_cmd_words``, not ``shlex``: a list argv must not be joined and
        # re-split in posix mode (see _cmd_tokens).
        return any(verb in _cmd_words(cmd) for verb in _MUTATING_VERBS)

    def _is_process_killer(cmd) -> bool:
        cmd_str = _cmd_to_string(cmd)
        # Same tokenising rule as the Node guard: joining a list argv and
        # re-splitting it with posix ``shlex`` eats the backslashes, so
        # ``["C:\\Windows\\System32\\taskkill.exe", "/F", "/PID", "123"]``
        # collapsed to one ``C:WindowsSystem32taskkill.exe`` token whose
        # basename matched nothing and the guard stayed silent. Bare
        # ``taskkill`` still fired, which is why it went unnoticed.
        # ``_exe_head`` also strips the ``.exe``/``.cmd`` suffix, so the
        # Windows spelling of the basename is matched as well.
        tokens = _cmd_words(cmd)
        if not tokens:
            return False
        for tok in tokens:
            head = _exe_head(tok)
            if head in _PROCESS_KILLERS:
                low = cmd_str.lower()
                # pkill -f pattern: catch hermes-themed patterns + a
                # plain "python" -f which would catch the live gateway
                # whose cmdline contains "python -m hermes_cli.main".
                if (
                    "hermes" in low
                    or "gateway" in low
                    or ("python" in low and "-f" in tokens)
                ):
                    return True
        return False

    def _is_foreign_pid_kill(cmd) -> bool:
        """True for a killer command naming a LIVE pid outside our subtree.

        ``_is_process_killer`` above only fires when the command string also
        carries a hermes/gateway/python token, because it is written for the
        name-pattern form (``pkill -f hermes``). The PID form carries no such
        token at all:

            subprocess.run(["taskkill", "/PID", "14284", "/F"])   # Windows
            subprocess.run(["kill", "-9", "14284"])               # POSIX

        which is verbatim what ``hermes_cli.main._kill_stale_dashboard_
        processes`` builds after walking the real process table. Found
        2026-08-13, when four ``test_cmd_update_*`` tests printed
        "✓ stopped PID <n>" for the developer's four live dashboards. Those
        four survived only because the test had replaced ``subprocess.run``
        with its own recorder — the guard itself said nothing.

        A PID that does not resolve is allowed: ``_is_own_subtree`` treats a
        stale PID as harmless, which keeps the invented PIDs tests use
        (12345, 33940, …) working.
        """
        tokens = _cmd_words(cmd)
        if not tokens:
            return False
        if not any(_exe_head(t) in _PID_TARGETED_KILLERS for t in tokens):
            return False
        for tok in tokens:
            stripped = tok.strip("\"'")
            if not stripped.isdigit():
                continue
            pid = int(stripped)
            if pid > 0 and not _is_own_subtree(pid):
                return True
        return False

    def _is_gateway_lifecycle_cmd(cmd) -> bool:
        """True for a subprocess that would START or STOP a real gateway.

        The systemctl guard above only fires when ``systemctl`` is in the
        command, so the *direct* spawn paths — the ones actually used on
        Windows and in every ``--detached`` flow — sailed straight through:

            subprocess.Popen(["hermes", "gateway", "run"])                 # launch_gateway_detached
            subprocess.Popen([pythonw, "-m", "hermes_cli.main", ...])      # gateway_windows._spawn_detached

        A test that stubs *some* of the launch surface but not all of it
        (e.g. stubs ``run_gateway`` but not ``launch_gateway_detached``,
        then hits the Windows branch that defaults to detached) reaches the
        real Popen and puts a background gateway on the developer's machine.
        That is exactly what
        ``test_gateway_restart_on_windows_preserves_failure_fallback`` did on
        every run until 82b130b6e — invisibly, because the test asserted on a
        ``calls`` list, so the unstubbed real spawn merely failed an
        assertion instead of announcing that it had launched a process.

        Detection is token-based rather than substring-based so that an
        absolute entrypoint (``C:\\...\\hermes.exe gateway run``) is caught as
        readily as the bare ``hermes gateway run``.
        """
        cmd_str = _cmd_to_string(cmd)
        if not cmd_str:
            return False
        low = cmd_str.lower()
        if "gateway" not in low:
            return False
        if "systemctl" in low:
            return False  # already covered by _is_blocked_systemctl

        # Normalise path separators so basenames are comparable, and avoid
        # shlex here: on Windows it eats the backslashes in a real argv.
        tokens = [t.strip("\"'") for t in low.replace("\\", "/").split()]
        basenames = [t.rsplit("/", 1)[-1] for t in tokens]

        # ``python -m gateway.run`` / ``gateway/run.py`` launch the gateway
        # with no ``gateway`` subcommand token at all.
        if "gateway/run.py" in low or "gateway.run" in basenames:
            return True

        # Otherwise require a Hermes entrypoint AND `gateway <lifecycle-verb>`.
        has_entry = any(
            b.startswith("hermes") or b in ("hermes_cli.main", "hermes_cli")
            for b in basenames
        )
        if not has_entry:
            return False
        for idx, base in enumerate(basenames):
            if base != "gateway":
                continue
            for nxt in basenames[idx + 1:]:
                if nxt.startswith("-"):
                    continue  # skip flags like --replace's leading dashes
                return nxt in _GATEWAY_LIFECYCLE_VERBS
        return False

    def _check_subprocess_cmd(name, cmd):
        if _is_gateway_lifecycle_cmd(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — this would start or stop a "
                "REAL Hermes gateway process on this machine. The test "
                "reached a genuine launch/stop path, which means part of "
                "the spawn surface is unstubbed. Stub the specific "
                "function the code path actually calls — e.g. "
                "launch_gateway_detached, gateway_windows._spawn_detached, "
                "_launch_detached_gateway, _spawn_gateway_restart_watcher, "
                "launch_detached_profile_gateway_restart — and remember "
                "that the Windows branch of _gateway_command_inner defaults "
                "the recovery relaunch to DETACHED (`detached = "
                "is_windows()` when args.detached is None), so stubbing "
                "run_gateway alone is not enough. Mark with "
                "@pytest.mark.live_system_guard_bypass only if a real "
                "gateway process is genuinely required."
            )
        if _is_blocked_systemctl(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — would mutate the "
                "live hermes-gateway systemd unit. Mock "
                "subprocess.run / _run_systemctl in the test, or "
                "mark with @pytest.mark.live_system_guard_bypass."
            )
        if _is_process_killer(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — process-killer command "
                "targeting hermes/python could hit the live gateway. "
                "Mark with @pytest.mark.live_system_guard_bypass if "
                "intentional."
            )
        if _is_foreign_pid_kill(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — this terminates a LIVE "
                "process outside the test subtree by PID. Reaching here "
                "means the test walked the real process table. The two "
                "known scanners (hermes_cli.main._find_stale_dashboard_pids, "
                "hermes_cli.gateway._scan_gateway_pids) already default to "
                "[] via _pid_scan_guard, so this is either a real_*_pid_scan "
                "opt-out that did not stub the process table beneath it, or "
                "a third scanner that needs a _PID_SCAN_SPECS entry. Stub the "
                "scan so the code under test never sees a real PID, or mark "
                "with @pytest.mark.live_system_guard_bypass if terminating a "
                "real foreign process is genuinely the point."
            )
        if _is_package_install(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — this would install or remove "
                "packages in the LIVE venv that the developer and the "
                "hermes-gateway run from. A `pytest tests/gateway` run "
                "reached tools/lazy_deps.py::_venv_pip_install this way "
                "(gateway/config.py::_apply_env_overrides -> entry.check_fn(), "
                "which lazy-installs the platform SDK as a side effect) and "
                "pulled lark_oapi 1.6.8 — 101 MB, 21,169 files — into "
                "~/.hermes/agent-src/.venv, timing out mid-install. "
                "Stub tools.lazy_deps._venv_pip_install (or whichever "
                "installer the code under test calls). If the install is the "
                "point, redirect it with --target/--prefix/--root/--python or "
                "run it against a throwaway venv's interpreter — both pass "
                "through this guard — or mark with "
                "@pytest.mark.live_system_guard_bypass."
            )
        if _is_node_package_install(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — this would run a REAL Node "
                "package manager: network access, a rewritten node_modules "
                "under the live checkout, and (on Windows) a node grandchild "
                "that can wedge the whole test file until pytest-timeout "
                "kills it, which the nightly gate reports as 'no tests ran'. "
                "Reaching here means the mock is not intercepting. The "
                "install call sites use "
                "hermes_cli._subprocess_compat.run_text_capture, NOT "
                "subprocess.run. Patch it where the call site resolves it: "
                "for a function-local import (the WhatsApp adapter, cli.py) "
                "patch 'hermes_cli._subprocess_compat.run_text_capture'; for "
                "a module-level import (hermes_cli.web_server, "
                "agent.lsp.install) the name is already bound, so patch "
                "'<that module>.run_text_capture' instead. Mark with "
                "@pytest.mark.live_system_guard_bypass only if a real "
                "package install is genuinely the point."
            )
        if _is_powershell_remote_exec(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — this is PowerShell's "
                "download-and-execute one-liner (irm/iwr/DownloadString piped "
                "or passed into iex). It runs whatever the remote host serves, "
                "on this machine, from a test run. On 2026-08-16 exactly this "
                "argv — hermes_cli/tools_config.py's cua-driver install, "
                "'irm .../install.ps1 | iex' — escaped a stub that was pointed "
                "at a seam the code did not call yet, reached the network, and "
                "hung the session on the capture-pipe reader thread. Patch the "
                "transport too: stub 'subprocess.Popen' (which every helper "
                "bottoms out in) alongside "
                "'hermes_cli._subprocess_compat.run_text_capture', so a dead "
                "seam fails closed instead of shelling out. Mark with "
                "@pytest.mark.live_system_guard_bypass only if genuinely "
                "intended."
            )
        if _is_remote_installer_pipe(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — this fetches a remote script "
                "and pipes it into a shell, executing whatever the remote "
                "host serves on this machine from a test run. Reaching here "
                "means the mock is not intercepting; "
                "hermes_cli.web_server._run_setup_command uses "
                "run_text_capture, not subprocess.run. Mark with "
                "@pytest.mark.live_system_guard_bypass only if genuinely "
                "intended."
            )
        # Block any subprocess that would run `hermes update` (or the
        # equivalent `python -m hermes_cli.main update`).  These commands
        # run `git fetch origin + git pull` against the REAL checkout,
        # overwriting files like pyproject.toml mid-test-run and corrupting
        # every subsequent subprocess that reads them.  The corruption is
        # especially insidious because the spawned process uses setsid/
        # start_new_session=True, making it invisible to pytest's process
        # tree (PPid=1) and nearly impossible to trace without explicit
        # inotify/SHA watchdogs.  Any test that legitimately needs to exercise
        # the update-spawn path must mock subprocess.Popen explicitly.
        cmd_str = _cmd_to_string(cmd)
        low = cmd_str.lower()
        if "update" in low and (
            # hermes update / hermes update --gateway / setsid bash -c ... hermes update
            ("hermes" in low and "update" in low.split())
            or
            # python -m hermes_cli.main update --gateway
            ("hermes_cli" in low and "update" in low.split())
            or
            # venv/bin/hermes update  (absolute path variant used in tests)
            (".venv/bin/hermes" in low and "update" in low)
        ):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — this command would run "
                "`hermes update` against the real checkout, fetching "
                "from origin and overwriting repo files (e.g. "
                "pyproject.toml) mid-test-run. This corrupts every "
                "subsequent subprocess in the same runner. "
                "Mock subprocess.Popen (and subprocess.run if used) "
                "in the test instead, or mark with "
                "@pytest.mark.live_system_guard_bypass if genuinely "
                "needed (e.g. an integration test testing the update "
                "flow against a dedicated throwaway repo)."
            )

    def _wrap_subprocess(name, real):
        def _guarded(cmd, *args, **kwargs):
            _check_subprocess_cmd(name, cmd)
            return real(cmd, *args, **kwargs)
        _guarded.__name__ = f"_guarded_{name}"
        # Make the wrapper subscriptable like the wrapped callable when
        # the wrapped object is. ``subprocess.Popen[bytes]`` is used as
        # a type annotation in third-party packages (mcp, etc.); replacing
        # ``Popen`` with a plain function breaks ``Popen[bytes]`` at
        # import time. Defer ``__class_getitem__`` to the original.
        if hasattr(real, "__class_getitem__"):
            _guarded.__class_getitem__ = real.__class_getitem__
        return _guarded

    def _wrap_popen():
        """Subclass Popen so isinstance checks AND Popen[bytes] still work."""
        real = _subprocess.Popen

        class _GuardedPopen(real):  # type: ignore[misc, valid-type]
            def __init__(self, cmd, *args, **kwargs):
                _check_subprocess_cmd("Popen", cmd)
                super().__init__(cmd, *args, **kwargs)

        _GuardedPopen.__name__ = "Popen"
        _GuardedPopen.__qualname__ = "Popen"
        return _GuardedPopen

    real_run = _subprocess.run
    real_call = _subprocess.call
    real_check_call = _subprocess.check_call
    real_check_output = _subprocess.check_output
    real_getoutput = _subprocess.getoutput
    real_getstatusoutput = _subprocess.getstatusoutput

    monkeypatch.setattr(_subprocess, "run", _wrap_subprocess("run", real_run))
    monkeypatch.setattr(_subprocess, "Popen", _wrap_popen())
    monkeypatch.setattr(_subprocess, "call", _wrap_subprocess("call", real_call))
    monkeypatch.setattr(
        _subprocess, "check_call", _wrap_subprocess("check_call", real_check_call)
    )
    monkeypatch.setattr(
        _subprocess,
        "check_output",
        _wrap_subprocess("check_output", real_check_output),
    )
    monkeypatch.setattr(
        _subprocess, "getoutput", _wrap_subprocess("getoutput", real_getoutput)
    )
    monkeypatch.setattr(
        _subprocess,
        "getstatusoutput",
        _wrap_subprocess("getstatusoutput", real_getstatusoutput),
    )

    # os.system / os.popen — same risk class, completely unwrapped before.
    real_os_system = _os.system
    real_os_popen = _os.popen

    def _guarded_os_system(command):
        _check_subprocess_cmd("os.system", command)
        return real_os_system(command)

    def _guarded_os_popen(cmd, *args, **kwargs):
        _check_subprocess_cmd("os.popen", cmd)
        return real_os_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(_os, "system", _guarded_os_system)
    monkeypatch.setattr(_os, "popen", _guarded_os_popen)

    # pty.spawn — POSIX-only.
    try:
        import pty as _pty
        if hasattr(_pty, "spawn"):
            real_pty_spawn = _pty.spawn

            def _guarded_pty_spawn(argv, *args, **kwargs):
                _check_subprocess_cmd("pty.spawn", argv)
                return real_pty_spawn(argv, *args, **kwargs)

            monkeypatch.setattr(_pty, "spawn", _guarded_pty_spawn)
    except Exception:
        pass

    # asyncio.create_subprocess_* — bypasses subprocess module entirely.
    try:
        import asyncio as _asyncio
        real_async_exec = _asyncio.create_subprocess_exec
        real_async_shell = _asyncio.create_subprocess_shell

        async def _guarded_async_exec(program, *args, **kwargs):
            _check_subprocess_cmd(
                "asyncio.create_subprocess_exec", [program, *args]
            )
            return await real_async_exec(program, *args, **kwargs)

        async def _guarded_async_shell(cmd, *args, **kwargs):
            _check_subprocess_cmd("asyncio.create_subprocess_shell", cmd)
            return await real_async_shell(cmd, *args, **kwargs)

        monkeypatch.setattr(_asyncio, "create_subprocess_exec", _guarded_async_exec)
        monkeypatch.setattr(
            _asyncio, "create_subprocess_shell", _guarded_async_shell
        )
    except Exception:
        pass

    yield


@pytest.fixture(autouse=True)
def _pid_scan_guard(request):
    """Choose whether this test gets the real host process-table scans.

    The wrappers themselves are installed at import time by
    ``_PidScanGuardFinder``; all this fixture does is flip the flag each wrapper
    reads when called. No import, no ``sys.modules`` lookup, no monkeypatch —
    which is why it cannot regress tests/test_conftest_import_cost.py.

    Tests of a scanner itself opt out with that scanner's marker
    (``@pytest.mark.real_gateway_pid_scan`` /
    ``@pytest.mark.real_dashboard_pid_scan``). Those are already hermetic and
    fast because they stub the process-table *source* beneath the scanner
    (``psutil.process_iter``, ``subprocess.run``, ``os.listdir``/``/proc``,
    ``shutil.which``), so they never touch the real host either. The markers are
    per-scanner rather than one shared marker so that opting into the dashboard
    walk does not silently re-enable the gateway sweep as well.

    Mirrors the existing ``real_concurrent_gate`` / ``real_agent_prewarm``
    autouse-stub-plus-opt-out-marker pattern.
    """
    for guarded in _PID_SCAN_SPECS:
        guarded.allow_real = (
            request.node.get_closest_marker(guarded.marker) is not None
        )
    try:
        yield
    finally:
        for guarded in _PID_SCAN_SPECS:
            guarded.allow_real = False


@pytest.fixture(autouse=True)
def _network_probe_guards(request):
    """Choose whether this test may reach the network through a guarded probe.

    Covers both guards — ``real_local_server_probe`` (agent.model_metadata,
    localhost) and ``real_provider_auth_probe`` (hermes_cli.auth /
    copilot_auth, the public internet). They are independent: opting out of one
    leaves the other stubbed, because wanting the real local-server waterfall
    is no reason to also start calling z.ai and api.github.com for real.

    The wrappers are installed at import time by each guard's ``find_spec``;
    this fixture only flips the flag they read at call time. No import, no
    ``sys.modules`` lookup, no monkeypatch — so it cannot regress
    tests/test_conftest_import_cost.py.

    The opt-out tests are already hermetic: they stub the HTTP layer *beneath*
    the probe, so they never reach the network either — the same shape as the
    ``real_gateway_pid_scan`` opt-outs, which stub the process-table source
    beneath the scanner.
    """
    for guard in _NETWORK_PROBE_GUARDS:
        guard.allow_real[0] = request.node.get_closest_marker(guard.marker) is not None
    try:
        yield
    finally:
        for guard in _NETWORK_PROBE_GUARDS:
            guard.allow_real[0] = False


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    """Collect cycle-held sqlite connections before fixture finalizers run.

    ``sqlite3.Connection`` keeps its prepared-statement cache in a reference
    cycle, so a connection a test opened and dropped is NOT freed by plain
    refcounting — it waits for a cyclic-GC pass. Until it is freed, its
    ``__del__`` has not closed the file, and on Windows the open handle blocks
    deletion of the directory holding the DB.

    ``tmp_path`` reclaims its directory with a best-effort
    ``rmtree(ignore_errors=True)``, so that failure is silent: the test passes
    and the tree survives in %TEMP% (30.4 GB / ~191K dirs by 2026-07-22).
    Because collection is timing-dependent, only *some* tests stranded a dir,
    which is why the leak looked erratic.

    ``tryfirst`` puts this ahead of pytest's own teardown, which is what
    finalizes fixtures — so the connections are closed while ``tmp_path``'s
    finalizer can still act on them. Deliberately NOT a fixture: an autouse
    fixture is set up before ``tmp_path`` and therefore torn down *after* it,
    and one depending on ``tmp_path`` would force a temp dir for every test.

    Purely a lifetime nudge — it never changes when anything commits, closes,
    or rolls back, so it cannot alter test semantics.

    **Only tests that could have created such a connection pay for it.** A full
    ``gc.collect()`` walks the whole tracked heap (61K objects at collection
    time, 211K by the end of ``tests/hermes_cli/test_doctor.py``) and cost
    0.163s per test — 16.0s of that 98-test file, charged to EVERY test in the
    repo. Only 10 of those 98 tests ever opened a sqlite connection.
    ``_sqlite_opened_since_collect`` (see ``sqlite3.connect`` wrapper above)
    gates the call: 12 collects instead of 98, 2.1s instead of 16.0s, with no
    change for any test that actually touches sqlite.

    The gate deliberately stays armed for ONE more test after the last open.
    Fixture finalizers run *after* this ``tryfirst`` hook, so a connection whose
    last reference lives in a fixture becomes collectable only once this
    teardown is over; the following test's collect is what frees it (the same
    ordering the unconditional version relied on).
    """
    global _sqlite_opened_since_collect, _collect_armed
    if _sqlite_opened_since_collect or _collect_armed:
        _collect_armed = bool(_sqlite_opened_since_collect)
        _sqlite_opened_since_collect = 0
        gc.collect()


@pytest.fixture(autouse=True)
def _no_live_checkout_on_sys_path():
    """Strip live-``~/.hermes`` entries a test leaves behind on ``sys.path``.

    Several tests exec a DEPLOYED script out of the live Hermes root --
    ``tests/devflow_delegation/test_observability.py::_load_observability``
    walks its own ancestors up out of the worktree until it finds
    ``profiles/main/scripts/devflow_observability.py`` and ``exec_module``s it.
    That script's line 34 is::

        sys.path.insert(0, str(HERMES_ROOT / "agent-src"))

    which is correct for the script and catastrophic for the test process: the
    entry is never removed, so it accumulates (22 copies after that one file)
    and sits AHEAD of the checkout under test. Every later first-time import of
    a Hermes package then comes from the shared ``~/.hermes/agent-src``
    checkout instead of this worktree.

    Measured: after that file, ``events.subscribers.scribe_action_telemetry``
    resolved to the shared checkout, so a full-suite audit reported live-root
    writes from code that had already been fixed here. Worse, the repo runs
    ``pytest-randomly`` by default, so which tests get the deployed copy varies
    run to run -- a verification can pass or fail on ordering alone.

    Only entries that (a) were not present before the test and (b) resolve
    inside the real ``~/.hermes`` but outside this repo are removed, so a test
    that legitimately extends ``sys.path`` is untouched. Deliberately NOT
    evicting ``sys.modules``: anything already imported from the shared
    checkout stays bound -- a narrower, known residue -- because blanket
    eviction defangs monkeypatches other fixtures are holding.
    """
    before = list(sys.path)
    try:
        yield
    finally:
        if sys.path != before:
            seen = set(before)
            kept = []
            for entry in sys.path:
                if entry in seen:
                    kept.append(entry)
                    continue
                try:
                    resolved = Path(entry).resolve()
                except (OSError, ValueError):
                    kept.append(entry)
                    continue
                inside_live_hermes = (
                    resolved == _REAL_HERMES_ROOT or _REAL_HERMES_ROOT in resolved.parents
                )
                inside_repo = resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents
                if inside_live_hermes and not inside_repo:
                    continue
                kept.append(entry)
            sys.path[:] = kept


def pytest_sessionfinish(session, exitstatus):
    """Reclaim the collection-time HERMES_HOME pinned at conftest import.

    ``ignore_errors`` because the import-time ``setup_logging()`` this
    directory exists to absorb still holds an open handle on
    ``logs/agent.log``, and on Windows that blocks the unlink. Leaving a few
    KB in %TEMP% is the correct trade against failing a green run -- but do
    not drop the call: a per-session temp home that is never removed is the
    leak documented for the stress suite's e2e temp home.
    """
    if _SESSION_HERMES_HOME is not None:
        shutil.rmtree(_SESSION_HERMES_HOME, ignore_errors=True)
