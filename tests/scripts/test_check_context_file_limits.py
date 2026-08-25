"""Invariants for scripts/check_context_file_limits.py.

The Hermes repository's own root ``AGENTS.md`` grew past the configured
startup context cap and was being silently head/tail truncated at session
start.  This lint is the guardrail against a recurrence: it models the
*selected* startup context chain (one winning context type per directory,
Hermes priority order) rather than summing every candidate file, and it
holds progressively-discovered nested context files to the subdirectory
hint budget.

Every test builds a throwaway repository on disk — nothing here reads the
implementation's source text.
"""

import json
from pathlib import Path

import scripts.check_context_file_limits as lint


def _repo(tmp_path, files):
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def test_root_agents_md_is_the_selected_startup_file(tmp_path):
    root = _repo(tmp_path, {"AGENTS.md": "# Root guide\n"})

    report = lint.scan(root)

    assert [entry["path"] for entry in report["startup_chain"]] == ["AGENTS.md"]


def test_startup_chain_is_empty_without_any_context_file(tmp_path):
    root = _repo(tmp_path, {"README.md": "no context here\n"})

    report = lint.scan(root)

    assert report["startup_chain"] == []


def test_hermes_md_outranks_agents_md_and_reports_it_as_shadowed(tmp_path):
    root = _repo(
        tmp_path,
        {".hermes.md": "# Wins\n", "AGENTS.md": "# Never loaded at startup\n"},
    )

    report = lint.scan(root)

    assert [entry["path"] for entry in report["startup_chain"]] == [".hermes.md"]
    assert report["startup_chain"][0]["shadowed"] == ["AGENTS.md"]


def test_agents_override_outranks_agents_md_in_the_same_directory(tmp_path):
    root = _repo(
        tmp_path,
        {"AGENTS.override.md": "# Local override\n", "AGENTS.md": "# Tracked\n"},
    )

    report = lint.scan(root)

    assert [entry["path"] for entry in report["startup_chain"]] == [
        "AGENTS.override.md"
    ]
    assert report["startup_chain"][0]["shadowed"] == ["AGENTS.md"]


def test_empty_candidates_fall_through_to_first_nonempty_context(tmp_path):
    from agent.prompt_builder import build_context_files_prompt

    root = _repo(
        tmp_path,
        {
            ".hermes.md": "  \n",
            "AGENTS.override.md": "\n",
            "AGENTS.md": "# Loaded guide\n",
        },
    )

    report = lint.scan(root)
    loaded = build_context_files_prompt(cwd=str(root), skip_soul=True)

    assert [entry["path"] for entry in report["startup_chain"]] == ["AGENTS.md"]
    assert report["startup_chain"][0]["shadowed"] == []
    assert "# Loaded guide" in loaded


def test_claude_variants_use_the_first_nonempty_file(tmp_path):
    from agent.prompt_builder import build_context_files_prompt

    root = _repo(
        tmp_path,
        {"CLAUDE.md": "\n", "claude.md": "# Lowercase guide\n"},
    )

    report = lint.scan(root)
    loaded = build_context_files_prompt(cwd=str(root), skip_soul=True)

    assert len(report["startup_chain"]) == 1
    selected = root / report["startup_chain"][0]["path"]
    assert selected.samefile(root / "claude.md")
    assert "# Lowercase guide" in loaded


def test_counts_unicode_characters_not_utf8_bytes(tmp_path):
    # Ten astral-plane characters: 40 UTF-8 bytes, 10 Unicode characters.
    root = _repo(tmp_path, {"AGENTS.md": "\U0001f600" * 10})

    report = lint.scan(root)

    assert report["startup_chain"][0]["chars"] == 10


def test_selected_startup_file_over_the_cap_fails(tmp_path):
    root = _repo(tmp_path, {"AGENTS.md": "x" * 41_000})

    report = lint.scan(root)

    assert report["ok"] is False
    assert report["failures"] == [
        {
            "kind": "startup_over_cap",
            "path": "AGENTS.md",
            "chars": 41_014,
            "source_chars": 41_000,
            "limit": 40_000,
        }
    ]


