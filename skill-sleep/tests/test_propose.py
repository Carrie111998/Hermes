import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.proposal import Proposal
import pipeline.propose as propose


# ── helpers ───────────────────────────────────────────────────────────────


def _write_tasks(tmp_path, tasks=None):
    data = {
        "generated_at": "2026-08-20T00:00:00+00:00",
        "total_sessions_scanned": 7,
        "total_cards": len(tasks or []),
        "tasks": tasks or [],
    }
    p = tmp_path / "tasks.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _sample_task(skill="Hermes", req="deploy please", evidence=None):
    return {
        "skill_name": skill,
        "session_id": "sess_1",
        "user_request": req,
        "friction_evidence": evidence or ["tool_error: terminal — failed"],
        "tool_calls": 3,
        "timestamp": 123.0,
    }


# ── Proposal dataclass ────────────────────────────────────────────────────


def test_proposal_to_dict_truncates():
    p = Proposal("2026-08-20T00:00:00+00:00", "/tmp/SKILL.md", 1, 5, "x" * 5000, ["a"] * 20)
    d = p.to_dict()
    assert len(d["summary"]) <= 2000
    assert len(d["focused_on"]) <= 10


def test_proposal_repr():
    p = Proposal("2026-08-20T00:00:00+00:00", "/s/SKILL.md", 2, 10, "summary", ["f1"])
    assert "/s/SKILL.md" in repr(p)
    assert "2" in repr(p)


def test_proposal_from_dict_roundtrip():
    p = Proposal("2026-08-20T00:00:00+00:00", "/a/SKILL.md", 3, 12, "hello", ["x", "y"])
    d = p.to_dict()
    p2 = Proposal.from_dict(d)
    assert p2.skill_path == p.skill_path
    assert p2.source_task_cards == 3
    assert p2.diff_lines == 12


def test_proposal_to_json_valid():
    p = Proposal(Proposal.now_iso(), "/a/SKILL.md", 1, 2, "s", ["f"])
    j = p.to_json()
    assert json.loads(j)["skill_path"] == "/a/SKILL.md"


def test_proposal_now_iso():
    iso = Proposal.now_iso()
    assert "T" in iso


# ── Secret guard ──────────────────────────────────────────────────────────


def test_contains_secret_sk():
    hit, _ = propose.contains_secret("token sk-abcdefghij1234567890extra")
    assert hit is True


def test_contains_secret_ghp():
    hit, _ = propose.contains_secret("token ghp_1234567890abcdef1234567890abcdef1234")
    assert hit is True


def test_contains_secret_private_key():
    hit, _ = propose.contains_secret("-----BEGIN PRIVATE KEY----- abc")
    assert hit is True


def test_contains_secret_clean():
    hit, _ = propose.contains_secret("normal diff + add pitfalls line")
    assert hit is False


# ── Diff helpers ──────────────────────────────────────────────────────────


def test_count_added_lines():
    diff = "--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1,2 +1,3 @@\n line\n+added\n line\n"
    assert propose.count_added_lines(diff) == 1


def test_count_added_lines_ignores_header():
    diff = "--- a/SKILL.md\n+++ b/SKILL.md\n+real\n"
    assert propose.count_added_lines(diff) == 1


def test_is_valid_diff_true():
    assert propose.is_valid_diff("--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1 +1 @@\n-foo\n+bar\n")


def test_is_valid_diff_false_empty():
    assert propose.is_valid_diff("") is False
    assert propose.is_valid_diff("no diff here") is False


def test_is_valid_diff_with_hunk_only():
    assert propose.is_valid_diff("@@ -1,2 +1,3 @@\n+added\n") is True


def test_extract_diff_fenced():
    raw = (
        "summary: hello world\n"
        "focused_on: pitfall: check path\n"
        "```diff\n"
        "--- a/SKILL.md\n"
        "+++ b/SKILL.md\n"
        "@@ -1,2 +1,3 @@\n"
        "+added line\n"
        "```\n"
    )
    diff, summary, focused = propose.extract_diff_and_meta(raw)
    assert "--- a/SKILL.md" in diff
    assert summary == "hello world"
    assert focused == ["pitfall: check path"]


