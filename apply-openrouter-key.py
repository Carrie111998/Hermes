"""Fix #1: Sync OPENROUTER_API_KEY in .env from config.yaml so the runtime
actually sees the key. The env var takes precedence over config.yaml, and an
empty `OPENROUTER_API_KEY=` line in .env was overriding the working key in
config.yaml. Idempotent — exits 0 if already in sync.
"""
from pathlib import Path
import re
import sys

HERMES_HOME = Path.home() / "AppData" / "Local" / "hermes"
CONFIG = HERMES_HOME / "config.yaml"
ENV = HERMES_HOME / ".env"

# Pull api_key from config.yaml model.api_key
text = CONFIG.read_text(encoding="utf-8")
m = re.search(r'^model:\s*\n(?:[ \t]+.*\n)*?[ \t]+api_key:\s*"([^"]+)"', text, re.MULTILINE)
if not m:
    print("[FAIL] could not find model.api_key in config.yaml")
    sys.exit(1)
api_key = m.group(1).strip()
if not api_key or "REPLACE" in api_key.upper():
    print(f"[FAIL] model.api_key in config.yaml is not a real key: {api_key[:20]}...")
    sys.exit(1)

masked = api_key[:10] + "..." + api_key[-4:]
print(f"[INFO] config.yaml model.api_key = {masked}")

# Read .env, replace the OPENROUTER_API_KEY line
env_text = ENV.read_text(encoding="utf-8")
lines = env_text.splitlines(keepends=True)
changed = False
found = False
for i, line in enumerate(lines):
    stripped = line.lstrip()
    if stripped.startswith("#"):
        continue
    if re.match(r"^OPENROUTER_API_KEY\s*=", line):
        found = True
        # Preserve trailing newline / line ending
        newline = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
        current_val = line.split("=", 1)[1].strip().strip('"').strip("'") if newline else ""
        if current_val == api_key:
            print(f"[SKIP] OPENROUTER_API_KEY in .env already matches config.yaml")
            sys.exit(0)
        # Replace
        new_line = f"OPENROUTER_API_KEY={api_key}{newline}"
        if lines[i] != new_line:
            lines[i] = new_line
            changed = True
        break

if not found:
    # Append
    newline = "\r\n" if env_text.endswith("\r\n") or "\r\n" in env_text[:200] else "\n"
    lines.append(f"{newline}OPENROUTER_API_KEY={api_key}{newline}")
    changed = True

if changed:
    ENV.write_text("".join(lines), encoding="utf-8")
    print(f"[ OK ] wrote OPENROUTER_API_KEY={masked} to .env")
else:
    print(f"[SKIP] no change needed")