def test_selected_startup_file_under_the_cap_passes(tmp_path):
    root = _repo(tmp_path, {"AGENTS.md": "x" * 39_986})

    report = lint.scan(root)

    assert report["ok"] is True
    assert report["failures"] == []


def test_agents_chain_is_merged_deduped_and_checked_as_one_context(tmp_path):
    from agent.prompt_builder import build_context_files_prompt

    root = _repo(
        tmp_path,
        {
            ".git/HEAD": "ref: refs/heads/main\n",
            "AGENTS.md": "shared guidance",
            "apps/AGENTS.md": "shared guidance",
            "apps/desktop/AGENTS.md": "specific guidance",
        },
    )
    cwd = root / "apps" / "desktop"

    report = lint.scan(root, cwd=cwd, startup_cap=50)
    loaded = build_context_files_prompt(cwd=str(cwd), skip_soul=True)

    assert [entry["path"] for entry in report["startup_chain"]] == [
        "AGENTS.md",
        "apps/desktop/AGENTS.md",
    ]
    assert report["startup_chain"][0]["duplicates"] == ["apps/AGENTS.md"]
    assert [failure["kind"] for failure in report["failures"]] == [
        "startup_chain_over_cap"
    ]
    assert loaded.count("shared guidance") == 1
    assert loaded.count("specific guidance") == 1


def test_same_resolved_startup_path_is_not_loaded_twice(tmp_path):
    root = _repo(
        tmp_path,
        {"AGENTS.md": "# Shared\n", "apps/desktop/code.py": "pass\n"},
    )
    alias = root / "apps" / "AGENTS.md"
    alias.symlink_to(root / "AGENTS.md")

    report = lint.scan(root, cwd=root / "apps" / "desktop")

    assert [entry["path"] for entry in report["startup_chain"]] == ["AGENTS.md"]
    assert report["startup_chain"][0]["duplicates"] == ["apps/AGENTS.md"]


def test_subdirectory_context_file_is_reported_as_nested_not_startup(tmp_path):
    root = _repo(
        tmp_path,
        {"AGENTS.md": "# Root\n", "apps/desktop/AGENTS.md": "# Desktop\n"},
    )

    report = lint.scan(root)

    assert [entry["path"] for entry in report["startup_chain"]] == ["AGENTS.md"]
    assert [entry["path"] for entry in report["nested"]] == [
        "apps/desktop/AGENTS.md"
    ]


def test_nested_context_file_over_the_hint_budget_fails(tmp_path):
    root = _repo(
        tmp_path,
        {"AGENTS.md": "# Root\n", "apps/desktop/AGENTS.md": "y" * 8_001},
    )

    report = lint.scan(root)

    assert report["ok"] is False
    assert report["failures"] == [
        {
            "kind": "nested_over_cap",
            "path": "apps/desktop/AGENTS.md",
            "chars": 8_001,
            "limit": 8_000,
        }
    ]


def test_nested_context_file_at_the_hint_budget_passes(tmp_path):
    root = _repo(
        tmp_path,
        {"AGENTS.md": "# Root\n", "apps/desktop/AGENTS.md": "y" * 8_000},
    )

    report = lint.scan(root)

    assert report["ok"] is True


def test_nested_loader_uses_first_nonempty_file_per_directory(tmp_path):
    from agent.subdirectory_hints import SubdirectoryHintTracker

    root = _repo(
        tmp_path,
        {
            "AGENTS.md": "# Root\n",
            "apps/AGENTS.override.md": "\n",
            "apps/AGENTS.md": "# Apps guide\n",
            "apps/CLAUDE.md": "# Shadowed guide\n",
            "apps/code.py": "pass\n",
        },
    )

    report = lint.scan(root)
    loaded = SubdirectoryHintTracker(str(root)).check_tool_call(
        "read_file", {"path": "apps/code.py"}
    )

    assert [entry["path"] for entry in report["nested"]] == ["apps/AGENTS.md"]
    assert report["nested"][0]["shadowed"] == ["apps/CLAUDE.md"]
    assert "# Apps guide" in loaded
    assert "# Shadowed guide" not in loaded


