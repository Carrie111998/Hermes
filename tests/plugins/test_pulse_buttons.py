"""Tests for the pulse_buttons plugin.

Covers the pure, deterministic pieces:
  - store round-trip / pop double-click guard / clear_unpublished
  - the merge gate's FAIL-CLOSED behavior (the teamwork-safety invariant)
  - squash_merge's re-gate + moved-head guard
  - block building (bucket → correct buttons; issues get none)
  - local_review command construction + clone-map resolution
  - unblock formatting

The async Slack wiring is thin and exercised elsewhere via injected fakes; here
we test the logic that decides whether anything irreversible happens.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.pulse_buttons import blocks, github, local_review, store
from plugins.pulse_buttons import actions, cli


# --- store -----------------------------------------------------------------

def _item(**over):
    base = {
        "repo": "Strike48-public/pick", "number": 302,
        "title": "docs(plg): taxonomy", "url": "https://github.com/x/pull/302",
        "head_sha": "abc123", "bucket": "awaiting",
    }
    base.update(over)
    return base


def test_store_roundtrip_and_pop(tmp_path: Path):
    p = tmp_path / "pulse.json"
    store.stage(_item(), path=p)
    got = store.get("Strike48-public/pick#302", path=p)
    assert got and got["bucket"] == "awaiting" and got["head_sha"] == "abc123"
    # pop returns once, then None (double-click guard).
    assert store.pop("Strike48-public/pick#302", path=p) is not None
    assert store.pop("Strike48-public/pick#302", path=p) is None


def test_stage_preserves_message_ts_on_restage(tmp_path: Path):
    p = tmp_path / "pulse.json"
    store.stage(_item(), path=p)
    store.mark_published("Strike48-public/pick#302", message_ts="1700.5", path=p)
    store.stage(_item(head_sha="def456"), path=p)  # re-stage at new head
    got = store.get("Strike48-public/pick#302", path=p)
    assert got["head_sha"] == "def456"
    assert got["message_ts"] == "1700.5"  # not lost


def test_clear_unpublished_keeps_posted(tmp_path: Path):
    p = tmp_path / "pulse.json"
    store.stage(_item(number=1), path=p)
    store.stage(_item(number=2), path=p)
    store.mark_published("Strike48-public/pick#2", message_ts="1.0", path=p)
    store.clear_unpublished(path=p)
    data = store.load(p)
    assert "Strike48-public/pick#2" in data       # posted survives
    assert "Strike48-public/pick#1" not in data   # stale unpublished dropped


# --- merge gate: FAIL CLOSED (the teamwork-safety invariant) ---------------

def _fake_run(payload: dict, rc: int = 0, threads=None):
    """Fake subprocess.run: JSON for `gh pr view`, a GraphQL reviewThreads
    response for `gh api graphql`, and success for `gh pr merge`.

    ``threads`` is a list of resolved flags, e.g. [True, False]; default all
    resolved (empty). Set ``threads="error"`` to simulate a GraphQL failure.
    """
    import json

    def run(args, capture_output=True, text=True):
        if "merge" in args:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "graphql" in args:
            if threads == "error":
                return SimpleNamespace(returncode=1, stdout="", stderr="graphql boom")
            nodes = [{"isResolved": r, "path": "a.py",
                      "comments": {"nodes": [{"author": {"login": "cc"}, "body": "fix"}]}}
                     for r in (threads or [])]
            body = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
        # gh pr view --json ...
        return SimpleNamespace(returncode=rc, stdout=json.dumps(payload), stderr="")
    return run


def _green_pr(**over):
    base = {
        "state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED",
        "headRefOid": "abc123",
        "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS", "status": "COMPLETED"}],
    }
    base.update(over)
    return base


def test_gate_passes_when_all_green():
    g = github.check_merge_gate("r/x", 1, run=_fake_run(_green_pr()))
    assert g.ok is True
    assert g.head_sha == "abc123"


def test_gate_blocks_on_pending_ci():
    """CI still running must NOT be green — fail closed."""
    pr = _green_pr(statusCheckRollup=[{"name": "ci", "status": "IN_PROGRESS"}])
    g = github.check_merge_gate("r/x", 1, run=_fake_run(pr))
    assert g.ok is False
    assert any("not green yet" in r or "still running" in r for r in g.reasons)


def test_gate_blocks_on_failing_ci():
    pr = _green_pr(statusCheckRollup=[{"name": "build", "conclusion": "FAILURE", "status": "COMPLETED"}])
    g = github.check_merge_gate("r/x", 1, run=_fake_run(pr))
    assert g.ok is False
    assert any("CI failing" in r for r in g.reasons)


def test_gate_blocks_on_no_checks():
    """No CI reported → refuse to merge blind (teamwork safety)."""
    pr = _green_pr(statusCheckRollup=[])
    g = github.check_merge_gate("r/x", 1, run=_fake_run(pr))
    assert g.ok is False
    assert any("no CI checks" in r for r in g.reasons)


def test_gate_blocks_on_unresolved_threads():
    g = github.check_merge_gate("r/x", 1, run=_fake_run(_green_pr(), threads=[False, True]))
    assert g.ok is False
    assert any("unresolved review comment" in r for r in g.reasons)


def test_gate_fails_closed_when_threads_unfetchable():
    """A GraphQL error must not read as 'all resolved' — fail closed."""
    g = github.check_merge_gate("r/x", 1, run=_fake_run(_green_pr(), threads="error"))
    assert g.ok is False
    assert any("could not verify review threads" in r for r in g.reasons)


def test_gate_blocks_on_merge_state_blocked():
    """mergeStateStatus=BLOCKED (branch protection) blocks even if fields look ok."""
    pr = _green_pr(mergeStateStatus="BLOCKED", reviewDecision="CHANGES_REQUESTED")
    g = github.check_merge_gate("r/x", 1, run=_fake_run(pr))
    assert g.ok is False
    assert any("blocked" in r.lower() or "changes requested" in r.lower() for r in g.reasons)


def test_gate_blocks_on_conflict():
    pr = _green_pr(mergeable="CONFLICTING")
    g = github.check_merge_gate("r/x", 1, run=_fake_run(pr))
    assert g.ok is False
    assert any("conflict" in r for r in g.reasons)


def test_gate_fails_closed_on_fetch_error():
    """A gh error (rc!=0 / unparsable) must never read as mergeable."""
    def run(args, capture_output=True, text=True):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")
    g = github.check_merge_gate("r/x", 1, run=run)
    assert g.ok is False


def test_squash_merge_reruns_gate_and_detects_moved_head():
    # Gate is green at abc123, but the operator confirmed at an older head.
    g_run = _fake_run(_green_pr(headRefOid="abc123"))
    res = github.squash_merge("r/x", 1, expected_head="OLD9999", run=g_run)
    assert res.ok is False
    assert "moved" in res.detail


def test_squash_merge_proceeds_when_green_and_head_matches():
    res = github.squash_merge("r/x", 1, expected_head="abc123", run=_fake_run(_green_pr()))
    assert res.ok is True
    assert "squash" in res.detail


def test_squash_merge_refuses_when_gate_fails():
    res = github.squash_merge("r/x", 1, run=_fake_run(_green_pr(mergeable="CONFLICTING")))
    assert res.ok is False
    assert "gate re-check failed" in res.detail


# --- blocks ----------------------------------------------------------------

def _staged(bucket, kind="pr", number=1):
    return {"repo": "Strike48-public/pick", "number": number, "title": "t",
            "url": "u", "detail": "d", "kind": kind, "_key": f"Strike48-public/pick#{number}"}


def _action_ids(row):
    return [e["action_id"] for e in row["elements"]] if row else []


def test_ready_bucket_gets_merge_button_only():
    row = blocks._action_row("ready", _staged("ready"))
    assert _action_ids(row) == [blocks.ACTION_MERGE_CHECK]


def test_awaiting_bucket_gets_review_and_merge():
    row = blocks._action_row("awaiting", _staged("awaiting"))
    assert _action_ids(row) == [blocks.ACTION_REVIEW, blocks.ACTION_MERGE_CHECK]


def test_blocked_bucket_gets_review_and_unblock():
    row = blocks._action_row("blocked", _staged("blocked"))
    assert _action_ids(row) == [blocks.ACTION_REVIEW, blocks.ACTION_UNBLOCK]


def test_new_prs_gets_review_only():
    row = blocks._action_row("new_prs", _staged("new_prs"))
    assert _action_ids(row) == [blocks.ACTION_REVIEW]


def test_rotting_and_issues_get_no_buttons():
    assert blocks._action_row("rotting", _staged("rotting")) is None
    assert blocks._action_row("blocked", _staged("blocked", kind="issue")) is None


def test_digest_stays_within_block_ceiling():
    repos = [{"repo": "Strike48-public/pick",
              "buckets": {"ready": [_staged("ready", number=i) for i in range(40)]}}]
    bk = blocks.build_digest_blocks(repos)
    assert len(bk) <= blocks.MAX_BLOCKS


def _many_repos(n_repos: int, per_bucket: int, buckets=("ready",), detail="d"):
    return [
        {"repo": f"Strike48-public/repo{r}",
         "buckets": {b: [dict(_staged(b, number=i), detail=detail,
                              _key=f"repo{r}-{b}-{i}") for i in range(per_bucket)]
                     for b in buckets}}
        for r in range(n_repos)
    ]


def test_ceiling_holds_across_many_repos():
    """Regression: per-repo divider/header and the '…and N more' tails are
    appended outside the per-item guard, so many repos overflowed to 53 blocks
    and Slack rejected the whole digest with invalid_blocks."""
    bk = blocks.build_digest_blocks(_many_repos(12, 8))
    assert len(bk) <= blocks.MAX_BLOCKS


def test_ceiling_holds_with_every_bucket_populated():
    bk = blocks.build_digest_blocks(
        _many_repos(40, 9, buckets=("ready", "awaiting", "blocked", "new_prs", "rotting"))
    )
    assert len(bk) <= blocks.MAX_BLOCKS


def test_truncation_is_announced_when_items_are_dropped():
    bk = blocks.build_digest_blocks(_many_repos(12, 8))
    assert any("truncated" in str(b) for b in bk)


def test_no_title_line_is_stranded_without_its_action_row():
    """A section that would land on the last slot must not push its buttons out."""
    bk = blocks.build_digest_blocks(_many_repos(12, 8))
    types = [b["type"] for b in bk]
    # 'ready' items always carry a button row, and each repo contributes exactly
    # one buttonless section (its name), so item sections must all be paired.
    item_sections = types.count("section") - types.count("divider")
    assert types.count("actions") == item_sections


def test_long_detail_is_clamped_to_slack_text_cap():
    """Regression: MAX_SECTION_TEXT was declared but never enforced — a long
    'detail' produced a 4028-char section and Slack rejected the digest."""
    item = dict(_staged("ready"), detail="x" * 4000)
    bk = blocks.build_digest_blocks(
        [{"repo": "Strike48-public/pick", "buckets": {"ready": [item]}}]
    )
    for b in bk:
        if b["type"] == "section":
            assert len(b["text"]["text"]) <= blocks.MAX_SECTION_TEXT


def test_long_text_is_clamped_in_confirm_and_resolved_blocks():
    item = dict(_staged("ready"), detail="y" * 5000)
    for bk in (
        blocks.build_confirm_merge_blocks(item, ["CI green"]),
        blocks.build_resolved_blocks(item, text="z" * 5000, context="c" * 5000),
    ):
        for b in bk:
            if b["type"] == "section":
                assert len(b["text"]["text"]) <= blocks.MAX_SECTION_TEXT
            if b["type"] == "context":
                for e in b["elements"]:
                    assert len(e["text"]) <= blocks.MAX_SECTION_TEXT


def test_confirm_merge_blocks_have_confirm_button():
    bk = blocks.build_confirm_merge_blocks(_staged("ready"), ["CI green", "mergeable"])
    ids = [e["action_id"] for b in bk if b.get("type") == "actions" for e in b["elements"]]
    assert blocks.ACTION_MERGE_CONFIRM in ids


# --- local_review ----------------------------------------------------------

def test_build_review_command_uses_slash_and_url():
    cmd = local_review.build_review_command("https://github.com/x/pull/9", Path("/tmp"))
    assert cmd[0] == "claude" and "-p" in cmd
    assert any("/review-pr https://github.com/x/pull/9" == a for a in cmd)


def test_build_review_command_passes_fallback_model():
    """A capacity blip on the primary must not lose the whole 25-min review.

    ``--fallback-model`` is the mechanism that works for a headless ``-p`` run
    (the flag's own help notes it applies to ``--print``). Verified end-to-end
    against the real settings.json: with the primary forced to fail, the run
    emitted "Opus 5 not available — using Opus 4.8 for this session" and billed
    ``us.anthropic.claude-opus-4-8``, exit 0.
    """
    cmd = local_review.build_review_command("https://github.com/x/pull/9", Path("/tmp"))
    assert "--fallback-model" in cmd
    fallback = cmd[cmd.index("--fallback-model") + 1]
    assert fallback == local_review._REVIEW_FALLBACK_MODEL
    # The fallback must be a DIFFERENT model than the primary, or it buys nothing.
    assert fallback != cmd[cmd.index("--model") + 1]


def test_review_models_are_bedrock_inference_profile_ids():
    """Bare Anthropic slugs silently resolve to the WRONG model on Bedrock.

    Verified: `--model claude-sonnet-5` under CLAUDE_CODE_USE_BEDROCK=1 billed
    `us.anthropic.claude-opus-5` at a 200k window — not Sonnet, and not 1M. Only
    full `us.anthropic.*` inference-profile ids resolve as written, and only the
    `[1m]` suffix yields the 1M context this box standardizes on.
    """
    for value in (local_review._REVIEW_MODEL, local_review._REVIEW_FALLBACK_MODEL):
        assert value.startswith("us.anthropic."), f"{value} is not a profile id"
        assert value.endswith("[1m]"), f"{value} would run at 200k, not 1M"


def test_clone_dir_returns_none_for_unknown_repo():
    assert local_review.clone_dir("nobody/nothing") is None


# --- unblock formatting ----------------------------------------------------

def test_format_unblock_lists_failing_ci_and_threads():
    ctx = {
        "failing_checks": [{"name": "build", "conclusion": "failure", "url": "http://ci"}],
        "unresolved_threads": [{"path": "a.py", "author": "cc", "body": "fix this"}],
    }
    msg = actions._format_unblock("r/x", 1, ctx)
    assert "build" in msg and "fix this" in msg and "a.py" in msg


def test_format_unblock_handles_error():
    msg = actions._format_unblock("r/x", 1, {"error": "gh down"})
    assert "gh down" in msg


# --- authorization (fail closed) -------------------------------------------

def test_is_authorized_denies_when_unset(monkeypatch):
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    assert actions.is_authorized("U1") is False


def test_is_authorized_allows_listed(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U1,U2")
    assert actions.is_authorized("U1") is True
    assert actions.is_authorized("U9") is False


def test_is_authorized_wildcard(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "*")
    assert actions.is_authorized("anyone") is True


# --- publish fails loud, never silent --------------------------------------

def test_plain_text_fallback_lists_the_waiting_items():
    staged = [{"repo": "Strike48-public/pick", "buckets": {
        "ready": [{"repo": "Strike48-public/pick", "number": 302,
                   "url": "https://gh/pull/302", "title": "docs: taxonomy", "_key": "k1"}]}}]
    out = cli._plain_text_digest(staged, Exception("invalid_blocks"))
    assert "invalid_blocks" in out and "302" in out and "docs: taxonomy" in out
    assert len(out) <= 3900


def test_publish_falls_back_to_plain_text_when_blocks_rejected(monkeypatch, tmp_path):
    """Regression: an invalid_blocks rejection left the digest undelivered, so a
    broken Pulse looked exactly like a quiet day (it sat dark for a full day)."""
    posts = []

    async def fake_post(client, channel, text, blocks_payload, thread_ts=None):
        # Mirror Slack: reject the Block Kit call, accept plain text.
        if blocks_payload:
            raise RuntimeError("chat.postMessage: invalid_blocks")
        posts.append(text)
        return "1784991363.081599"

    scanner = SimpleNamespace(scan_all=lambda advance_watermark=True: {
        "repos": [{"repo": "Strike48-public/pick", "buckets": {
            "ready": [{"repo": "Strike48-public/pick", "number": 302, "url": "u",
                       "title": "t", "head_sha": "abc", "kind": "pr"}]}}],
        "warnings": [],
    })

    monkeypatch.setattr(cli, "_load_pulse_scanner", lambda: scanner)
    monkeypatch.setattr(cli.slackio, "post_message", fake_post)
    monkeypatch.setattr(cli.slackio, "make_client", lambda: object())
    monkeypatch.setattr(cli.slackio, "home_channel", lambda: "C123")
    monkeypatch.setattr(store, "default_path", lambda: tmp_path / "pulse.json")

    rc = cli.pulse_command(SimpleNamespace(pulse_action="publish", channel="", dry_run=False))

    # Delivered something despite the rejection, and still reported failure.
    assert posts, "no fallback message was delivered — Pulse went silent"
    assert "302" in posts[0]
    assert rc == 1, "must exit non-zero so the cron records the real error"
