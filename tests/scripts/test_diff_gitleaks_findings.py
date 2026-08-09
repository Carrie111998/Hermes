"""Tests for scripts/ci/diff_gitleaks_findings.py.

The script decides which gitleaks findings a change *introduced*. Its key
choice — (rule, secret) rather than (file, rule, line) — is what makes an
in-place credential replacement visible, so it is pinned here.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "diff_gitleaks_findings.py"


def _load():
    spec = importlib.util.spec_from_file_location("diff_gitleaks_findings", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


def _finding(secret: str, *, rule: str = "anthropic-api-key", file: str = "a.py", line: int = 1):
    return {"RuleID": rule, "Secret": secret, "File": file, "StartLine": line}


def _write(tmp_path: Path, name: str, findings) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(findings), encoding="utf-8")
    return path


def _run(tmp_path, base, head) -> int:
    return mod.main([str(_write(tmp_path, "b.json", base)), str(_write(tmp_path, "h.json", head))])


def test_unchanged_secret_is_not_new(tmp_path):
    f = [_finding("AAA")]
    assert _run(tmp_path, f, f) == 0


def test_replaced_secret_at_the_same_location_is_new(tmp_path):
    """The case gitleaks' own --baseline-path misses on an entropy collision."""
    assert _run(tmp_path, [_finding("AAA")], [_finding("BBB")]) == 1


def test_moved_secret_is_not_new(tmp_path):
    """A line shift or a rename is not an introduction."""
    base = [_finding("AAA", file="a.py", line=1)]
    head = [_finding("AAA", file="b.py", line=99)]
    assert _run(tmp_path, base, head) == 0


def test_same_value_under_a_different_rule_is_new(tmp_path):
    assert _run(tmp_path, [_finding("AAA", rule="r1")], [_finding("AAA", rule="r2")]) == 1


def test_added_secret_alongside_an_existing_one(tmp_path):
    assert _run(tmp_path, [_finding("AAA")], [_finding("AAA"), _finding("BBB")]) == 1


def test_a_second_copy_of_an_existing_secret_is_new(tmp_path):
    """Occurrences are counted, not merely matched.

    A resolution that duplicates a credential already in the changed files
    leaves the (rule, secret) key unchanged, so plain set membership would
    exempt both copies and report nothing.
    """
    base = [_finding("AAA", line=1)]
    head = [_finding("AAA", line=1), _finding("AAA", line=2)]
    assert _run(tmp_path, base, head) == 1


def test_matching_counts_are_all_exempt(tmp_path):
    two = [_finding("AAA", line=1), _finding("AAA", line=2)]
    assert _run(tmp_path, two, two) == 0


def test_only_the_surplus_is_reported(tmp_path, capsys):
    base = [_finding("AAA"), _finding("AAA")]
    head = [_finding("AAA"), _finding("AAA"), _finding("AAA")]
    assert _run(tmp_path, base, head) == 1
    assert capsys.readouterr().out.count("secret introduced") == 1


def test_removed_secret_is_not_a_failure(tmp_path):
    assert _run(tmp_path, [_finding("AAA"), _finding("BBB")], [_finding("AAA")]) == 0


@pytest.mark.parametrize("empty", ["", "[]", "null"])
def test_empty_reports(tmp_path, empty):
    base = tmp_path / "b.json"
    head = tmp_path / "h.json"
    base.write_text(empty, encoding="utf-8")
    head.write_text(empty, encoding="utf-8")
    assert mod.main([str(base), str(head)]) == 0


def test_unparseable_report_fails_closed(tmp_path):
    """A broken report must not read as 'nothing found'."""
    base = tmp_path / "b.json"
    base.write_text("not json", encoding="utf-8")
    head = _write(tmp_path, "h.json", [])
    with pytest.raises(SystemExit):
        mod.main([str(base), str(head)])


def test_non_list_report_fails_closed(tmp_path):
    base = tmp_path / "b.json"
    base.write_text('{"findings": []}', encoding="utf-8")
    head = _write(tmp_path, "h.json", [])
    with pytest.raises(SystemExit):
        mod.main([str(base), str(head)])


class TestExtraBaseReports:
    """Deleted files are scanned into their own tree, so they arrive separately.

    A file and a directory cannot share a name, so materializing a deleted
    blob alongside the added/modified ones breaks an ordinary file-to-package
    refactor. The deleted tree is scanned on its own; its findings are still
    base-side findings and must exempt the same way.
    """

    def test_a_value_only_in_the_extra_base_is_exempt(self, tmp_path):
        base = _write(tmp_path, "b.json", [])
        extra = _write(tmp_path, "d.json", [_finding("AAA", file="old.py")])
        head = _write(tmp_path, "h.json", [_finding("AAA", file="new.py")])
        assert mod.main([str(base), str(head), "--extra-base", str(extra)]) == 0

    def test_without_the_extra_base_the_same_value_is_new(self, tmp_path):
        """The control: it is the extra report doing the work, not the key."""
        base = _write(tmp_path, "b.json", [])
        head = _write(tmp_path, "h.json", [_finding("AAA", file="new.py")])
        assert mod.main([str(base), str(head)]) == 1

    def test_several_extra_bases_are_all_merged(self, tmp_path):
        base = _write(tmp_path, "b.json", [])
        e1 = _write(tmp_path, "d1.json", [_finding("AAA")])
        e2 = _write(tmp_path, "d2.json", [_finding("BBB")])
        head = _write(tmp_path, "h.json", [_finding("AAA"), _finding("BBB")])
        args = [str(base), str(head), "--extra-base", str(e1), "--extra-base", str(e2)]
        assert mod.main(args) == 0

    def test_an_unparseable_extra_base_fails_closed(self, tmp_path):
        base = _write(tmp_path, "b.json", [])
        extra = tmp_path / "d.json"
        extra.write_text("not json", encoding="utf-8")
        head = _write(tmp_path, "h.json", [])
        with pytest.raises(SystemExit):
            mod.main([str(base), str(head), "--extra-base", str(extra)])