def test_vendor_and_cache_directories_are_not_scanned(tmp_path):
    oversized = "z" * 9_000
    root = _repo(
        tmp_path,
        {
            "AGENTS.md": "# Root\n",
            "node_modules/pkg/AGENTS.md": oversized,
            "vendor/dep/AGENTS.md": oversized,
            ".venv/lib/AGENTS.md": oversized,
            "backups/AGENTS.md": oversized,
        },
    )

    report = lint.scan(root)

    assert report["nested"] == []
    assert report["ok"] is True


def test_empty_root_override_does_not_seed_nested_duplicate_dedupe(tmp_path):
    same = "# Shared guide\n" + "q" * 9_000
    root = _repo(
        tmp_path,
        {
            "AGENTS.override.md": "\n",
            "AGENTS.md": same,
            "apps/AGENTS.md": same,
        },
    )

    report = lint.scan(root)

    assert [entry["path"] for entry in report["startup_chain"]] == ["AGENTS.md"]
    assert [entry["path"] for entry in report["nested"]] == ["apps/AGENTS.md"]
    assert report["nested"][0]["duplicates"] == []
    assert [failure["path"] for failure in report["failures"]] == [
        "apps/AGENTS.md"
    ]


def test_nested_copy_identical_to_startup_stays_visible_as_provenance(tmp_path):
    same = "# Shared guide\n" + "q" * 9_000
    root = _repo(tmp_path, {"AGENTS.md": same, "apps/desktop/AGENTS.md": same})

    report = lint.scan(root)

    # Hermes does not inject this content twice, but review output must retain
    # the duplicate path so a healthy repository still explains the decision.
    assert report["nested"] == []
    assert report["startup_chain"][0]["duplicates"] == [
        "apps/desktop/AGENTS.md"
    ]
    assert report["ok"] is True


def test_two_identical_nested_copies_are_reported_once(tmp_path):
    same = "# Shared nested guide\n"
    root = _repo(
        tmp_path,
        {
            "AGENTS.md": "# Root\n",
            "apps/desktop/AGENTS.md": same,
            "apps/mobile/AGENTS.md": same,
        },
    )

    report = lint.scan(root)

    assert [entry["path"] for entry in report["nested"]] == [
        "apps/desktop/AGENTS.md"
    ]
    assert report["nested"][0]["duplicates"] == ["apps/mobile/AGENTS.md"]


def test_deeper_startup_cwd_selects_the_merged_parent_to_cwd_chain(tmp_path):
    root = _repo(
        tmp_path,
        {
            "AGENTS.md": "# Root\n",
            "apps/AGENTS.md": "# Apps\n",
            "apps/desktop/AGENTS.md": "# Desktop\n",
            "apps/mobile/AGENTS.md": "# Mobile\n",
        },
    )

    report = lint.scan(root, cwd=root / "apps" / "desktop")

    assert [entry["path"] for entry in report["startup_chain"]] == [
        "AGENTS.md",
        "apps/AGENTS.md",
        "apps/desktop/AGENTS.md",
    ]
    # Off-chain guides stay nested — they are never part of this startup load.
    assert [entry["path"] for entry in report["nested"]] == ["apps/mobile/AGENTS.md"]


def test_cursor_rule_files_are_collectively_loaded_and_capped(tmp_path):
    from agent.prompt_builder import build_context_files_prompt

    root = _repo(
        tmp_path,
        {
            ".cursor/rules/10-base.mdc": "a" * 25,
            ".cursor/rules/20-extra.mdc": "b" * 25,
        },
    )

    report = lint.scan(root, startup_cap=60)
    loaded = build_context_files_prompt(cwd=str(root), skip_soul=True)

    assert [entry["path"] for entry in report["startup_chain"]] == [
        ".cursor/rules/10-base.mdc",
        ".cursor/rules/20-extra.mdc",
    ]
    assert [failure["kind"] for failure in report["failures"]] == [
        "startup_chain_over_cap"
    ]
    assert "## .cursor/rules/10-base.mdc" in loaded
    assert "## .cursor/rules/20-extra.mdc" in loaded


