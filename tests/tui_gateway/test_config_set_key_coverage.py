"""Every config.set key the TUI sends must reach a handler, not the fall-through.

``show_message_tokens`` shipped as a slash command that issued a ``config.set``
the gateway had never been taught, so the RPC fell through to ``unknown config
key`` and the preference silently vanished on restart. Adding that one key
fixes today's bug; this test is what stops the *next* key repeating it, by
pinning the client's vocabulary against the server's.

It is a source-level cross-check on purpose: invoking each key for real would
mutate the on-disk config, and the failure mode being guarded is structural —
a key exists on one side of the wire and not the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import tui_gateway.server as srv

_SERVER_PY = Path(srv.__file__).resolve()
_REPO_ROOT = _SERVER_PY.parent.parent
_COMMANDS_DIR = _REPO_ROOT / "ui-tui" / "src" / "app" / "slash" / "commands"

# Keys whose payload is assembled by a helper rather than written inline at the
# rpc call site, so the scanner below cannot see them.
_HELPER_BUILT_KEYS = {
    "reasoning",  # session.ts -> reasoningConfigPayload()
}

# A canary for the scanner itself: if the regexes ever stop matching, the
# coverage assertion would pass vacuously over an empty set.
_KEYS_KNOWN_TO_BE_SENT = {"density", "model", "show_message_tokens", "theme"}

_KEY_LITERAL = re.compile(r"key:\s*(?:'([A-Za-z_.]+)'|`([A-Za-z_.]*)\$\{)")


def _keys_sent_by_tui() -> set[str]:
    """Key literals passed to config.set from the TUI slash commands.

    Template-literal keys (``details_mode.${section}``) contribute their static
    prefix, which is what the server matches with ``startswith``.
    """
    found: set[str] = set(_HELPER_BUILT_KEYS)
    for path in sorted(_COMMANDS_DIR.glob("*.ts")):
        text = path.read_text(encoding="utf-8")
        for call in re.finditer(r"'config\.set'", text):
            # The payload object follows the method name; a generous window
            # covers the multi-line object literals without spilling into the
            # next call.
            window = text[call.end() : call.end() + 400]
            match = _KEY_LITERAL.search(window)
            if match:
                found.add(match.group(1) or match.group(2))
    return found


def _keys_handled_by_server() -> tuple[set[str], set[str]]:
    """Exact and prefix key matches in config.set, ahead of the fall-through."""
    source = _SERVER_PY.read_text(encoding="utf-8")
    start = source.index('@method("config.set")')
    end = source.index('f"unknown config key: {key}"', start)
    body = source[start:end]

    exact = set(re.findall(r'key == "([A-Za-z_.]+)"', body))
    for group in re.findall(r"key in \{([^}]*)\}", body):
        exact.update(re.findall(r'"([A-Za-z_.]+)"', group))
    prefixes = set(re.findall(r'key\.startswith\("([A-Za-z_.]+)"\)', body))
    return exact, prefixes


def test_scanner_still_sees_the_call_sites():
    """Guard against the coverage test passing over an empty scan."""
    assert _KEYS_KNOWN_TO_BE_SENT <= _keys_sent_by_tui()


def test_every_tui_config_key_has_a_handler():
    exact, prefixes = _keys_handled_by_server()
    orphans = sorted(
        key
        for key in _keys_sent_by_tui()
        if key not in exact and not any(key.startswith(p) for p in prefixes)
    )
    assert not orphans, (
        "config.set keys the TUI sends with no handler in tui_gateway/server.py — "
        f"they fall through to `unknown config key`: {orphans}"
    )