def test_extract_diff_raw_header():
    raw = "--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1 +1 @@\n-foo\n+bar\n"
    diff, _, _ = propose.extract_diff_and_meta(raw)
    assert "---" in diff


def test_extract_diff_no_summary_fallback():
    raw = "```diff\n--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1 +1 @@\n+hi\n```\n"
    diff, summary, focused = propose.extract_diff_and_meta(raw)
    assert diff
    assert isinstance(focused, list)


# ── Tasks loading ─────────────────────────────────────────────────────────


def test_load_tasks_missing_exits(tmp_path):
    with pytest.raises(SystemExit):
        propose.load_tasks(str(tmp_path / "nope.json"))


def test_load_tasks_invalid_json(tmp_path):
    p = tmp_path / "tasks.json"
    p.write_text("not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        propose.load_tasks(str(p))


def test_build_tasks_summary_empty():
    out = propose.build_tasks_summary({"tasks": []})
    assert "no task cards" in out


def test_build_tasks_summary_with_tasks():
    data = {"tasks": [_sample_task()]}
    out = propose.build_tasks_summary(data)
    assert "Hermes" in out
    assert "deploy please" in out


# ── Skill loading ──────────────────────────────────────────────────────────


def test_load_skill_content_missing(tmp_path):
    with pytest.raises(SystemExit):
        propose.load_skill_content(str(tmp_path / "SKILL.md"))


def test_load_skill_content_ok(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("# hello", encoding="utf-8")
    assert propose.load_skill_content(str(p)) == "# hello"


# ── Rejected buffer ───────────────────────────────────────────────────────


def test_load_rejected_context_no_dir():
    # no rejected dir anywhere — should return placeholder
    out = propose.load_rejected_context("/tmp/__no_such_rejected_xyz__")
    assert "none" in out.lower()


def test_load_rejected_context_with_files(tmp_path):
    rej = tmp_path / "rejected"
    rej.mkdir()
    (rej / "a.jsonl").write_text(
        json.dumps({"diff": "--- a/SKILL.md\n+++ b/SKILL.md\n+hi\n", "reason": "gate failed"}) + "\n",
        encoding="utf-8",
    )
    (rej / "b.diff").write_text("--- a/SKILL.md\n+++ b/SKILL.md\n+hi\n", encoding="utf-8")
    out = propose.load_rejected_context(str(rej))
    assert "rejected" in out.lower()
    assert "hi" in out


def test_load_rejected_context_empty_dir(tmp_path):
    rej = tmp_path / "empty_rej"
    rej.mkdir()
    out = propose.load_rejected_context(str(rej))
    assert "empty" in out.lower()


# ── Prompt rendering ──────────────────────────────────────────────────────


def test_render_prompt_replaces_placeholders(tmp_path):
    tmpl = tmp_path / "tmpl.md"
    tmpl.write_text("skill:{skill_content} tasks:{tasks_summary} rej:{rejected_context}", encoding="utf-8")
    out = propose.render_prompt(str(tmpl), "SKILL", "TASKS", "REJ")
    assert "SKILL" in out
    assert "TASKS" in out
    assert "REJ" in out


def test_render_prompt_missing_exits(tmp_path):
    with pytest.raises(SystemExit):
        propose.render_prompt(str(tmp_path / "nope.md"), "a", "b", "c")


# ── call_omp (mocked) ─────────────────────────────────────────────────────


def test_call_omp_uses_list_args(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello", encoding="utf-8")
    mock = MagicMock(returncode=0, stdout="output ok", stderr="")
    with patch("pipeline.propose.subprocess.run", return_value=mock) as sp:
        out = propose.call_omp(prompt, str(tmp_path), propose.DEFAULT_MODEL, 10)
        assert out.strip() == "output ok"
        args = sp.call_args[0][0]
        assert isinstance(args, list)
        assert args[0] == "omp"
        assert "-p" in args
        assert "--cwd" in args
        assert "--model" in args


def test_call_omp_not_found(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello", encoding="utf-8")
    with patch("pipeline.propose.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(SystemExit):
            propose.call_omp(prompt, str(tmp_path), propose.DEFAULT_MODEL, 10)


def test_call_omp_timeout(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello", encoding="utf-8")
    with patch("pipeline.propose.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="omp", timeout=1)):
        with pytest.raises(SystemExit):
            propose.call_omp(prompt, str(tmp_path), propose.DEFAULT_MODEL, 10)


def test_call_omp_empty_output_exits(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello", encoding="utf-8")
    mock = MagicMock(returncode=0, stdout="   ", stderr="")
    with patch("pipeline.propose.subprocess.run", return_value=mock):
        with pytest.raises(SystemExit):
            propose.call_omp(prompt, str(tmp_path), propose.DEFAULT_MODEL, 10)


# ── write_candidate_diff ──────────────────────────────────────────────────


def test_write_candidate_diff_secret_rejected(tmp_path):
    secret_diff = "--- a/SKILL.md\n+++ b/SKILL.md\n+sk-abcdefghij1234567890extra\n"
    with pytest.raises(SystemExit):
        propose.write_candidate_diff(secret_diff, str(tmp_path))


def test_write_candidate_diff_ok(tmp_path):
    diff = "--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1 +1 @@\n+added\n"
    out = propose.write_candidate_diff(diff, str(tmp_path))
    assert Path(out).exists()
    assert Path(out).read_text(encoding="utf-8") == diff


# ── CLI integration (dry-run) ─────────────────────────────────────────────


def test_cli_dry_run_generates_outputs(tmp_path):
    tasks_path = _write_tasks(tmp_path, [_sample_task()])
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Skill\n## Pitfalls\n- foo\n", encoding="utf-8")
    outdir = tmp_path / "out"
    cmd = [
        sys.executable,
        str(ROOT / "pipeline" / "propose.py"),
        "--tasks",
        tasks_path,
        "--skill",
        str(skill),
        "--output-dir",
        str(outdir),
        "--dry-run",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    assert "[propose]" in proc.stdout
    assert (outdir / "candidate.diff").exists()
    assert (outdir / "proposal.json").exists()
    data = json.loads((outdir / "proposal.json").read_text(encoding="utf-8"))
    assert data["source_task_cards"] == 1
    assert data["diff_lines"] >= 1
    # diff must be valid unified diff
    diff_text = (outdir / "candidate.diff").read_text(encoding="utf-8")
    assert "---" in diff_text
    assert "+++" in diff_text or "@@" in diff_text
    # secret guard: no secret in diff
    hit, _ = propose.contains_secret(diff_text)
    assert hit is False


def test_cli_missing_tasks_exits(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Skill\n", encoding="utf-8")
    cmd = [
        sys.executable,
        str(ROOT / "pipeline" / "propose.py"),
        "--tasks",
        str(tmp_path / "nope.json"),
        "--skill",
        str(skill),
        "--output-dir",
        str(tmp_path / "out2"),
        "--dry-run",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    assert proc.returncode != 0
    assert "ERROR" in proc.stderr


def test_cli_ninerouter_check(tmp_path):
    tasks_path = _write_tasks(tmp_path, [_sample_task()])
    skill = tmp_path / "SKILL2.md"
    skill.write_text("# Skill\n", encoding="utf-8")
    cmd = [
        sys.executable,
        str(ROOT / "pipeline" / "propose.py"),
        "--tasks",
        tasks_path,
        "--skill",
        str(skill),
        "--output-dir",
        str(tmp_path / "out3"),
    ]
    env = {k: v for k, v in os.environ.items() if k != "NINEROUTER_KEY"}
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    assert proc.returncode != 0
    assert "NINEROUTER_KEY" in proc.stderr


def test_cli_output_dir_writes_valid_json(tmp_path):
    tasks_path = _write_tasks(tmp_path, [_sample_task(), _sample_task(skill="other")])
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Skill\n", encoding="utf-8")
    outdir = tmp_path / "out_json"
    cmd = [
        sys.executable,
        str(ROOT / "pipeline" / "propose.py"),
        "--tasks",
        tasks_path,
        "--skill",
        str(skill),
        "--output-dir",
        str(outdir),
        "--dry-run",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0
    data = json.loads((outdir / "proposal.json").read_text(encoding="utf-8"))
    assert data["source_task_cards"] == 2
    assert "generated_at" in data
    assert "skill_path" in data


def test_help_exits_zero():
    cmd = [sys.executable, str(ROOT / "pipeline" / "propose.py"), "--help"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0
    assert "--tasks" in proc.stdout
