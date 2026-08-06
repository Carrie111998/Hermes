from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from scripts.check_agents_context import (
    MAX_ROOT_CHARS,
    REQUIRED_REFERENCES,
    extract_navigable_links,
    validate_repository,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _write(repo: Path, relative: str, content: str = "# Reference\n") -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _contract_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for relative in REQUIRED_REFERENCES:
        _write(repo, relative)
    links = "\n".join(f"[{relative}]({relative})" for relative in REQUIRED_REFERENCES)
    _write(repo, "AGENTS.md", f"# Guide\n\n{links}\n")
    _write(repo, "apps/desktop/AGENTS.md", "# Desktop\n\n[Design](./DESIGN.md)\n")
    _git(repo, "add", ".")
    return repo


def _root_at_size(repo: Path, size: int) -> None:
    path = repo / "AGENTS.md"
    current = path.read_text(encoding="utf-8")
    assert len(current) <= size
    path.write_text(current + "x" * (size - len(current)), encoding="utf-8")


def test_contract_accepts_exact_root_character_limit_and_resolved_links(tmp_path):
    repo = _contract_repo(tmp_path)
    _root_at_size(repo, MAX_ROOT_CHARS)
    _git(repo, "add", "AGENTS.md")

    assert validate_repository(repo) == []


def test_contract_rejects_one_character_above_root_limit(tmp_path):
    repo = _contract_repo(tmp_path)
    _root_at_size(repo, MAX_ROOT_CHARS + 1)

    errors = validate_repository(repo)

    assert any("18,001" in error and "18,000" in error for error in errors)


@pytest.mark.parametrize(
    "replacement",
    [
        "![target](target.md)",
        "![nested [target](target.md)](image.png)",
        "`[target](target.md)`",
        "``[target](target.md)``",
        r"\[target](target.md)",
        "<!-- [target](target.md) -->",
        "    [target](target.md)",
        "<pre>\n[target](target.md)\n</pre>",
        "<code>\n[target](target.md)\n</code>",
        "```markdown\n[target](target.md)\n```",
        "````markdown\n```\n[target](target.md)\n```\n````",
        "~~~~markdown\n[target](target.md)\n~~~~",
    ],
    ids=[
        "image",
        "nested-image-label",
        "inline-code",
        "long-inline-code",
        "escaped",
        "comment",
        "indented-code",
        "html-pre",
        "html-code",
        "fence",
        "long-fence",
        "tilde-fence",
    ],
)
def test_required_reference_must_be_an_ordinary_navigable_link(tmp_path, replacement):
    repo = _contract_repo(tmp_path)
    _write(repo, "target.md")
    _write(repo, "AGENTS.md", f"# Guide\n\n{replacement}\n")
    _git(repo, "add", ".")

    errors = validate_repository(repo, required_references=("target.md",))

    assert any("required navigable link is missing: target.md" in error for error in errors)


def test_link_extractor_ignores_non_links_and_keeps_ordinary_links():
    markdown = """
[ordinary](docs/guide.md#part)
![image](image.png)
`[inline](inline.md)`
\[escaped](escaped.md)
<!-- [comment](comment.md) -->
```md
[fenced](fenced.md)
```
[external](https://example.com/x)
[anchor](#local)
"""

    assert extract_navigable_links(markdown, source="AGENTS.md") == [
        "docs/guide.md#part",
        "https://example.com/x",
        "#local",
    ]


def test_invalid_fence_closer_does_not_expose_links_from_fenced_code():
    markdown = """```
``` trailing text is not a closer
[hidden](hidden.md)
```
[ordinary](ordinary.md)
"""

    assert extract_navigable_links(markdown, source="AGENTS.md") == ["ordinary.md"]


def test_fence_markers_inside_raw_html_code_do_not_change_parser_state():
    markdown = """<pre>
```
[hidden](hidden.md)
</pre>
[ordinary](ordinary.md)
"""

    assert extract_navigable_links(markdown, source="AGENTS.md") == ["ordinary.md"]


@pytest.mark.parametrize("delimiter_length", [1, 2, 3])
def test_inline_code_closes_only_on_an_exact_backtick_run(delimiter_length):
    delimiter = "`" * delimiter_length
    longer_length = 3 if delimiter_length == 1 else delimiter_length + 1
    longer_run = "`" * longer_length
    leftover_run = "`" * (longer_length - delimiter_length)
    markdown = (
        f"prose {delimiter}opener {longer_run}internal{leftover_run}"
        f"[hidden](hidden.md) closer{delimiter}\n[ordinary](ordinary.md)\n"
    )

    assert extract_navigable_links(markdown, source="AGENTS.md") == ["ordinary.md"]


@pytest.mark.parametrize("delimiter_length", [1, 2, 3])
def test_inline_code_exact_closer_is_not_escaped_inside_span(delimiter_length):
    delimiter = "`" * delimiter_length
    markdown = (
        f"prose {delimiter}code\\{delimiter}\n[ordinary](ordinary.md)\n"
    )

    assert extract_navigable_links(markdown, source="AGENTS.md") == ["ordinary.md"]


def test_relative_link_query_and_fragment_are_stripped_for_resolution(tmp_path):
    repo = _contract_repo(tmp_path)
    _write(repo, "extra.md")
    with (repo / "AGENTS.md").open("a", encoding="utf-8") as handle:
        handle.write("\n[extra](extra.md?plain=1#section)\n")
    _git(repo, "add", ".")

    assert validate_repository(repo) == []


def test_untracked_oversized_agents_fixture_is_outside_repository_contract(tmp_path):
    repo = _contract_repo(tmp_path)
    _write(repo, "fixtures/AGENTS.md", "x" * (MAX_ROOT_CHARS + 1))

    assert validate_repository(repo) == []


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("missing.md", "does not resolve to a tracked regular file"),
        ("untracked.md", "does not resolve to a tracked regular file"),
        ("../outside.md", "escapes repository root"),
        ("docs", "does not resolve to a tracked regular file"),
    ],
)
def test_contract_rejects_invalid_relative_links(tmp_path, target, expected):
    repo = _contract_repo(tmp_path)
    if target == "untracked.md":
        _write(repo, target)
    with (repo / "AGENTS.md").open("a", encoding="utf-8") as handle:
        handle.write(f"\n[bad]({target})\n")

    errors = validate_repository(repo)

    assert any(expected in error and target in error for error in errors)