def test_cli_exits_zero_and_emits_json_for_a_healthy_repository(tmp_path, capsys):
    root = _repo(tmp_path, {"AGENTS.md": "# Root\n"})

    exit_code = lint.main(["--json", str(root)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["failures"] == []
    assert payload["startup_cap"] == 40_000
    assert payload["nested_max"] == 8_000
    assert [entry["path"] for entry in payload["startup_chain"]] == ["AGENTS.md"]


def test_cli_exits_nonzero_when_a_budget_is_exceeded(tmp_path, capsys):
    root = _repo(tmp_path, {"AGENTS.md": "x" * 41_000})

    exit_code = lint.main([str(root)])

    assert exit_code == 1
    assert "AGENTS.md" in capsys.readouterr().err


def test_healthy_human_report_shows_shadow_and_nested_provenance(tmp_path, capsys):
    shared = "# Shared guide\n"
    root = _repo(
        tmp_path,
        {
            ".hermes.md": shared,
            "AGENTS.md": "# Shadowed guide\n",
            "apps/AGENTS.md": shared,
        },
    )

    exit_code = lint.main([str(root)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "shadows AGENTS.md" in captured.out
    assert "nested" in captured.out
    assert "apps/AGENTS.md" in captured.out
    assert "identical copy" not in captured.out
    assert captured.err == ""


def test_healthy_human_report_shows_nested_shadowing(tmp_path, capsys):
    root = _repo(
        tmp_path,
        {
            "AGENTS.md": "# Root\n",
            "apps/AGENTS.md": "# Apps guide\n",
            "apps/CLAUDE.md": "# Shadowed guide\n",
        },
    )

    exit_code = lint.main([str(root)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "apps/AGENTS.md" in captured.out
    assert "shadows apps/CLAUDE.md" in captured.out
    assert captured.err == ""


def test_healthy_human_report_is_silent_without_provenance(tmp_path, capsys):
    root = _repo(tmp_path, {"AGENTS.md": "# Root\n"})

    exit_code = lint.main([str(root)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_failures_describe_loader_truncation_markers(tmp_path):
    root = _repo(
        tmp_path,
        {
            "AGENTS.md": "r" * 41_000,
            "apps/AGENTS.md": "n" * 8_001,
        },
    )

    message = lint._format_failures(lint.scan(root))

    assert "preserves the head and tail with an explicit truncation marker" in message
    assert "preserves the first 8,000 characters and adds an explicit truncation marker" in message
    assert "silently dropped" not in message
    assert "never reach" not in message


def test_cli_honours_an_explicit_startup_cap(tmp_path, capsys):
    root = _repo(tmp_path, {"AGENTS.md": "x" * 30_000})

    exit_code = lint.main(["--json", "--startup-cap", "28000", str(root)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["failures"][0]["limit"] == 28_000


def test_constants_fall_back_to_pinned_literals_outside_a_hermes_checkout():
    def _no_hermes():
        raise ImportError("standalone CI checkout")

    constants = lint.resolve_constants(_no_hermes)

    assert constants["source"] == "fallback"
    assert constants["nested_max"] == 8_000
    assert constants["hint_filenames"][:2] == ("AGENTS.override.md", "AGENTS.md")
    assert "node_modules" in constants["excluded_dir_names"]


def test_constants_track_hermes_own_loader_when_importable():
    from agent import prompt_builder, subdirectory_hints

    constants = lint.resolve_constants()

    assert constants["source"] == "hermes"
    assert constants["nested_max"] == subdirectory_hints._MAX_HINT_CHARS
    assert constants["hint_filenames"] == tuple(subdirectory_hints._HINT_FILENAMES)
    assert constants["excluded_dir_names"] == frozenset(
        subdirectory_hints._EXCLUDED_DIR_NAMES
    )
    assert constants["hermes_md_names"] == tuple(prompt_builder._HERMES_MD_NAMES)


def test_module_defaults_match_the_live_hermes_constants():
    from agent import subdirectory_hints

    assert lint.DEFAULT_NESTED_MAX == subdirectory_hints._MAX_HINT_CHARS
    assert lint._HINT_FILENAMES == tuple(subdirectory_hints._HINT_FILENAMES)
    assert lint._EXCLUDED_DIR_NAMES == frozenset(
        subdirectory_hints._EXCLUDED_DIR_NAMES
    )


def test_repository_context_files_fit_the_shipped_budgets():
    repository_root = Path(__file__).resolve().parents[2]

    report = lint.scan(repository_root)

    assert report["ok"] is True, report["failures"]
