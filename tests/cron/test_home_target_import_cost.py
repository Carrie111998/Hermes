"""Regression: expanding the ``all`` routing token must not import platform SDKs.

Why this exists
---------------
``_iter_home_target_platforms`` yields the built-in platforms from
``_HOME_TARGET_ENV_VARS`` and then asks the platform registry for *plugin*
platforms that declare a ``cron_deliver_env_var``. It used to do that via
``platform_registry.plugin_entries()``, which calls ``_resolve_all()`` and runs
**every** deferred platform loader — importing the full adapter module, and with
it the whole vendor SDK, for all ~18 bundled platforms.

The only thing the loop wants from each entry is a single metadata string. The
last line of the loop already discards any entry whose name is in
``_HOME_TARGET_ENV_VARS`` — but only *after* paying for its import. Eleven of
the bundled platforms (telegram, discord, slack, matrix, whatsapp, feishu,
wecom, mattermost, sms, email, dingtalk) are exactly those built-ins, and they
are the expensive ones.

Measured on a dev box before the fix: ``deliver="all"`` expansion cost **102.0s**
of ``call`` phase and pulled 721 modules — telegram 23.8s, discord 21.8s,
hermes_cli 13.7s, aiohttp 8.4s, cryptography 7.3s. ``pyproject.toml`` sets
``--timeout=30`` with ``--timeout-method=thread``, which hard-exits the
interpreter and destroys the run's summary line, so in a monolithic run whichever
test first triggered this died and took the summary with it.

Filtering by *name* before resolving is semantically identical — the loop
already rejects those names — and skips the imports entirely.

This test runs a SUBPROCESS on purpose: ``sys.modules`` is process-global, so a
sibling test that legitimately imports ``telegram`` would mask the regression.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Vendor SDK roots owned by bundled platform adapters. None of these is needed
# to decide which platforms have a configured home channel.
FORBIDDEN_ROOTS = (
    "telegram",      # python-telegram-bot  (23.8s)
    "discord",       # discord.py           (21.8s)
    "slack_sdk",
    "slack_bolt",
    "lark_oapi",     # feishu
    "nio",           # matrix
    "qrcode",        # whatsapp pairing
    "PIL",
)

_SCRIPT = '''\
import json, os, sys

# Two built-in home channels configured; the expansion must find them.
os.environ["TELEGRAM_HOME_CHANNEL"] = "-111"
os.environ["DISCORD_HOME_CHANNEL"] = "-222"
os.environ.pop("SIGNAL_HOME_CHANNEL", None)
os.environ.pop("MATRIX_HOME_ROOM", None)

import cron.scheduler as _sched
from cron.scheduler import _expand_routing_tokens

expanded = _expand_routing_tokens("all")

roots = sorted({m.split(".")[0] for m in list(sys.modules)})
with open(os.environ["OUT"], "w", encoding="utf-8") as fh:
    json.dump({
        "expanded": sorted(expanded),
        "roots": roots,
        "sched_file": _sched.__file__,
    }, fh)
'''


@pytest.mark.timeout(300)
def test_all_expansion_does_not_import_platform_sdks(tmp_path):
    """``all`` expansion must resolve home channels without loading vendor SDKs."""
    script = tmp_path / "expand.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    out = tmp_path / "result.json"

    env = dict(os.environ)
    env["OUT"] = str(out)
    # Keep the child off the lazy-install path so a slow/absent optional dep
    # can never turn this into a pip run. See tools/lazy_deps.py.
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    # ``python <script>`` puts the SCRIPT's dir on sys.path[0], not the cwd, so
    # without this the child imports ``cron`` from whatever checkout the
    # editable install points at — i.e. it would silently test a different tree
    # than the one under test. It also shadows a site-packages ``utils.py``
    # that otherwise wins over the repo's own ``utils``.
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) + os.pathsep + existing if existing else str(REPO_ROOT)
    )

    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert out.exists(), (
        "child never wrote its result.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    data = json.loads(out.read_text(encoding="utf-8"))

    # Guard against measuring the wrong checkout: an editable install can point
    # ``cron`` at a different tree entirely, which would make this test pass
    # against code that is not the code under test.
    assert Path(data["sched_file"]).resolve().is_relative_to(REPO_ROOT), (
        f"child imported cron.scheduler from {data['sched_file']!r}, "
        f"which is outside the tree under test ({REPO_ROOT})."
    )

    # Behaviour is preserved: the configured built-ins still expand.
    assert "telegram" in data["expanded"]
    assert "discord" in data["expanded"]
    # ...and unconfigured ones stay out.
    assert "signal" not in data["expanded"]
    assert "matrix" not in data["expanded"]

    offenders = sorted(set(data["roots"]) & set(FORBIDDEN_ROOTS))
    assert not offenders, (
        f"Expanding deliver='all' imported {len(offenders)} vendor SDK "
        f"root(s): {offenders}. _iter_home_target_platforms must filter "
        "deferred platform names against _HOME_TARGET_ENV_VARS BEFORE "
        "resolving them, instead of calling plugin_entries() which runs every "
        "deferred loader."
    )
