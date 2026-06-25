from __future__ import annotations


def test_doctor_summary_does_not_call_warnings_a_clean_pass(capsys):
    from hermes_cli.doctor import _print_doctor_summary

    _print_doctor_summary(
        should_fix=False,
        fixed_count=0,
        remaining_issues=[],
        warning_count=2,
    )

    output = capsys.readouterr().out
    assert "Checks completed with 2 warning(s)." in output
    assert "No blocking issues found" in output
    assert "All checks passed" not in output


def test_doctor_summary_keeps_clean_pass_when_no_warnings_or_issues(capsys):
    from hermes_cli.doctor import _print_doctor_summary

    _print_doctor_summary(
        should_fix=False,
        fixed_count=0,
        remaining_issues=[],
        warning_count=0,
    )

    output = capsys.readouterr().out
    assert "All checks passed" in output


def test_check_warn_increments_doctor_warning_count(capsys):
    from hermes_cli import doctor

    doctor._DOCTOR_WARNING_COUNT = 0
    doctor.check_warn("example warning")

    assert doctor._DOCTOR_WARNING_COUNT == 1
    assert "example warning" in capsys.readouterr().out


def test_disabled_platform_id_filters_namespaced_toolset_warning():
    from hermes_cli.doctor import _filter_doctor_disabled_toolsets

    unavailable = [
        {"name": "hermes-yuanbao", "tools": ["yb_send_dm"]},
        {"name": "other-tool", "tools": []},
    ]

    assert _filter_doctor_disabled_toolsets(unavailable, {"yuanbao"}) == [
        {"name": "other-tool", "tools": []},
    ]
