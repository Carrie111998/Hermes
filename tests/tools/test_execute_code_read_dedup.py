#!/usr/bin/env python3
"""End-to-end regression tests for the execute_code read-dedup boundary.

Issue #44843 / PR #44847.  ``read_file``'s dedup exists to save MODEL context:
on a repeated same-range read of an unchanged file it returns a
``status: "unchanged"`` stub instead of the content, because the earlier
content is still visible in the conversation.  A script running inside
``execute_code`` has no conversation, so the stub points at something it can
never see — and the documented sandbox contract
(``read_file(path) -> {"content": ..., "total_lines": N}``) is silently
violated.  A script that does ``read_file(p)["content"]`` raises ``KeyError``;
one that does ``.get("content", "")`` and writes the result back truncates the
file.

Everything here drives the REAL chain — ``model_tools.handle_function_call`` →
``execute_code`` → sandbox RPC → ``handle_function_call`` again → registry →
``tools.file_tools`` — against real files on disk.  The dispatcher and the file
layer are deliberately not mocked: the boundary this fix introduces lives
*between* them, so unit tests on either side cannot see a break in the middle.

The RPC transport is whichever one the host provides: an AF_UNIX socket on
POSIX, loopback TCP on native Windows (``_use_tcp_rpc``).  Both run the same
``_rpc_server_loop`` and the same dispatch site, so these tests exercise the
Windows TCP RPC route when they run on a Windows host.
"""

import json
import os
import tempfile
import unittest
import uuid

import pytest

os.environ["TERMINAL_ENV"] = "local"

from model_tools import handle_function_call  # noqa: E402
from tools.file_tools import _read_tracker, _read_tracker_lock  # noqa: E402


@pytest.fixture(autouse=True)
def _force_local_terminal(monkeypatch):
    """Keep the terminal backend local for every test in this module."""
    monkeypatch.setenv("TERMINAL_ENV", "local")


# The sandbox stub always sends this limit (``_TOOL_STUBS["read_file"]``), and
# the dedup key is ``(resolved_path, offset, limit)``.  Model-facing reads in
# these tests use the same value on purpose: a different limit lands on a
# different key and the surfaces would never meet.
SANDBOX_READ_LIMIT = 2000


def _model_read(path, task_id, limit=SANDBOX_READ_LIMIT, offset=1):
    """One model-facing ``read_file`` call, through the normal dispatcher."""
    return json.loads(handle_function_call(
        "read_file",
        {"path": path, "offset": offset, "limit": limit},
        task_id=task_id,
    ))


def _run_sandbox(code, task_id):
    """One ``execute_code`` call, dispatched the way the model dispatches it."""
    return json.loads(handle_function_call(
        "execute_code",
        {"code": code},
        task_id=task_id,
        enabled_tools=["read_file"],
    ))


def _sandbox_reads(result):
    """Parse the JSON line the sandbox scripts below print as their last line."""
    output = (result.get("output") or "").strip()
    assert output, f"sandbox produced no output: {result}"
    return json.loads(output.splitlines()[-1])


_READ_TWICE = """
import json
from hermes_tools import read_file

seen = []
for _ in range(2):
    r = read_file({path!r})
    seen.append({{"keys": sorted(r), "content": r.get("content")}})
print(json.dumps(seen))
"""


_POLL_A_CHANGING_FILE = """
import json
from hermes_tools import read_file

seen = []
for i in range(5):
    with open({path!r}, "a", encoding="utf-8") as fh:
        fh.write("tick %d\\n" % i)
    r = read_file({path!r})
    seen.append({{"keys": sorted(r), "content": r.get("content")}})
print(json.dumps(seen))
"""


class _SandboxReadCase(unittest.TestCase):
    """Shared fixture: a real file, a fresh task id, a clean read tracker."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="hermes_read_dedup_")
        self.path = os.path.join(self._dir, "ledger.md")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("alpha\nbravo\ncharlie\n")
        self.task_id = f"read-dedup-{uuid.uuid4().hex[:8]}"
        self.addCleanup(self._forget_task)

    def _forget_task(self):
        with _read_tracker_lock:
            _read_tracker.pop(self.task_id, None)

    def assert_carries_content(self, response, where):
        self.assertIn(
            "content", response["keys"],
            f"{where} returned no content: {response['keys']}",
        )
        self.assertIn("alpha", response["content"] or "", where)


class TestSandboxReadsAlwaysReturnContent(_SandboxReadCase):
    """The bug in #44843, both orders, through the real sandbox."""

    def test_repeated_sandbox_reads_after_a_model_read_both_return_content(self):
        """The regression test asked for in the 2026-07-14 review of #44847.

        Seed normal dedup for the task with a model-facing read, then call
        ``hermes_tools.read_file()`` twice through ``execute_code`` and assert
        both responses contain ``content``.
        """
        seed = _model_read(self.path, self.task_id)
        self.assertIn("content", seed, "the seeding model read should return content")

        result = _run_sandbox(_READ_TWICE.format(path=self.path), self.task_id)
        self.assertEqual(result.get("status"), "success", result)
        self.assertEqual(result.get("tool_calls_made"), 2)

        reads = _sandbox_reads(result)
        self.assertEqual(len(reads), 2)
        self.assert_carries_content(reads[0], "first sandbox read")
        self.assert_carries_content(reads[1], "second sandbox read")

    def test_two_sandbox_reads_in_one_script_both_return_content(self):
        """The reported incident needs no model read at all.

        Two ``read_file`` calls on the same path inside one script: before the
        fix the second returns a stub with no ``content``, which is what a
        read-modify-write helper writes back over the file.
        """
        result = _run_sandbox(_READ_TWICE.format(path=self.path), self.task_id)
        self.assertEqual(result.get("status"), "success", result)

        reads = _sandbox_reads(result)
        self.assertEqual(len(reads), 2)
        self.assert_carries_content(reads[0], "first sandbox read")
        self.assert_carries_content(reads[1], "second sandbox read")

    def test_sandbox_polling_a_changing_file_keeps_returning_content(self):
        """A script that re-reads a file it is appending to must keep seeing it.

        Every read here reaches disk (the file changes between reads, so dedup
        never applies) — what denied content before the fix was the
        consecutive-read guard, which counts the repeated *key* and blocks on
        the fourth hit while asserting "The content has NOT changed."
        """
        result = _run_sandbox(_POLL_A_CHANGING_FILE.format(path=self.path), self.task_id)
        self.assertEqual(result.get("status"), "success", result)
        self.assertEqual(result.get("tool_calls_made"), 5)

        reads = _sandbox_reads(result)
        self.assertEqual(len(reads), 5)
        for i, read in enumerate(reads):
            self.assert_carries_content(read, f"poll read {i + 1}")
        self.assertIn("tick 4", reads[-1]["content"])