@pytest.mark.parametrize("target", ["http://[", "//["])
def test_contract_reports_malformed_url_destinations_without_raising(tmp_path, target):
    repo = _contract_repo(tmp_path)
    with (repo / "AGENTS.md").open("a", encoding="utf-8") as handle:
        handle.write(f"\n[bad]({target})\n")

    errors = validate_repository(repo)

    assert f"AGENTS.md link {target!r} has malformed URL destination" in errors


def test_contract_requires_root_and_desktop_governing_files(tmp_path):
    repo = _contract_repo(tmp_path)
    (repo / "AGENTS.md").unlink()
    (repo / "apps/desktop/AGENTS.md").unlink()

    errors = validate_repository(repo)

    assert any("required governing file is missing: AGENTS.md" in error for error in errors)
    assert any("required governing file is missing: apps/desktop/AGENTS.md" in error for error in errors)


def test_contract_fails_closed_on_non_repository_and_unreadable_utf8(tmp_path):
    repo = _contract_repo(tmp_path)
    (repo / "AGENTS.md").write_bytes(b"\xff")
    read_errors = validate_repository(repo)
    not_repo_errors = validate_repository(tmp_path / "not-a-repo")

    assert any("could not read AGENTS.md" in error for error in read_errors)
    assert any("could not enumerate tracked files" in error for error in not_repo_errors)


@pytest.mark.parametrize("content", ["```\n[link](target.md)\n", "<!-- [link](target.md)\n"])
def test_link_extractor_fails_closed_on_unterminated_exclusions(content):
    with pytest.raises(ValueError, match="unterminated"):
        extract_navigable_links(content, source="AGENTS.md")


def test_repository_agents_context_contract():
    repo_root = Path(__file__).resolve().parents[2]

    assert validate_repository(repo_root) == []


def test_documented_test_isolation_preserves_real_home_and_redirects_hermes_home():
    repo_root = Path(__file__).resolve().parents[2]
    root_contract = " ".join(
        (repo_root / "AGENTS.md").read_text(encoding="utf-8").split()
    )
    testing_reference = " ".join(
        (repo_root / "docs/development/testing.md")
        .read_text(encoding="utf-8")
        .split()
    )

    assert "real `HOME` remains stable" in root_contract
    assert "Direct `Path.home() / \".hermes\"` access is a bug" in root_contract
    assert "hermetic HOME" not in root_contract
    assert "Real home (stable); profile state redirected by `HERMES_HOME`" in testing_reference
    assert "does **not** redirect `HOME`" in testing_reference


def test_configuration_reference_names_config_data_definitions_and_reexports():
    from hermes_cli import config, config_defaults

    repo_root = Path(__file__).resolve().parents[2]
    reference = " ".join(
        (repo_root / "docs/development/configuration.md")
        .read_text(encoding="utf-8")
        .split()
    )

    assert (
        "Both `DEFAULT_CONFIG` and `OPTIONAL_ENV_VARS` are defined in "
        "`hermes_cli/config_defaults.py` and re-exported from "
        "`hermes_cli/config.py`"
    ) in reference
    assert "`_EXTRA_KNOWN_ROOT_KEYS` and `read_user_config_raw()`" in reference
    assert config.DEFAULT_CONFIG is config_defaults.DEFAULT_CONFIG
    assert config.OPTIONAL_ENV_VARS is config_defaults.OPTIONAL_ENV_VARS


def test_plugin_reference_uses_noncontradictory_discovery_taxonomy():
    repo_root = Path(__file__).resolve().parents[2]
    reference = " ".join(
        (repo_root / "docs/development/plugins.md")
        .read_text(encoding="utf-8")
        .split()
    )

    assert "three primary discovery systems" in reference
    assert "two plugin surfaces" not in reference
    assert "General plugins (`hermes_cli/plugins.py`" in reference
    assert "Memory-provider plugins (`plugins/memory/<name>/`)" in reference
    assert "Model-provider plugins (`plugins/model-providers/<name>/`)" in reference
    assert "Additional provider families" in reference


def test_kanban_reference_matches_non_success_outcomes_counted_by_dispatcher():
    from hermes_cli.kanban_db import _record_task_failure

    repo_root = Path(__file__).resolve().parents[2]
    reference = " ".join(
        (
            repo_root
            / "skills/autonomous-ai-agents/hermes-agent/references/background-systems.md"
        )
        .read_text(encoding="utf-8")
        .split()
    )
    runtime_contract = inspect.getdoc(_record_task_failure) or ""

    assert "consecutive non-success outcomes" in reference
    assert "consecutive spawn failures" not in reference
    for outcome in ("spawn_failed", "crashed", "timed_out"):
        assert outcome in reference
        assert outcome in runtime_contract


def test_curator_reference_documents_builtin_pruning_safeguards():
    repo_root = Path(__file__).resolve().parents[2]
    reference = (
        repo_root
        / "skills/autonomous-ai-agents/hermes-agent/references/background-systems.md"
    ).read_text(encoding="utf-8")

    assert "`curator.prune_builtins: true` (the default)" in reference
    assert "protected, pinned, or referenced by cron jobs" in reference
