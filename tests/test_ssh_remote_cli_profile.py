"""Class repro for #88625: desktop SSH must not pass conn:<id>::<profile> as --profile.

Exercises the real buildSpawnCommand / ownership probe from apps/desktop
(no MagicMock, no source-regex). After the desktop vitest lands, this file
is the Gate-3 harness that run_tests_gated.sh can stamp.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DESKTOP = REPO / "apps" / "desktop"
OWNERSHIP_ID = "0123456789abcdef0123456789abcdef"
SPAWN_NONCE = "0123456789abcdef"


def _node_eval(script: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    assert node, "node is required to exercise desktop spawn helpers"
    return subprocess.run(
        [node, "--experimental-strip-types", "--input-type=module", "-e", script],
        cwd=DESKTOP,
        capture_output=True,
        text=True,
        check=False,
    )


def test_build_spawn_command_omits_desktop_composite_scope_as_profile():
    script = f"""
import {{ buildSpawnCommand, spawnLogPath }} from './electron/remote-lifecycle.ts'
const cmd = buildSpawnCommand('/x/hermes', 'conn:yun3d::default', {{
  logPath: spawnLogPath({OWNERSHIP_ID!r}, {SPAWN_NONCE!r}),
}})
if (cmd.includes('--profile') || cmd.includes('conn:yun3d')) {{
  console.error(cmd)
  process.exit(2)
}}
if (!cmd.includes('serve --isolated')) {{
  console.error(cmd)
  process.exit(3)
}}
"""
    result = _node_eval(script)
    assert result.returncode == 0, result.stderr + result.stdout


def test_build_spawn_command_still_pins_a_valid_profile_name():
    script = f"""
import {{ buildSpawnCommand, spawnLogPath }} from './electron/remote-lifecycle.ts'
const cmd = buildSpawnCommand('/x/hermes', 'writer_2', {{
  logPath: spawnLogPath({OWNERSHIP_ID!r}, {SPAWN_NONCE!r}),
}})
if (!cmd.includes('--profile') || !cmd.includes('writer_2')) {{
  console.error(cmd)
  process.exit(2)
}}
"""
    result = _node_eval(script)
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.linux_only
def test_ownership_probe_dead_pid_is_foreign_not_a_crash():
    """Dead PID must print FOREIGN (exit 0). CalledProcessError from ps is the bug."""
    script = f"""
import {{ pidIsOurDashboard }} from './electron/remote-lifecycle.ts'
import {{ execFileSync }} from 'node:child_process'
const ssh = {{
  async exec(cmd) {{
    try {{
      return execFileSync('bash', ['-lc', cmd], {{ encoding: 'utf8' }})
    }} catch (error) {{
      throw error
    }}
  }},
}}
const owned = await pidIsOurDashboard(ssh, 2147483647, {SPAWN_NONCE!r}, '/x/hermes')
if (owned !== false) {{
  console.error('expected FOREIGN/false, got', owned)
  process.exit(2)
}}
"""
    result = _node_eval(script)
    assert result.returncode == 0, result.stderr + result.stdout