class TestSandboxReadsLeaveTheModelSurfaceAlone(_SandboxReadCase):
    """The reverse direction: a program's reads must not degrade the model's."""

    def test_sandbox_reads_do_not_block_a_later_model_read(self):
        """Program reads must not spend the model's repeat-read budget.

        Before the fix the sandbox's two reads land on the model's own dedup
        key, so the model's next read of a file it read once is refused
        outright — told it has already read the region four times, citing
        results that were never in the conversation.
        """
        _model_read(self.path, self.task_id)
        _run_sandbox(_READ_TWICE.format(path=self.path), self.task_id)

        after = _model_read(self.path, self.task_id)

        self.assertNotIn(
            "already_read", after,
            f"a program's reads escalated the model's read guard: {after}",
        )
        self.assertIsNone(after.get("error"), after)

    def test_model_dedup_still_applies_to_the_model_after_a_sandbox_read(self):
        """The other half: model-facing dedup is preserved, not disabled.

        The model really did read this region, so the stub it gets back is
        correct — the program's intervening reads neither suppress it nor
        escalate it.
        """
        _model_read(self.path, self.task_id)
        _run_sandbox(_READ_TWICE.format(path=self.path), self.task_id)

        after = _model_read(self.path, self.task_id)

        self.assertTrue(after.get("dedup"), after)
        self.assertEqual(after.get("status"), "unchanged", after)
        self.assertFalse(after.get("content_returned"), after)

    def test_a_model_read_after_program_reads_still_returns_content(self):
        """Program first, model second — the order the two tests above miss.

        Nothing has ever read this file on the model surface, so the model's
        FIRST read of it must carry ``content``.  Before the fix the sandbox's
        reads land on the model's own dedup key, so the model is handed a
        ``status: "unchanged"`` stub for content it never received — the same
        empty-``content`` shape that truncated the file in #44843, one surface
        over.

        This is the half of the fix that lives in the ``dedup[dedup_key]``
        write guard in ``read_file_tool``.  Both other program/model tests
        begin with a *model* read, which seeds that key legitimately, so they
        stay green if the guard is dropped in a rebase.  This one does not.
        """
        result = _run_sandbox(_READ_TWICE.format(path=self.path), self.task_id)
        self.assertEqual(result.get("status"), "success", result)

        first_model_read = _model_read(self.path, self.task_id)

        self.assertIn(
            "content", first_model_read,
            "the model's first read of this file returned no content: "
            f"{first_model_read}",
        )
        self.assertIn("alpha", first_model_read.get("content") or "")
        self.assertIsNone(first_model_read.get("error"), first_model_read)

@pytest.mark.windows_only
class TestSandboxReadsOnWindowsTcpRpc(_SandboxReadCase):
    """Same contract over the native-Windows loopback-TCP RPC transport.

    #44843 was re-confirmed from Windows 11 on 2026-08-15, and that reporter
    asked for the TCP route to be covered as well as the POSIX one.  The socket
    family is the only difference — both transports run the same
    ``_rpc_server_loop`` and the same dispatch site — so this is the same
    assertion pinned to the host that selects ``_use_tcp_rpc``.  Skipped
    everywhere else; do not gate it on a patched ``sys.platform``.
    """

    def test_repeated_sandbox_reads_after_a_model_read_both_return_content(self):
        _model_read(self.path, self.task_id)

        result = _run_sandbox(_READ_TWICE.format(path=self.path), self.task_id)
        self.assertEqual(result.get("status"), "success", result)

        reads = _sandbox_reads(result)
        self.assertEqual(len(reads), 2)
        self.assert_carries_content(reads[0], "first sandbox read")
        self.assert_carries_content(reads[1], "second sandbox read")


if __name__ == "__main__":
    unittest.main()
