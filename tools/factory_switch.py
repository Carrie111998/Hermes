#!/usr/bin/env python3
"""
factory_switch.py — the master OFF switch for the self-feeding factory loop.

His words: "how do you pause or stop a self-feeding system. you make a switch."
Every hook (dispatch, block-response) checks this FIRST-LINE before it fires.

FAIL-SAFE / DEADMAN (load-bearing): anything unexpected -> OFF. Absent file,
corrupt content, unknown flag, read error -> OFF. The system fails TOWARD quiesced,
never toward runaway. Default when the file does not exist is OFF (paused), because
a factory that starts itself on a missing flag is the opposite of a stop switch.

Positions:
  status  -> print current state (RUN | OFF), exit 0
  on      -> set RUN
  off     -> set OFF (loop starves gracefully; hooks no-op)
  kill    -> set OFF and pkill in-flight workers

Hooks call: `python3 factory_switch.py status` and act only on the literal "RUN".
"""
import sys
import subprocess
from pathlib import Path

FLAG = Path.home() / ".hermes" / "workspace" / ".factory_switch"
RUN = "RUN"
OFF = "OFF"


def read_state() -> str:
    """Deadman read: ANYTHING that is not a clean 'RUN' resolves to OFF."""
    try:
        if not FLAG.exists():
            return OFF  # absent -> OFF (never auto-run on a missing flag)
        raw = FLAG.read_text(encoding="utf-8").strip()
    except Exception:
        return OFF  # read error / corrupt -> OFF
    return RUN if raw == RUN else OFF  # only the exact token "RUN" runs; else OFF


def is_on() -> bool:
    """The single predicate hooks should use if importing instead of shelling out."""
    return read_state() == RUN


def set_state(state: str) -> None:
    FLAG.parent.mkdir(parents=True, exist_ok=True)
    FLAG.write_text(state + "\n", encoding="utf-8")


def kill_workers() -> int:
    """Best-effort terminate in-flight factory workers (kanban swarm / lane chats)."""
    n = 0
    for pat in ("kanban .* swarm", r"hermes -p .* chat"):
        try:
            r = subprocess.run(["pkill", "-f", pat], capture_output=True)
            if r.returncode == 0:
                n += 1
        except Exception:
            pass
    return n


def main() -> int:
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if arg == "status":
        print(f"factory switch: {read_state()}")
        return 0
    if arg == "on":
        set_state(RUN)
        print("factory switch: RUN (loop armed; hooks will dispatch)")
        return 0
    if arg == "off":
        set_state(OFF)
        print("factory switch: OFF (loop quiesces; hooks no-op)")
        return 0
    if arg == "kill":
        set_state(OFF)
        killed = kill_workers()
        print(f"factory switch: OFF + kill (pkill patterns matched: {killed})")
        return 0
    # unknown arg -> report OFF-safe, do not mutate
    print(f"factory switch: {read_state()} (unknown arg '{arg}'; use status|on|off|kill)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
