"""Tests for workspace_runtime.discovery.

Five states are exercised against a real temporary filesystem:
  1. inside_workspace     — all 4 canonical signals
  2. partial_workspace    — 2 of 4 canonical signals
  3. multi_workspace      — two candidate roots
  4. not_a_workspace      — bare /tmp
  5. discovery_error      — path that does not exist

Plus the canonical workspace (`/home/taras/projects`) is exercised as a
real-world smoke test against the actual filesystem (read-only).

The verdict encoder is exercised for byte-stability: the same verdict
must produce a byte-identical block on every call.
"""
