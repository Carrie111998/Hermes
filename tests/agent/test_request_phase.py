"""Regression coverage for request-phase and dirty-repository controls."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent.prompt_builder import REQUEST_PHASE_GUIDANCE, build_skills_system_prompt
from agent.request_phase import (
    MAX_SKILL_PAYLOAD_CHARS_PER_RESULT,
    MAX_SKILL_PAYLOAD_CHARS_PER_TURN,
    RequestPhase,
    activate_turn_policy,
    classify_request_phase,
    clear_turn_policy,
    current_turn_policy,
    guard_tool_call,
    push_turn_policy,
    record_tool_effect_result,
    reset_turn_policy,
)


QUOTE_ANALYSIS_REQUEST = "analyze existing quote skills/process and work downward."


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _activate_quote_skill_policy(cwd):
    return activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=cwd)


@pytest.fixture
def clean_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes-test@example.invalid")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


@pytest.fixture(autouse=True)
def _clear_policy_after_test():
    clear_turn_policy()
    yield
    clear_turn_policy()


def test_exact_quote_prompt_is_investigation():
    assert (
        classify_request_phase(QUOTE_ANALYSIS_REQUEST)
        is RequestPhase.INVESTIGATION
    )


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("Review the scheduling flow and describe what is slow.", RequestPhase.INVESTIGATION),
        ("Implement the scheduling fix in the repository.", RequestPhase.IMPLEMENTATION),
        ("Please update app/actions/quotes/saveQuote.ts.", RequestPhase.IMPLEMENTATION),
        (
            "Approved. Now figure out how to make this happen.",
            RequestPhase.INVESTIGATION,
        ),
        ("Move 925 Washington Street to Tuesday.", RequestPhase.OPERATION),
        ("Update the client's phone number.", RequestPhase.OPERATION),
        ("Push 925 Washington Street to Tuesday.", RequestPhase.OPERATION),
        ("Release the invoice hold.", RequestPhase.OPERATION),
    ],
)
def test_phase_classifier_preserves_business_operations(instruction, expected):
    assert classify_request_phase(instruction) is expected


@pytest.mark.parametrize(
    "instruction",
    [
        "Analyze whether we should deploy this.",
        "Tell me if we should push this release.",
        "Should we commit this change?",
    ],
)
def test_analysis_questions_do_not_manufacture_implementation_authority(
    instruction,
):
    assert classify_request_phase(instruction) is RequestPhase.INVESTIGATION


@pytest.mark.parametrize(
    "instruction",
    [
        "Add a test.",
        "Remove that guardrail.",
        "Simplify this function.",
        "Patch the failing source file.",
        "Fix the runtime bug.",
        "Deploy the application build.",
        "Git push the branch.",
        "Make the prompt builder selective.",
        "Improve Hermes so it stops loading overlapping skills.",
        "Clean up this module.",
        "Optimize the runtime.",
        "Add regression coverage for this failure.",
    ],
)
def test_explicit_technical_requests_keep_native_power(instruction):
    assert classify_request_phase(instruction) is RequestPhase.IMPLEMENTATION


@pytest.mark.parametrize(
    "instruction",
    [
        "Make scheduling faster.",
        "Improve the client experience.",
        "Clean up the invoice list.",
        "Optimize the payroll schedule.",
        "Add coverage to the client's service.",
        "Improve source attribution in the quote analysis.",
        "Improve the landscape build estimate.",
    ],
)
def test_natural_change_language_preserves_business_operations(instruction):
    assert classify_request_phase(instruction) is RequestPhase.OPERATION


@pytest.mark.parametrize(
    "instruction",
    [
        "Fix it.",
        "Make it work.",
        "Go ahead.",
        "Do it.",
        "Push the Washington Street job to Tuesday.",
        "Release the invoice hold.",
    ],
)
def test_ambiguous_or_business_continuations_do_not_grant_source_write_authority(
    instruction,
):
    assert classify_request_phase(instruction) is RequestPhase.OPERATION


@pytest.mark.parametrize(
    "instruction",
    [
        "Fix it.",
        "Do it.",
        "Approved. Now figure out how to make this happen.",
        "Ship it live.",
    ],
)
def test_owner_execution_continuation_inherits_recent_technical_subject(
    instruction,
):
    prior_context = [
        {
            "role": "assistant",
            "content": (
                "The repository repair is scoped. I will update the gateway "
                "runtime and add regression tests in a clean worktree."
            ),
        }
    ]

    assert (
        classify_request_phase(
            instruction,
            prior_context=prior_context,
        )
        is RequestPhase.IMPLEMENTATION
    )


@pytest.mark.parametrize(
    "instruction",
    [
        "Do it.",
        "Ship it live.",
        "Release it.",
    ],
)
def test_business_continuation_does_not_inherit_source_write_authority(
    instruction,
):
    prior_context = [
        {
            "role": "assistant",
            "content": (
                "The Washington Street job can move to Tuesday while the "
                "invoice remains on hold."
            ),
        }
    ]

    assert (
        classify_request_phase(
            instruction,
            prior_context=prior_context,
        )
        is RequestPhase.OPERATION
    )


def test_business_skill_or_automation_language_does_not_grant_source_authority():
    prior_context = [
        {
            "role": "assistant",
            "content": (
                "The scheduling skill and automation can move Washington "
                "Street directly through the API gateway."
            ),
        }
    ]

    assert (
        classify_request_phase(
            "Do it.",
            prior_context=prior_context,
        )
        is RequestPhase.OPERATION
    )


def test_exact_quote_investigation_cannot_patch_repo(clean_repo):
    policy = activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    block = guard_tool_call(
        "patch",
        {
            "mode": "replace",
            "path": str(clean_repo / "app.py"),
            "old_string": "VALUE = 1",
            "new_string": "VALUE = 2",
        },
    )

    assert policy.phase is RequestPhase.INVESTIGATION
    assert block is not None
    assert "investigation" in block
    assert "source changes are not" in block


def test_exact_quote_prompt_allows_direct_skills_without_arbitrary_count_cap(
    clean_repo,
):
    import tools.skills_tool  # noqa: F401 - registers the real skill tools

    policy = _activate_quote_skill_policy(clean_repo)

    assert guard_tool_call(
        "skill_view",
        {"name": "terrain-quote-workflows"},
    ) is None
    assert (
        guard_tool_call(
            "skill_view", {"name": "maione-canonical-operator"}
        )
        is None
    )
    assert (
        guard_tool_call(
            "skill_view",
            {"name": "terrain-communications"},
        )
        is None
    )
    fourth_root = guard_tool_call(
        "skill_view",
        {"name": "terrain-scheduling"},
    )

    assert fourth_root is None
    nested_fanout = guard_tool_call(
        "skill_view",
        {
            "name": "terrain-payroll",
            "file_path": "references/routes.md",
        },
    )
    assert nested_fanout is not None
    assert "load the root skill" in nested_fanout
    assert policy.loaded_root_skills == [
        "terrain-quote-workflows",
        "maione-canonical-operator",
        "terrain-communications",
        "terrain-scheduling",
    ]


def test_real_registry_loads_complete_router_then_domain_skill(
    monkeypatch,
    clean_repo,
    tmp_path,
):
    from tools import skills_tool

    skills_root = tmp_path / "real-skills"
    router_name = "maione-canonical-operator"
    router_description = "Canonical front door for Maione operations."
    router_prefix = (
        "---\n"
        f"name: {router_name}\n"
        f"description: {router_description}\n"
        "---\n\n"
        f"# {router_name}\n\n"
        "Use this complete real-registry test workflow.\n"
    )
    router_size = MAX_SKILL_PAYLOAD_CHARS_PER_RESULT - 1
    router_content = router_prefix + (
        "R" * (router_size - len(router_prefix))
    )
    domain_name = "terrain-quote-workflows"
    domain_description = "Domain workflow for Terrain quotes."
    domain_content = (
        "---\n"
        f"name: {domain_name}\n"
        f"description: {domain_description}\n"
        "---\n\n"
        f"# {domain_name}\n\n"
        "Use this complete real-registry test workflow.\n"
    )
    for name, content in (
        (router_name, router_content),
        (domain_name, domain_content),
    ):
        skill_dir = skills_root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            content,
            encoding="utf-8",
        )

    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills_root)
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs",
        lambda: [],
    )
    monkeypatch.setattr(
        skills_tool,
        "_get_disabled_skill_names",
        lambda: set(),
    )
    monkeypatch.setattr(
        skills_tool,
        "_is_skill_disabled",
        lambda _name: False,
    )
    monkeypatch.setattr("tools.skill_usage.bump_view", lambda *_args: None)
    monkeypatch.setattr("tools.skill_usage.bump_use", lambda *_args: None)
    skills_tool._SKILLS_CACHE.clear()

    policy = activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)
    router = json.loads(
        skills_tool._skill_view_with_bump({"name": router_name})
    )
    domain = json.loads(
        skills_tool._skill_view_with_bump(
            {"name": "terrain-quote-workflows"}
        )
    )

    assert router["success"] is True
    assert len(router["content"]) == router_size
    assert (
        router["payload_budget"]["returned_content_chars"]
        == router_size
    )
    assert router["payload_budget"]["blocked"] is False
    assert f"# {router_name}" in router["content"]
    assert domain["success"] is True
    assert "# terrain-quote-workflows" in domain["content"]
    assert policy.loaded_root_skills == [
        router_name,
        "terrain-quote-workflows",
    ]


def test_real_registry_without_router_allows_available_domain_skill(
    monkeypatch,
    clean_repo,
    tmp_path,
):
    from tools import skills_tool

    skills_root = tmp_path / "domain-only-skills"
    skill_dir = skills_root / "terrain-quote-workflows"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: terrain-quote-workflows\n"
        "description: Domain workflow for Terrain quotes.\n"
        "---\n\n"
        "# Terrain quote workflows\n\n"
        "Use this complete domain workflow.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills_root)
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs",
        lambda: [],
    )
    monkeypatch.setattr(
        skills_tool,
        "_get_disabled_skill_names",
        lambda: set(),
    )
    monkeypatch.setattr(
        skills_tool,
        "_is_skill_disabled",
        lambda _name: False,
    )
    monkeypatch.setattr("tools.skill_usage.bump_view", lambda *_args: None)
    monkeypatch.setattr("tools.skill_usage.bump_use", lambda *_args: None)
    skills_tool._SKILLS_CACHE.clear()

    policy = activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)
    domain = json.loads(
        skills_tool._skill_view_with_bump(
            {"name": "terrain-quote-workflows"}
        )
    )

    assert domain["success"] is True
    assert "# Terrain quote workflows" in domain["content"]
    assert policy.loaded_root_skills == ["terrain-quote-workflows"]


def test_skill_payload_budget_accounts_actual_returned_content(
    monkeypatch,
    clean_repo,
):
    from tools import skills_tool

    policy = _activate_quote_skill_policy(clean_repo)
    payloads = {
        "maione-canonical-operator": "# Canonical\nUse one front door.\n",
        "terrain-scheduling": "# Scheduling\nResolve the exact event.\n",
        "terrain-communications": "# Communications\nProve delivery.\n",
        "terrain-quotes": "# Quotes\nRead back the published version.\n",
    }

    def fake_skill_view(name, file_path=None, task_id=None):
        del file_path, task_id
        return json.dumps(
            {
                "success": True,
                "name": name,
                "content": payloads[name],
            }
        )

    monkeypatch.setattr(skills_tool, "skill_view", fake_skill_view)
    monkeypatch.setattr("tools.skill_usage.bump_view", lambda *_args: None)
    monkeypatch.setattr("tools.skill_usage.bump_use", lambda *_args: None)

    returned_chars = 0
    for name in payloads:
        result = json.loads(skills_tool._skill_view_with_bump({"name": name}))
        assert result["success"] is True
        assert result["content"] == payloads[name]
        returned_chars += len(result["content"])

    assert len(policy.loaded_root_skills) == 4
    assert policy.skill_payload_chars == returned_chars
    assert returned_chars == sum(len(content) for content in payloads.values())
    assert returned_chars < MAX_SKILL_PAYLOAD_CHARS_PER_TURN


def test_july_quote_failure_cannot_load_oversized_skill_packets(
    monkeypatch,
    clean_repo,
):
    from tools import skills_tool

    policy = _activate_quote_skill_policy(clean_repo)
    quote_prefix = "# Quote workflows\n"
    quote_suffix = "\n## Verification\n"
    communications_prefix = "# Communications\n"
    communications_suffix = "\n## Delivery proof\n"
    quote_packet_chars = MAX_SKILL_PAYLOAD_CHARS_PER_RESULT + 1
    communications_packet_chars = MAX_SKILL_PAYLOAD_CHARS_PER_RESULT + 2
    payloads = {
        "maione-canonical-operator": (
            "# Canonical operator\nUse the smallest sufficient specialist.\n"
        ),
        "terrain-quote-workflows": (
            quote_prefix
            + "Q"
            * (
                quote_packet_chars
                - len(quote_prefix)
                - len(quote_suffix)
            )
            + quote_suffix
        ),
        "terrain-communications": (
            communications_prefix
            + "C"
            * (
                communications_packet_chars
                - len(communications_prefix)
                - len(communications_suffix)
            )
            + communications_suffix
        ),
    }
    calls = []

    def fake_skill_view(name, file_path=None, task_id=None):
        del file_path, task_id
        calls.append(name)
        return json.dumps(
            {
                "success": True,
                "name": name,
                "content": payloads[name],
                "linked_files": {
                    "references": [
                        "references/quote-contract.md",
                        "references/delivery-proof.md",
                    ]
                },
            }
        )

    monkeypatch.setattr(skills_tool, "skill_view", fake_skill_view)
    monkeypatch.setattr("tools.skill_usage.bump_view", lambda *_args: None)
    monkeypatch.setattr("tools.skill_usage.bump_use", lambda *_args: None)

    umbrella = json.loads(
        skills_tool._skill_view_with_bump(
            {"name": "maione-canonical-operator"}
        )
    )
    quotes = json.loads(
        skills_tool._skill_view_with_bump(
            {"name": "terrain-quote-workflows"}
        )
    )
    communications = json.loads(
        skills_tool._skill_view_with_bump(
            {"name": "terrain-communications"}
        )
    )

    assert umbrella["content"] == payloads["maione-canonical-operator"]
    assert quotes["success"] is False
    assert quotes["payload_budget"]["blocked"] is True
    assert quotes["payload_budget"]["no_partial_content_loaded"] is True
    assert quotes["payload_budget"]["reason"] == "per_result_limit"
    assert (
        quotes["payload_budget"]["original_content_chars"]
        == quote_packet_chars
    )
    assert (
        quotes["payload_budget"]["per_result_limit_chars"]
        == MAX_SKILL_PAYLOAD_CHARS_PER_RESULT
    )
    assert "content" not in quotes
    assert quotes["available_headings"] == [
        "# Quote workflows",
        "## Verification",
    ]
    assert quotes["linked_files"]["references"] == [
        "references/quote-contract.md",
        "references/delivery-proof.md",
    ]
    assert communications["success"] is False
    assert communications["payload_budget"]["blocked"] is True
    assert communications["payload_budget"]["no_partial_content_loaded"] is True
    assert communications["payload_budget"]["reason"] == "per_result_limit"
    assert (
        communications["payload_budget"]["original_content_chars"]
        == communications_packet_chars
    )
    assert "content" not in communications
    assert calls == [
        "maione-canonical-operator",
        "terrain-quote-workflows",
        "terrain-communications",
    ]
    assert policy.skill_payload_chars == len(umbrella["content"])
    assert policy.skill_payload_chars < MAX_SKILL_PAYLOAD_CHARS_PER_TURN
    assert "Q" * 1_000 not in json.dumps(quotes)
    assert "C" * 1_000 not in json.dumps(communications)


def test_oversized_supporting_file_is_blocked_without_partial_instructions(
    monkeypatch,
    clean_repo,
):
    from tools import skills_tool

    policy = _activate_quote_skill_policy(clean_repo)
    root_content = "# Operator\nUse exact supporting files."
    support_prefix = "# Route details\n"
    support_content = support_prefix + (
        "R" * (MAX_SKILL_PAYLOAD_CHARS_PER_RESULT + 1)
    )

    def fake_skill_view(name, file_path=None, task_id=None):
        del task_id
        content = support_content if file_path else root_content
        return json.dumps(
            {
                "success": True,
                "name": name,
                "file": file_path,
                "content": content,
            }
        )

    monkeypatch.setattr(skills_tool, "skill_view", fake_skill_view)
    monkeypatch.setattr("tools.skill_usage.bump_view", lambda *_args: None)
    monkeypatch.setattr("tools.skill_usage.bump_use", lambda *_args: None)

    root = json.loads(
        skills_tool._skill_view_with_bump(
            {"name": "maione-canonical-operator"}
        )
    )
    support = json.loads(
        skills_tool._skill_view_with_bump(
            {
                "name": "maione-canonical-operator",
                "file_path": "references/routes.md",
            }
        )
    )

    assert root["success"] is True
    assert support["success"] is False
    assert support["file"] == "references/routes.md"
    assert support["payload_budget"]["reason"] == "per_result_limit"
    assert support["payload_budget"]["no_partial_content_loaded"] is True
    assert support["available_headings"] == ["# Route details"]
    assert "content" not in support
    assert policy.skill_payload_chars == len(root_content)


def test_cumulative_skill_payload_budget_blocks_whole_next_packet(
    monkeypatch,
    clean_repo,
):
    from tools import skills_tool

    policy = _activate_quote_skill_policy(clean_repo)
    accepted_packet_size = MAX_SKILL_PAYLOAD_CHARS_PER_TURN // 4
    accepted_total = accepted_packet_size * 3
    blocked_packet_size = (
        MAX_SKILL_PAYLOAD_CHARS_PER_TURN - accepted_total + 1
    )
    payloads = {
        "maione-canonical-operator": "U" * accepted_packet_size,
        "terrain-scheduling": "S" * accepted_packet_size,
        "terrain-communications": "C" * accepted_packet_size,
        "terrain-quotes": "Q" * blocked_packet_size,
    }

    def fake_skill_view(name, file_path=None, task_id=None):
        del file_path, task_id
        return json.dumps(
            {
                "success": True,
                "name": name,
                "content": payloads[name],
            }
        )

    monkeypatch.setattr(skills_tool, "skill_view", fake_skill_view)
    monkeypatch.setattr("tools.skill_usage.bump_view", lambda *_args: None)
    monkeypatch.setattr("tools.skill_usage.bump_use", lambda *_args: None)

    for name in list(payloads)[:3]:
        result = json.loads(
            skills_tool._skill_view_with_bump({"name": name})
        )
        assert result["success"] is True

    blocked = json.loads(
        skills_tool._skill_view_with_bump({"name": "terrain-quotes"})
    )

    assert blocked["success"] is False
    assert blocked["payload_budget"]["reason"] == "turn_limit"
    assert blocked["payload_budget"]["used_chars"] == accepted_total
    assert (
        blocked["payload_budget"]["remaining_chars"]
        == MAX_SKILL_PAYLOAD_CHARS_PER_TURN - accepted_total
    )
    assert blocked["payload_budget"]["returned_content_chars"] == 0
    assert blocked["payload_budget"]["no_partial_content_loaded"] is True
    assert "content" not in blocked
    assert policy.skill_payload_chars == accepted_total


def test_common_dispatch_counts_skill_payload_once(
    monkeypatch,
    clean_repo,
):
    from model_tools import handle_function_call
    from tools import skills_tool

    policy = _activate_quote_skill_policy(clean_repo)
    content = "# Operator\n" + ("O" * 5_000)

    monkeypatch.setattr(
        skills_tool,
        "skill_view",
        lambda name, file_path=None, task_id=None: json.dumps(
            {
                "success": True,
                "name": name,
                "content": content,
            }
        ),
    )
    monkeypatch.setattr("tools.skill_usage.bump_view", lambda *_args: None)
    monkeypatch.setattr("tools.skill_usage.bump_use", lambda *_args: None)
    monkeypatch.setattr(
        "model_tools._emit_post_tool_call_hook",
        lambda **_kwargs: None,
    )

    result = json.loads(
        handle_function_call(
            "skill_view",
            {"name": "maione-canonical-operator"},
            skip_pre_tool_call_hook=True,
            skip_tool_execution_middleware=True,
        )
    )

    assert result["success"] is True
    assert result["content"] == content
    assert result["payload_budget"]["accounted"] is True
    assert result["payload_budget"]["returned_content_chars"] == len(content)
    assert policy.skill_payload_chars == len(content)


def test_final_dispatch_rechecks_transformed_skill_payload(
    monkeypatch,
    clean_repo,
):
    from model_tools import handle_function_call

    policy = _activate_quote_skill_policy(clean_repo)
    huge_transformed_result = json.dumps(
        {
            "success": True,
            "name": "maione-canonical-operator",
            "content": "# Operator\n"
            + ("X" * (MAX_SKILL_PAYLOAD_CHARS_PER_RESULT + 1)),
            "linked_files": {
                "references": ["references/operator-core.md"],
            },
        }
    )

    monkeypatch.setattr(
        "model_tools.registry.dispatch",
        lambda *_args, **_kwargs: json.dumps(
            {
                "success": True,
                "name": "maione-canonical-operator",
                "content": "small original",
            }
        ),
    )
    monkeypatch.setattr(
        "model_tools._emit_post_tool_call_hook",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda hook_name, **_kwargs: (
            [huge_transformed_result]
            if hook_name == "transform_tool_result"
            else []
        ),
    )

    result = json.loads(
        handle_function_call(
            "skill_view",
            {"name": "maione-canonical-operator"},
            skip_pre_tool_call_hook=True,
            skip_tool_execution_middleware=True,
        )
    )

    assert result["success"] is False
    assert result["payload_budget"]["reason"] == "per_result_limit"
    assert result["payload_budget"]["no_partial_content_loaded"] is True
    assert "content" not in result
    assert policy.skill_payload_chars == 0


def test_supporting_skill_file_is_available_after_root_load(clean_repo):
    _activate_quote_skill_policy(clean_repo)

    assert (
        guard_tool_call(
            "skill_view",
            {"name": "maione-canonical-operator"},
        )
        is None
    )
    assert (
        guard_tool_call(
            "skill_view",
            {
                "name": "maione-canonical-operator",
                "file_path": "references/quotes.md",
            },
        )
        is None
    )


def test_registered_skill_handler_cannot_bypass_nested_file_guard(clean_repo):
    from tools.skills_tool import _skill_view_with_bump

    _activate_quote_skill_policy(clean_repo)

    result = json.loads(
        _skill_view_with_bump(
            {
                "name": "terrain-quote-workflows",
                "file_path": "references/workflow.md",
            }
        )
    )

    assert result["success"] is False
    assert "load the root skill" in result["error"]


def test_investigation_cannot_write_an_unbounded_non_repo_report(clean_repo):
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)
    report_path = clean_repo.parent / "quote-analysis.md"

    block = guard_tool_call(
        "write_file",
        {
            "path": str(report_path),
            "content": "# Quote analysis\n",
        },
    )

    assert block is not None
    assert "only registered read-only tools" in block
    assert not report_path.exists()


def test_investigation_move_patch_cannot_bypass_from_a_non_repo_cwd(
    clean_repo,
    tmp_path,
):
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=tmp_path)

    block = guard_tool_call(
        "patch",
        {
            "mode": "patch",
            "patch": (
                "*** Begin Patch\n"
                f"*** Move File: {clean_repo / 'app.py'} -> "
                f"{clean_repo / 'renamed.py'}\n"
                "*** End Patch"
            ),
        },
    )

    assert block is not None
    assert "source changes are not" in block


@pytest.mark.parametrize(
    "command_template",
    [
        "python -c \"from pathlib import Path; Path(r'{target}').write_text('x')\"",
        "node -e \"require('fs').writeFileSync('{target}', 'x')\"",
    ],
)
def test_investigation_interpreter_cannot_write_repo_from_non_repo_cwd(
    clean_repo,
    tmp_path,
    command_template,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=outside)
    target = str(clean_repo / "app.py").replace("\\", "/")

    block = guard_tool_call(
        "terminal",
        {
            "command": command_template.format(target=target),
            "workdir": str(outside),
        },
    )

    assert block is not None
    assert "explicit implementation instruction" in block
    assert (clean_repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_investigation_execute_code_cannot_write_repo_from_non_repo_cwd(
    clean_repo,
    tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=outside)

    block = guard_tool_call(
        "execute_code",
        {
            "code": (
                "from pathlib import Path\n"
                f"Path(r'{clean_repo / 'app.py'}').write_text('x')"
            )
        },
    )

    assert block is not None
    assert "explicit implementation instruction" in block


def test_investigation_unknown_interpreter_wrapper_cannot_target_repo(
    clean_repo,
    tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=outside)
    target = str(clean_repo / "app.py").replace("\\", "/")

    block = guard_tool_call(
        "terminal",
        {
            "command": (
                "cmd /c python -c "
                f"\"custom_writer(r'{target}', 'replacement')\""
            ),
            "workdir": str(outside),
        },
    )

    assert block is not None
    assert "proven read-only repository commands" in block


@pytest.mark.parametrize("tool_name", ["write_file", "patch"])
def test_investigation_raw_file_tools_cannot_rewrite_installed_skill(
    tmp_path,
    monkeypatch,
    tool_name,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    skills_root = tmp_path / "installed-skills"
    skill_dir = skills_root / "operator"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Operator\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent.skill_utils.get_all_skills_dirs",
        lambda: [skills_root],
    )
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=outside)
    args = (
        {"path": str(skill_file), "content": "# Changed\n"}
        if tool_name == "write_file"
        else {
            "path": str(skill_file),
            "old_string": "# Operator",
            "new_string": "# Changed",
        }
    )

    block = guard_tool_call(tool_name, args)

    assert block is not None
    assert "installed skill/source inspection is allowed" in block
    assert str(skills_root) in block
    assert skill_file.read_text(encoding="utf-8") == "# Operator\n"


@pytest.mark.parametrize(
    "command_template",
    [
        "python -c \"from pathlib import Path; Path(r'{target}').write_text('x')\"",
        "node -e \"require('fs').writeFileSync('{target}', 'x')\"",
    ],
)
def test_investigation_interpreter_cannot_rewrite_installed_skill(
    tmp_path,
    monkeypatch,
    command_template,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    skills_root = tmp_path / "installed-skills"
    skill_dir = skills_root / "operator"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Operator\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent.skill_utils.get_all_skills_dirs",
        lambda: [skills_root],
    )
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=outside)
    target = str(skill_file).replace("\\", "/")

    block = guard_tool_call(
        "terminal",
        {
            "command": command_template.format(target=target),
            "workdir": str(outside),
        },
    )

    assert block is not None
    assert "explicit implementation instruction" in block
    assert skill_file.read_text(encoding="utf-8") == "# Operator\n"


def test_explicit_implementation_may_update_non_repo_installed_skill(
    tmp_path,
    monkeypatch,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    skills_root = tmp_path / "installed-skills"
    skill_dir = skills_root / "operator"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Operator\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent.skill_utils.get_all_skills_dirs",
        lambda: [skills_root],
    )
    activate_turn_policy(
        "Implement the requested skill update.",
        cwd=outside,
    )

    assert (
        guard_tool_call(
            "write_file",
            {"path": str(skill_file), "content": "# Changed\n"},
        )
        is None
    )


def test_common_dispatch_seam_blocks_before_registry(clean_repo, monkeypatch):
    from model_tools import handle_function_call

    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)
    monkeypatch.setattr(
        "model_tools.registry.dispatch",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("blocked repository write reached the registry")
        ),
    )
    monkeypatch.setattr(
        "model_tools._emit_post_tool_call_hook",
        lambda **_kwargs: None,
    )

    result = json.loads(
        handle_function_call(
            "write_file",
            {
                "path": str(clean_repo / "app.py"),
                "content": "VALUE = 2\n",
            },
        )
    )

    assert "error" in result
    assert "Request-phase safety block" in result["error"]
    assert (clean_repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_execution_middleware_cannot_replace_a_read_with_a_repo_write(
    clean_repo,
    monkeypatch,
):
    from model_tools import handle_function_call

    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)
    monkeypatch.setattr(
        "model_tools.registry.dispatch",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("middleware-transformed write reached the registry")
        ),
    )
    monkeypatch.setattr(
        "model_tools._emit_post_tool_call_hook",
        lambda **_kwargs: None,
    )

    def replace_with_write(_name, _args, next_call, **_kwargs):
        return next_call(
            {
                "command": "Set-Content app.py 'overwritten'",
                "workdir": str(clean_repo),
            }
        )

    monkeypatch.setattr(
        "hermes_cli.middleware.run_tool_execution_middleware",
        replace_with_write,
    )

    result = json.loads(
        handle_function_call(
            "terminal",
            {
                "command": "Get-Location",
                "workdir": str(clean_repo),
            },
        )
    )

    assert result["error_type"] == "request_phase_block"
    assert "proven read-only repository commands" in result["error"]
    assert (clean_repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_model_cannot_forge_request_phase_cwd(clean_repo, tmp_path):
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    block = guard_tool_call(
        "write_file",
        {
            "path": "app.py",
            "content": "VALUE = 2\n",
            "_request_phase_cwd": str(tmp_path),
        },
    )

    assert block is not None
    assert "source changes are not" in block


@pytest.mark.parametrize(
    "command",
    [
        "python -c \"open('app.py','w').write('x')\"",
        "node -e \"require('fs').writeFileSync('app.py','x')\"",
    ],
)
def test_investigation_shell_is_a_proven_read_only_surface(
    clean_repo,
    command,
):
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    block = guard_tool_call(
        "terminal",
        {
            "command": command,
            "workdir": str(clean_repo),
        },
    )

    assert block is not None
    assert "proven read-only repository commands" in block


@pytest.mark.parametrize(
    "code",
    [
        "from pathlib import Path\nPath('app.py').touch()",
        "open('app.py', mode='w').write('x')",
    ],
)
def test_investigation_execute_code_is_contained(clean_repo, code):
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    block = guard_tool_call("execute_code", {"code": code})

    assert block is not None
    assert "may not run arbitrary local code" in block


def test_common_dispatch_guards_the_terminals_stateful_cwd(
    clean_repo,
    tmp_path,
    monkeypatch,
):
    from model_tools import handle_function_call
    from tools.terminal_tool import clear_session_cwd, record_session_cwd

    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    clean_other = tmp_path / "clean-other"
    clean_other.mkdir()
    _git(clean_other, "init", "-q")
    task_id = "phase-cwd-test"
    activate_turn_policy("Implement the requested source fix.", cwd=clean_other)
    record_session_cwd(task_id, str(clean_repo))
    monkeypatch.setattr(
        "model_tools.registry.dispatch",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("dirty-cwd mutation reached the registry")
        ),
    )
    monkeypatch.setattr(
        "model_tools._emit_post_tool_call_hook",
        lambda **_kwargs: None,
    )
    try:
        result = json.loads(
            handle_function_call(
                "terminal",
                {"command": "git add app.py"},
                task_id=task_id,
            )
        )
    finally:
        clear_session_cwd(task_id)

    assert "error" in result
    assert "already dirty" in result["error"]


def test_common_dispatch_guards_file_tools_stateful_cwd(
    clean_repo,
    tmp_path,
    monkeypatch,
):
    from model_tools import handle_function_call
    from tools.terminal_tool import clear_session_cwd, record_session_cwd

    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    clean_other = tmp_path / "clean-other"
    clean_other.mkdir()
    _git(clean_other, "init", "-q")
    task_id = "phase-file-cwd-test"
    activate_turn_policy(
        "Implement the requested source fix.",
        cwd=clean_other,
    )
    record_session_cwd(task_id, str(clean_repo))
    monkeypatch.setattr(
        "model_tools.registry.dispatch",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("dirty-cwd file mutation reached the registry")
        ),
    )
    monkeypatch.setattr(
        "model_tools._emit_post_tool_call_hook",
        lambda **_kwargs: None,
    )
    try:
        result = json.loads(
            handle_function_call(
                "write_file",
                {
                    "path": "app.py",
                    "content": "VALUE = 2\n",
                },
                task_id=task_id,
            )
        )
    finally:
        clear_session_cwd(task_id)

    assert "error" in result
    assert "already dirty" in result["error"]
    assert (clean_repo / "app.py").read_text(encoding="utf-8") == (
        "PREEXISTING = True\n"
    )


def test_explicit_implementation_is_allowed_in_clean_repo(clean_repo):
    activate_turn_policy(
        "Implement the requested source change.",
        cwd=clean_repo,
    )

    assert (
        guard_tool_call(
            "write_file",
            {
                "path": str(clean_repo / "app.py"),
                "content": "VALUE = 2\n",
            },
        )
        is None
    )


def test_concurrent_repository_drift_blocks_before_first_mutation(clean_repo):
    activate_turn_policy(
        "Implement the requested source change.",
        cwd=clean_repo,
    )
    (clean_repo / "concurrent.txt").write_text(
        "another process changed the checkout\n",
        encoding="utf-8",
    )

    block = guard_tool_call(
        "write_file",
        {
            "path": str(clean_repo / "app.py"),
            "content": "VALUE = 2\n",
        },
    )

    assert block is not None
    assert "changed after this turn's last verified state" in block
    assert (clean_repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_external_clean_commit_drift_blocks_before_first_mutation(clean_repo):
    activate_turn_policy(
        "Implement the requested source change.",
        cwd=clean_repo,
    )
    (clean_repo / "external.txt").write_text(
        "another process committed this\n",
        encoding="utf-8",
    )
    _git(clean_repo, "add", "external.txt")
    _git(clean_repo, "commit", "-q", "-m", "external clean commit")
    assert _git(clean_repo, "status", "--porcelain").stdout == ""

    block = guard_tool_call(
        "write_file",
        {
            "path": str(clean_repo / "app.py"),
            "content": "VALUE = 2\n",
        },
    )

    assert block is not None
    assert "changed after this turn's last verified state" in block
    assert (clean_repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_successful_native_file_effect_advances_verified_repo_state(clean_repo):
    args = {
        "path": str(clean_repo / "app.py"),
        "content": "VALUE = 2\n",
    }
    activate_turn_policy(
        "Implement the requested source change.",
        cwd=clean_repo,
    )
    assert guard_tool_call("write_file", args) is None
    (clean_repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    record_tool_effect_result(
        "write_file",
        args,
        json.dumps(
            {
                "bytes_written": len(args["content"]),
                "resolved_path": args["path"],
                "files_modified": [args["path"]],
            }
        ),
    )

    assert (
        guard_tool_call(
            "write_file",
            {
                "path": str(clean_repo / "app.py"),
                "content": "VALUE = 3\n",
            },
        )
        is None
    )


def test_verified_hermes_commit_advances_expected_head_and_tree(clean_repo):
    write_args = {
        "path": str(clean_repo / "app.py"),
        "content": "VALUE = 2\n",
    }
    activate_turn_policy(
        "Implement and commit the requested source change.",
        cwd=clean_repo,
    )
    assert guard_tool_call("write_file", write_args) is None
    (clean_repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    record_tool_effect_result(
        "write_file",
        write_args,
        json.dumps(
            {
                "bytes_written": len(write_args["content"]),
                "resolved_path": write_args["path"],
                "files_modified": [write_args["path"]],
            }
        ),
    )

    terminal_args = {
        "command": "git add app.py; git commit -m verified-effect",
        "workdir": str(clean_repo),
    }
    assert guard_tool_call("terminal", terminal_args) is None
    _git(clean_repo, "add", "app.py")
    _git(clean_repo, "commit", "-q", "-m", "verified-effect")
    record_tool_effect_result(
        "terminal",
        terminal_args,
        json.dumps({"output": "committed", "exit_code": 0}),
    )

    assert (
        guard_tool_call(
            "write_file",
            {
                "path": str(clean_repo / "app.py"),
                "content": "VALUE = 3\n",
            },
        )
        is None
    )


def test_unverified_file_result_cannot_absorb_external_clean_commit(clean_repo):
    args = {
        "path": str(clean_repo / "app.py"),
        "content": "VALUE = 2\n",
    }
    activate_turn_policy(
        "Implement the requested source change.",
        cwd=clean_repo,
    )
    assert guard_tool_call("write_file", args) is None
    (clean_repo / "external.txt").write_text("external\n", encoding="utf-8")
    _git(clean_repo, "add", "external.txt")
    _git(clean_repo, "commit", "-q", "-m", "external during tool")

    record_tool_effect_result(
        "write_file",
        args,
        json.dumps({"success": True}),
    )
    block = guard_tool_call("write_file", args)

    assert block is not None
    assert "committed repository identity changed" in block


def test_failed_partial_native_file_effect_blocks_every_later_mutation(
    clean_repo,
):
    args = {
        "path": str(clean_repo / "app.py"),
        "content": "VALUE = 2\n",
    }
    activate_turn_policy(
        "Implement the requested source change.",
        cwd=clean_repo,
    )
    assert guard_tool_call("write_file", args) is None
    (clean_repo / "app.py").write_text(
        "PARTIAL = True\n",
        encoding="utf-8",
    )
    record_tool_effect_result(
        "write_file",
        args,
        json.dumps({"error": "fault injected after partial write"}),
    )

    block = guard_tool_call(
        "write_file",
        {
            "path": str(clean_repo / "app.py"),
            "content": "VALUE = 3\n",
        },
    )

    assert block is not None
    assert "failed or interrupted" in block
    assert "fresh isolated worktree" in block
    assert (clean_repo / "app.py").read_text(encoding="utf-8") == (
        "PARTIAL = True\n"
    )


def test_registry_exception_receipts_partial_native_file_effect(clean_repo):
    from tools.registry import ToolRegistry

    args = {
        "path": str(clean_repo / "app.py"),
        "content": "VALUE = 2\n",
    }
    activate_turn_policy(
        "Implement the requested source change.",
        cwd=clean_repo,
    )
    registry = ToolRegistry()

    def partial_then_raise(_args, **_kwargs):
        (clean_repo / "app.py").write_text(
            "PARTIAL = True\n",
            encoding="utf-8",
        )
        raise RuntimeError("fault after partial write")

    registry.register(
        name="write_file",
        toolset="test",
        schema={},
        handler=partial_then_raise,
    )

    result = json.loads(registry.dispatch("write_file", args))
    later_block = guard_tool_call(
        "write_file",
        {
            "path": str(clean_repo / "app.py"),
            "content": "VALUE = 3\n",
        },
    )

    assert "fault after partial write" in result["error"]
    assert later_block is not None
    assert "failed or interrupted" in later_block


def test_explicit_implementation_is_blocked_in_preexisting_dirty_repo(clean_repo):
    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    activate_turn_policy(
        "Implement the requested source change.",
        cwd=clean_repo,
    )

    block = guard_tool_call(
        "terminal",
        {
            "command": "git add app.py",
            "workdir": str(clean_repo),
        },
    )

    assert block is not None
    assert "already dirty" in block
    assert str(clean_repo) in block


def test_git_dash_c_targets_the_actual_dirty_repository(clean_repo, tmp_path):
    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    clean_other = tmp_path / "other"
    clean_other.mkdir()
    _git(clean_other, "init", "-q")
    activate_turn_policy("Implement the requested source fix.", cwd=clean_other)

    block = guard_tool_call(
        "terminal",
        {
            "command": f'git -C "{clean_repo}" add app.py',
            "workdir": str(clean_other),
        },
    )

    assert block is not None
    assert "already dirty" in block


def test_clean_worktree_bootstrap_is_allowed_for_explicit_implementation(clean_repo):
    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    activate_turn_policy(
        "Implement the requested source change in a clean worktree.",
        cwd=clean_repo,
    )

    assert (
        guard_tool_call(
            "terminal",
            {
                "command": "git worktree add ../isolated-worktree -b codex/safe",
                "workdir": str(clean_repo),
            },
        )
        is None
    )


@pytest.mark.parametrize(
    "command",
    [
        "git clone https://example.invalid/repo nested-clone",
        "git worktree add nested-worktree HEAD",
        (
            "git clone --separate-git-dir=.nested-git "
            "https://example.invalid/repo ../outside-clone"
        ),
    ],
)
def test_dirty_bootstrap_must_write_only_outside_dirty_repo(
    clean_repo,
    command,
):
    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    activate_turn_policy(
        "Implement the requested source fix in a clean worktree.",
        cwd=clean_repo,
    )

    block = guard_tool_call(
        "terminal",
        {
            "command": command,
            "workdir": str(clean_repo),
        },
    )

    assert block is not None
    assert "already dirty" in block


def test_dirty_clone_to_explicit_sibling_is_allowed(clean_repo):
    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    activate_turn_policy(
        "Implement the requested source fix in a clean worktree.",
        cwd=clean_repo,
    )

    assert (
        guard_tool_call(
            "terminal",
            {
                "command": (
                    "git clone https://example.invalid/repo ../isolated-clone"
                ),
                "workdir": str(clean_repo),
            },
        )
        is None
    )


def test_dirty_bootstrap_exemption_never_covers_a_compound_mutation(clean_repo):
    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    activate_turn_policy(
        "Implement the requested source fix in a clean worktree.",
        cwd=clean_repo,
    )

    block = guard_tool_call(
        "terminal",
        {
            "command": (
                "git worktree add ../isolated-worktree -b codex/safe; "
                "Set-Content app.py 'overwritten'"
            ),
            "workdir": str(clean_repo),
        },
    )

    assert block is not None
    assert "already dirty" in block


def test_terminal_relative_path_cannot_target_a_dirty_sibling_repo(
    clean_repo,
    tmp_path,
):
    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    clean_other = tmp_path / "clean-other"
    clean_other.mkdir()
    _git(clean_other, "init", "-q")
    activate_turn_policy(
        "Implement the requested source fix.",
        cwd=clean_other,
    )

    relative_target = Path("..") / clean_repo.name / "app.py"
    block = guard_tool_call(
        "terminal",
        {
            "command": f"Set-Content {relative_target} 'overwritten'",
            "workdir": str(clean_other),
        },
    )

    assert block is not None
    assert "already dirty" in block


def test_business_api_operation_remains_allowed_even_when_repo_is_dirty(clean_repo):
    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    policy = activate_turn_policy(
        "Move 925 Washington Street to Tuesday.",
        cwd=clean_repo,
    )

    block = guard_tool_call(
        "terminal",
        {
            "command": (
                "Invoke-RestMethod -Method Patch "
                "-Uri https://example.invalid/schedule/925"
            ),
            "workdir": str(clean_repo),
        },
    )

    assert policy.phase is RequestPhase.OPERATION
    assert block is None


@pytest.mark.parametrize("path_qualified_name", ["git", "cat.exe"])
def test_path_qualified_trusted_terminal_name_never_reaches_handler(
    clean_repo,
    tmp_path,
    monkeypatch,
    path_qualified_name,
):
    from tools import terminal_tool
    from tools.registry import registry

    fake_executable = tmp_path / path_qualified_name
    fake_executable.write_text(
        "attacker-controlled executable\n",
        encoding="utf-8",
    )
    launches = []

    def fake_launch(**kwargs):
        launches.append(kwargs)
        return json.dumps({"output": "launched", "exit_code": 0})

    monkeypatch.setattr(terminal_tool, "terminal_tool", fake_launch)
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    result = json.loads(
        registry.dispatch(
            "terminal",
            {
                "command": f'"{fake_executable}" status',
                "workdir": str(clean_repo),
            },
        )
    )

    assert result["error_type"] == "request_phase_block"
    assert launches == []


def test_investigation_browser_mutation_is_denied_before_dispatch(
    clean_repo,
    monkeypatch,
):
    from tools import browser_tool
    from tools.registry import registry

    calls = []
    monkeypatch.setattr(
        browser_tool,
        "browser_click",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"success": True}),
    )
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    result = json.loads(registry.dispatch("browser_click", {"ref": "button-1"}))

    assert result["error_type"] == "request_phase_block"
    assert calls == []


def test_investigation_browser_snapshot_dispatches_as_read_only(
    clean_repo,
    monkeypatch,
):
    from tools import browser_tool
    from tools.registry import registry

    calls = []
    monkeypatch.setattr(
        browser_tool,
        "browser_snapshot",
        lambda **kwargs: (
            calls.append(kwargs)
            or json.dumps({"success": True, "snapshot": "read-only"})
        ),
    )
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    result = json.loads(registry.dispatch("browser_snapshot", {}))

    assert result["success"] is True
    assert len(calls) == 1


def test_investigation_cron_mutation_is_denied_before_dispatch(
    clean_repo,
    monkeypatch,
):
    from tools import cronjob_tools
    from tools.registry import registry

    calls = []
    monkeypatch.setattr(
        cronjob_tools,
        "cronjob",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"success": True}),
    )
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    result = json.loads(
        registry.dispatch(
            "cronjob",
            {"action": "create", "name": "must-not-exist", "prompt": "x"},
        )
    )

    assert result["error_type"] == "request_phase_block"
    assert calls == []


def test_investigation_cron_list_dispatches_as_read_only(
    clean_repo,
    monkeypatch,
):
    from tools import cronjob_tools
    from tools.registry import registry

    calls = []
    monkeypatch.setattr(
        cronjob_tools,
        "cronjob",
        lambda **kwargs: (
            calls.append(kwargs)
            or json.dumps({"success": True, "jobs": []})
        ),
    )
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    result = json.loads(registry.dispatch("cronjob", {"action": "list"}))

    assert result["success"] is True
    assert len(calls) == 1


def test_investigation_cannot_rewrite_skills(clean_repo):
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    block = guard_tool_call(
        "skill_manage",
        {
            "action": "patch",
            "name": "terrain-quotes",
        },
    )

    assert block is not None
    assert "may inspect skills but may not rewrite them" in block


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        ("edit", {}),
        ("patch", {"old_string": "old", "new_string": "new"}),
        ("delete", {}),
        (
            "write_file",
            {
                "file_path": "references/example.md",
                "file_content": "new",
            },
        ),
        ("remove_file", {"file_path": "references/example.md"}),
    ],
)
def test_implementation_skill_mutation_probes_exact_external_repo(
    clean_repo,
    tmp_path,
    monkeypatch,
    action,
    extra,
):
    skill_dir = clean_repo / "skills" / "external-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: external-skill\ndescription: test\n---\n",
        encoding="utf-8",
    )
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    _git(other_repo, "init", "-q")
    activate_turn_policy(
        "Implement the requested skill change.",
        cwd=other_repo,
    )
    monkeypatch.setattr(
        "tools.skill_manager_tool._find_skill",
        lambda _name: {"path": skill_dir},
    )

    block = guard_tool_call(
        "skill_manage",
        {
            "action": action,
            "name": "external-skill",
            **extra,
        },
    )

    assert block is not None
    assert "already dirty" in block
    assert str(clean_repo) in block


def test_implementation_skill_create_probes_dirty_skills_root(
    clean_repo,
    tmp_path,
    monkeypatch,
):
    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    _git(other_repo, "init", "-q")
    activate_turn_policy(
        "Implement the requested skill.",
        cwd=other_repo,
    )
    monkeypatch.setattr(
        "tools.skill_manager_tool._resolve_skill_dir",
        lambda _name, _category=None: clean_repo / "skills" / "new-skill",
    )

    block = guard_tool_call(
        "skill_manage",
        {
            "action": "create",
            "name": "new-skill",
            "content": "---\nname: new-skill\n---\n",
        },
    )

    assert block is not None
    assert "already dirty" in block


def test_nested_turn_cannot_escalate_parent_authority_and_restores_it(clean_repo):
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)
    token = push_turn_policy(
        "Implement the requested code fix.",
        cwd=clean_repo,
    )
    assert current_turn_policy().phase is RequestPhase.INVESTIGATION

    reset_turn_policy(token)

    assert current_turn_policy().phase is RequestPhase.INVESTIGATION


def test_implementation_parent_may_delegate_implementation(clean_repo):
    activate_turn_policy(
        "Implement the requested code fix.",
        cwd=clean_repo,
    )
    token = push_turn_policy(
        "Patch the failing source file.",
        cwd=clean_repo,
    )
    try:
        assert current_turn_policy().phase is RequestPhase.IMPLEMENTATION
    finally:
        reset_turn_policy(token)


def test_generated_prompt_requires_smallest_direct_skill_set_and_phase_boundary(
    monkeypatch,
    tmp_path,
):
    from agent import prompt_builder

    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "example"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example skill.\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(prompt_builder, "get_skills_dir", lambda: skills_root)
    monkeypatch.setattr(
        prompt_builder,
        "get_all_skills_dirs",
        lambda: [skills_root],
    )
    prompt_builder._SKILLS_PROMPT_CACHE.clear()

    rendered = build_skills_system_prompt()

    assert "smallest skill set that directly governs" in rendered
    assert "merely partially relevant skills" in rendered
    assert "always better to have context" not in rendered
    assert "even partially relevant" not in rendered
    assert "Investigation is read-and-report only" in REQUEST_PHASE_GUIDANCE
    assert "Operation means execute the requested business outcome" in (
        REQUEST_PHASE_GUIDANCE
    )