def test_secret_values_are_never_printed(tmp_path, capsys):
    secret = "sk-ant-api03-THIS-MUST-NOT-APPEAR"
    _run(tmp_path, [], [_finding(secret)])
    out = capsys.readouterr()
    assert secret not in out.out
    assert secret not in out.err
    assert "a.py" in out.out and "anthropic-api-key" in out.out


class TestSeveralBaseSideReports:
    """The base side arrives in several reports, and they are not tallies.

    The target tip, the deleted-file tree and the branch's previous head all
    describe base-side state. Summing them exempts a value twice that two
    parents each hold once; taking the maximum drops an occurrence the two
    parents hold at *different* paths. Both were shipped in turn on this PR.
    Unioning over (rule, secret, file, line) does both jobs.
    """

    def test_a_line_shift_between_parents_is_not_a_second_occurrence(self, tmp_path):
        """Reconciliation is per file, not per line.

        The same inherited credential routinely sits at different lines in the
        target tip and the branch's previous head. Counting those as two
        occurrences inflated the exemption enough for a merge resolution to
        slip a genuinely new copy past.
        """
        base = _write(tmp_path, "b.json", [_finding("AAA", file="a.py", line=2)])
        prev = _write(tmp_path, "p.json", [_finding("AAA", file="a.py", line=3)])
        head = _write(
            tmp_path,
            "h.json",
            [_finding("AAA", file="a.py", line=3), _finding("AAA", file="a.py", line=9)],
        )
        assert mod.main([str(base), str(head), "--extra-base", str(prev)]) == 1

    def test_the_same_occurrence_in_two_parents_counts_once(self, tmp_path):
        """Summing would exempt two copies; a merge duplicating one must fail."""
        base = _write(tmp_path, "b.json", [_finding("AAA", file="a.py", line=1)])
        prev = _write(tmp_path, "p.json", [_finding("AAA", file="a.py", line=1)])
        head = _write(
            tmp_path,
            "h.json",
            [_finding("AAA", file="a.py", line=1), _finding("AAA", file="a.py", line=9)],
        )
        assert mod.main([str(base), str(head), "--extra-base", str(prev)]) == 1

    def test_disjoint_occurrences_from_two_parents_both_count(self, tmp_path):
        """Maximum would drop one; a clean merge inheriting both must pass."""
        base = _write(tmp_path, "b.json", [_finding("AAA", file="target_only.py")])
        prev = _write(tmp_path, "p.json", [_finding("AAA", file="branch_only.py")])
        head = _write(
            tmp_path,
            "h.json",
            [_finding("AAA", file="target_only.py"), _finding("AAA", file="branch_only.py")],
        )
        assert mod.main([str(base), str(head), "--extra-base", str(prev)]) == 0

    def test_a_third_copy_beyond_both_parents_is_new(self, tmp_path):
        base = _write(tmp_path, "b.json", [_finding("AAA", file="a.py", line=1)])
        prev = _write(tmp_path, "p.json", [_finding("AAA", file="b.py", line=1)])
        head = _write(
            tmp_path,
            "h.json",
            [
                _finding("AAA", file="a.py", line=1),
                _finding("AAA", file="b.py", line=1),
                _finding("AAA", file="c.py", line=1),
            ],
        )
        assert mod.main([str(base), str(head), "--extra-base", str(prev)]) == 1

    def test_two_copies_in_one_parent_still_both_exempt(self, tmp_path):
        """Multiplicity within a single snapshot is preserved by location."""
        two = [_finding("AAA", file="a.py", line=1), _finding("AAA", file="a.py", line=2)]
        assert _run(tmp_path, two, two) == 0

    def test_a_value_only_a_secondary_report_has_is_exempt(self, tmp_path):
        base = _write(tmp_path, "b.json", [])
        extra = _write(tmp_path, "d.json", [_finding("AAA", file="old.py")])
        head = _write(tmp_path, "h.json", [_finding("AAA", file="new.py")])
        assert mod.main([str(base), str(head), "--extra-base", str(extra)]) == 0

    def test_the_alt_base_spelling_is_accepted(self, tmp_path):
        """The workflow and older invocations use --alt-base; same behaviour."""
        base = _write(tmp_path, "b.json", [_finding("AAA", file="a.py")])
        prev = _write(tmp_path, "p.json", [_finding("AAA", file="b.py")])
        head = _write(
            tmp_path, "h.json", [_finding("AAA", file="a.py"), _finding("AAA", file="b.py")]
        )
        assert mod.main([str(base), str(head), "--alt-base", str(prev)]) == 0

    def test_an_unparseable_secondary_report_fails_closed(self, tmp_path):
        base = _write(tmp_path, "b.json", [])
        extra = tmp_path / "d.json"
        extra.write_text("not json", encoding="utf-8")
        head = _write(tmp_path, "h.json", [])
        with pytest.raises(SystemExit):
            mod.main([str(base), str(head), "--extra-base", str(extra)])
