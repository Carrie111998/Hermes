import pytest

from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.extension_canaries import (
    REQUIRED_RTK_CASES,
    evaluate_rtk_comparisons,
    evaluate_skill_retrieval,
    extension_capabilities,
    extension_health,
    is_pinned_revision,
)


def test_extension_defaults_are_profile_safe_and_fail_closed():
    retrieval = DEFAULT_CONFIG["skill_retrieval"]
    assert retrieval["enabled"] is False
    assert retrieval["top_k"] == 8
    assert retrieval["regression"]["status"] == "not_run"

    rtk = DEFAULT_CONFIG["rtk"]
    assert rtk["enabled"] is False
    assert rtk["routine_filter"] is False
    assert rtk["raw_bypass"] is True


@pytest.mark.parametrize("revision", ["", "latest", "main", "master", "HEAD"])
def test_unreviewed_floating_revisions_are_not_pinned(revision):
    assert is_pinned_revision(revision) is False


@pytest.mark.parametrize(
    "revision", ["v1.2.3", "1.2.3", "8a20a7a6a731a1fcb77c32102bfc21c17252e7c9"]
)
def test_release_tags_and_commit_shas_are_pinned(revision):
    assert is_pinned_revision(revision) is True


def test_skill_retrieval_regression_measures_expected_skill_in_top_k():
    corpus = [
        {"id": "email", "prompt": "triage my inbox", "expected_skill": "himalaya"},
        {"id": "pdf", "prompt": "merge these PDFs", "expected_skill": "pdf"},
    ]
    rankings = {
        "triage my inbox": ["email-inbox-triage", "himalaya", "google-workspace"],
        "merge these PDFs": ["docx", "xlsx", "powerpoint", "pdf"],
    }

    result = evaluate_skill_retrieval(
        corpus,
        lambda prompt, top_k: rankings[prompt][:top_k],
        top_k=3,
        minimum_recall=0.5,
    )

    assert result == {
        "status": "passed",
        "top_k": 3,
        "minimum_recall": 0.5,
        "total": 2,
        "hits": 1,
        "recall": 0.5,
        "misses": [{"id": "pdf", "expected_skill": "pdf"}],
    }


def test_skill_retrieval_regression_rejects_empty_corpus():
    with pytest.raises(ValueError, match="corpus"):
        evaluate_skill_retrieval([], lambda _prompt, _top_k: [])


def test_rtk_comparison_requires_all_routine_cases_and_preserves_raw_authority():
    cases = [
        {
            "case": case,
            "raw_output": f"raw:{case}",
            "compressed_output": f"compressed:{case}",
            "raw_bypass_output": f"raw:{case}",
            "raw_exit_code": 1 if case == "failing_output" else 0,
            "compressed_exit_code": 1 if case == "failing_output" else 0,
        }
        for case in REQUIRED_RTK_CASES
    ]

    result = evaluate_rtk_comparisons(cases)

    assert result["status"] == "passed"
    assert result["compressed_authoritative"] is False
    assert result["raw_bypass_verified"] is True
    assert result["exit_codes_preserved"] is True
    assert result["missing_cases"] == []


def test_rtk_comparison_fails_when_bypass_or_exit_code_changes():
    result = evaluate_rtk_comparisons(
        [
            {
                "case": "git_status",
                "raw_output": "raw",
                "compressed_output": "short",
                "raw_bypass_output": "not raw",
                "raw_exit_code": 1,
                "compressed_exit_code": 0,
            }
        ]
    )

    assert result["status"] == "failed"
    assert result["raw_bypass_verified"] is False
    assert result["exit_codes_preserved"] is False
    assert set(result["missing_cases"]) == set(REQUIRED_RTK_CASES) - {"git_status"}


def test_health_distinguishes_disabled_missing_canary_and_promoted():
    base = {
        "skill_retrieval": {
            "enabled": False,
            "plugin": "skill-retrieval",
            "promotion": "canary",
            "regression": {"status": "not_run"},
            "source": {"revision": "v1.0.0"},
        },
        "rtk": {
            "enabled": True,
            "plugin": "rtk-rewrite",
            "promotion": "canary",
            "raw_bypass": True,
            "regression": {"status": "not_run"},
            "source": {"revision": "v1.0.0"},
        },
    }
    assert extension_health(base, installed_plugins=set()) == {
        "skill_retrieval": "disabled",
        "rtk": "missing",
    }

    base["skill_retrieval"].update(
        enabled=True,
        promotion="promoted",
        regression={"status": "passed"},
    )
    assert extension_health(
        base, installed_plugins={"skill-retrieval", "rtk-rewrite"}
    ) == {"skill_retrieval": "promoted", "rtk": "installed"}

    base["rtk"]["regression"] = {"status": "passed"}
    assert extension_health(base, installed_plugins={"rtk-rewrite"}) == {
        "skill_retrieval": "missing",
        "rtk": "canary",
    }


def test_capabilities_only_advertise_verified_surfaces():
    config = {
        "skill_retrieval": {
            "enabled": True,
            "plugin": "skill-retrieval",
            "promotion": "canary",
            "regression": {"status": "passed"},
            "source": {"revision": "v1.0.0"},
        },
        "rtk": {
            "enabled": True,
            "plugin": "rtk-rewrite",
            "promotion": "canary",
            "routine_filter": True,
            "raw_bypass": True,
            "regression": {"status": "passed"},
            "source": {"revision": "v1.0.0"},
        },
    }

    assert extension_capabilities(
        config, installed_plugins={"skill-retrieval", "rtk-rewrite"}
    ) == {
        "skill_retrieval.retrieve",
        "rtk.routine_filter",
        "terminal.raw_evidence",
    }


def test_unpinned_installed_extension_cannot_become_canary():
    config = {
        "skill_retrieval": {
            "enabled": True,
            "plugin": "skill-retrieval",
            "promotion": "promoted",
            "regression": {"status": "passed"},
            "source": {"revision": "latest"},
        }
    }

    assert extension_health(config, installed_plugins={"skill-retrieval"}) == {
        "skill_retrieval": "installed",
        "rtk": "disabled",
    }
    assert extension_capabilities(
        config, installed_plugins={"skill-retrieval"}
    ) == set()