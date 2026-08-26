"""Behavioral test for caps→config change (PR-A).

Verifies:
1. Defaults hold: no config keys → 4000/8000 behavior identical to stock.
2. Config keys raise ceilings.
3. Aggregate floor can't undercut per-section ceiling.
4. Garbage config values fall back to defaults.
"""
import os
import sys
import tempfile

# Isolated HERMES_HOME so the test never reads the real user config
_tmp = tempfile.mkdtemp()
os.environ["HERMES_HOME"] = _tmp

sys.path.insert(0, "/home/wisp/projects/hermes-agent")
from hermes_cli.plugins import (
    DEFAULT_SYSTEM_PROMPT_SECTION_MAX_CHARS,
    _system_prompt_section_max_chars,
    _system_prompt_sections_total_chars,
)

failures = []

def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'} {name}: got {got}, want {want}")
    if not ok:
        failures.append(name)

# --- 1. defaults (no config file) ---
check("default per-section", _system_prompt_section_max_chars(), 4_000)
check("default total", _system_prompt_sections_total_chars(), 8_000)

# --- 2. config raises ceilings ---
cfg_path = os.path.join(_tmp, "config.yaml")
with open(cfg_path, "w") as f:
    f.write(
        "plugins:\n"
        "  system_prompt_section_max_chars: 24000\n"
        "  system_prompt_sections_total_chars: 64000\n"
    )
check("config per-section", _system_prompt_section_max_chars(), 24_000)
check("config total", _system_prompt_sections_total_chars(), 64_000)

# --- 3. aggregate floor >= per-section ---
with open(cfg_path, "w") as f:
    f.write(
        "plugins:\n"
        "  system_prompt_section_max_chars: 24000\n"
        "  system_prompt_sections_total_chars: 4000\n"
    )
check("total clamped to per-section floor",
      _system_prompt_sections_total_chars(), 24_000)

# --- 4. garbage values fall back ---
with open(cfg_path, "w") as f:
    f.write(
        "plugins:\n"
        "  system_prompt_section_max_chars: hello\n"
        "  system_prompt_sections_total_chars: -5\n"
    )
check("garbage per-section falls back", _system_prompt_section_max_chars(), 4_000)
check("garbage total falls back", _system_prompt_sections_total_chars(), 8_000)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL BEHAVIORAL TESTS PASS")
