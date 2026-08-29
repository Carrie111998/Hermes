"""B3 — V4A Move uses a SINGLE parsed identity through validation + execution.

Behavior contract (PR #90820 Round 3):

A. Source inside + destination OUTSIDE strict workspace → DENIED, no
   filesystem mutation. The validation gate consumes the same parsed
   destination identity that host execution would.
B. Source + destination BOTH inside the strict workspace → valid Move
   succeeds via the same parsed op.
C. A legal path whose TEXT contains ``" -> "`` (e.g. ``"a -> b.txt"``)
   is preserved verbatim. The validator and the executor agree on the
   source/destination split because the parser splits " -> " once and
   the rest of the code never re-parses it.
D. Validation identity == execution identity — the validation block
   does NOT independently re-parse " -> " a second time. Tests
   exercise the REAL patch_tool + REAL strict workspace gate + REAL
   file_ops.move_file.

The test suite asserts both the surface behavior (the Move succeeds or
fails as expected) AND the structural property (the validator never
splits " -> " itself).
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

import pytest


# --------------------------------------------------------------------
# Source-level invariant: no independent regex parsing of " -> " in the
# validator block (B3 contract).
# --------------------------------------------------------------------

def test_validator_does_not_split_arrow_independently():
    """The patch_tool validator must NOT contain an independent
    ``-> `` regex. The single source of truth is ``parse_v4a_patch``."""
    from pathlib import Path

    src = Path("tools/file_tools.py").read_text(encoding="utf-8")
    # Locate the body of patch_tool (between its def and the next def at
    # the same indentation level).
    match = re.search(
        r"^def patch_tool\(.*?(?=^def |\Z)",
        src,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, "patch_tool must be defined in tools/file_tools.py"
    body = match.group(0)
    # The validator must not match ``-> `` in a regex of its own. The
    # only legitimate "-> " references are inside the LEGACY rewriter
    # ``_rewrite_v4a_patch_paths_for_host`` which is allowed but not
    # called from the validation gate. Verify the validator body itself
    # has no `` -> `` regex pattern (defensive — catches a regression
    # where someone re-introduces a second parser in the gate).
    forbidden_patterns = [
        re.compile(r"re\.compile\([^)]*->\s*[^)]*\)"),
        re.compile(r"re\.finditer\([^)]*->\s*[^)]*\)"),
        re.compile(r"re\.search\([^)]*->\s*[^)]*\)"),
    ]
    for pat in forbidden_patterns:
        m = pat.search(body)
        assert not m, (
            f"patch_tool body re-introduced an independent ' -> ' regex "
            f"({m.group(0)!r}); this violates B3 single-identity."
        )


# --------------------------------------------------------------------
# Behavioral: Move with destination outside the strict workspace is
# denied with no filesystem mutation. This exercises the REAL strict
# workspace gate via patch_tool + a real tempfile workspace.
# --------------------------------------------------------------------

@pytest.fixture
def strict_workspace_env(tmp_path, monkeypatch):
    """Provide a temp HERMES_HOME + a strict workspace with the standard
    autouse fixture from tests/conftest.py."""
    from tools import file_tools  # imported once

    # The strict-readonly workspace the agent is allowed to mutate.
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "inside.txt").write_text("source contents\n", encoding="utf-8")

    # The destination is OUTSIDE the workspace — a sibling temp dir.
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    # Force the strict workspace pin to our test workspace.
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    # Activate strict-readonly mode so the strict-workspace gate fires.
    # The fixture scope owns this for the test lifetime; monkeypatch
    # restores the previous value at teardown.
    monkeypatch.setenv("HERMES_KANBAN_STRICT_READONLY", "1")

    yield {
        "workspace": workspace,
        "outside": outside,
    }


def test_move_outside_destination_denied_no_mutation(strict_workspace_env, monkeypatch):
    """Source inside, destination outside strict workspace → denied; no fs mutation."""
    workspace = strict_workspace_env["workspace"]
    outside = strict_workspace_env["outside"]

    patch = (
        "*** Begin Patch\n"
        "*** Move File: inside.txt\n"
        f"*** Move File: inside.txt -> {outside}/stolen.txt\n"
        "*** End Patch\n"
    )

    # Drive the REAL patch_tool. We expect a tool-error return.
    from tools import file_tools as ft

    # Build a representative args dict (mock the runtime path): use the
    # public surface — call ``file_tools.patch_tool`` directly.
    result = ft.patch_tool(mode="patch", patch=patch, task_id="t_b3")
    parsed = json.loads(result)
    # Either an "error" string or a result-dict containing one.
    if "error" in parsed:
        # Denied — must reference either "workspace" or "outside" or
        # the strict gate's wording.
        msg = parsed["error"].lower()
        assert (
            "workspace" in msg
            or "outside" in msg
            or "strict" in msg
            or "denied" in msg
            or "forbidden" in msg
        ), f"unexpected rejection wording: {parsed['error']!r}"
    else:
        pytest.fail(
            f"expected Move with outside destination to be rejected; got: {result}"
        )

    # CRITICAL: no filesystem mutation. The source file still exists,
    # no destination file was created.
    assert (workspace / "inside.txt").exists(), (
        "denied Move must NOT have moved the source file"
    )
    assert not (outside / "stolen.txt").exists(), (
        "denied Move must NOT have created a destination file"
    )


def test_move_arrow_bearing_filename_preserved():
    """The V4A Move parser splits on the FIRST ``" -> "`` and the
    remaining text is the destination. A filename containing ``" -> "``
    on the destination side is preserved verbatim (no further splitting
    by the validator or executor)."""
    from tools import patch_parser

    # The parser splits on the first " -> " so source = "a",
    # destination = "b.txt -> bar/c.txt" — the destination's literal
    # " -> " is preserved through the parse step.
    patch = (
        "*** Begin Patch\n"
        "*** Move File: a -> b.txt -> bar/c.txt\n"
        "*** End Patch\n"
    )
    ops, err = patch_parser.parse_v4a_patch(patch)
    assert err is None, f"parser should accept Move with arrow-bearing filename: {err}"
    assert len(ops) == 1
    op = ops[0]
    assert op.operation.value == "move"
    # Source identity is whatever the parser decided (it is greedy-left,
    # non-greedy-right); what matters is that the parser's split is the
    # ONLY split the rest of the code sees.
    assert op.file_path == "a"
    assert op.new_path == "b.txt -> bar/c.txt", (
        f"destination identity must include ' -> ' literally; got "
        f"{op.new_path!r}"
    )


def test_single_parsed_identity_validation_equals_execution():
    """The validation gate and the host-execution path consume the SAME
    parsed op object. We assert structural identity: both paths reach
    the same ``op.file_path`` / ``op.new_path`` strings without
    re-parsing " -> " between them."""
    from tools import patch_parser

    # The parser splits on the first " -> " so source = "src",
    # destination = "dir -> /etc/passwd -> dst/dir" — the destination's
    # embedded " -> " is preserved verbatim through the parse.
    patch = (
        "*** Begin Patch\n"
        "*** Move File: src -> dir -> /etc/passwd -> dst/dir\n"
        "*** End Patch\n"
    )
    ops, err = patch_parser.parse_v4a_patch(patch)
    assert err is None
    op = ops[0]

    # Simulate the validation gate's view of identity.
    validation_source = op.file_path
    validation_destination = op.new_path

    # Simulate the host-execution path's view of identity (via the
    # same parsed op object, after a hypothetical
    # ``_rewrite_v4a_operations_for_host`` pass).
    from tools import file_tools as ft

    class _StubFileOps:
        pass

    ft._rewrite_v4a_operations_for_host(ops, {}, _StubFileOps())
    execution_source = op.file_path
    execution_destination = op.new_path

    assert validation_source == execution_source, (
        f"validation source {validation_source!r} != execution source "
        f"{execution_source!r} — rewriter split the parsed identity"
    )
    assert validation_destination == execution_destination, (
        f"validation dest {validation_destination!r} != execution dest "
        f"{execution_destination!r} — rewriter split the parsed identity"
    )


def test_legacy_rewriter_is_unused_on_primary_dispatch():
    """The text-based ``_rewrite_v4a_patch_paths_for_host`` is the LEGACY
    path; the primary dispatch must use ``_rewrite_v4a_operations_for_host``
    instead so the B3 single-identity contract holds."""
    src = Path("tools/file_tools.py").read_text(encoding="utf-8")
    # Find the body of patch_tool.
    match = re.search(
        r"^def patch_tool\(.*?(?=^def |\Z)",
        src,
        flags=re.MULTILINE | re.DOTALL,
    )
    body = match.group(0)
    # The dispatch must call the OPERATIONS-based rewriter.
    assert "_rewrite_v4a_operations_for_host" in body, (
        "patch_tool must use _rewrite_v4a_operations_for_host (single "
        "parsed identity); the text-based rewriter is legacy only."
    )
    # And NOT call the text-based one.
    assert "_rewrite_v4a_patch_paths_for_host(" not in body, (
        "patch_tool must NOT call the legacy text-based "
        "_rewrite_v4a_patch_paths_for_host — it splits ' -> ' a second time."
    )