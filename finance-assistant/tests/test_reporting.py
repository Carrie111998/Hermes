import pytest

from app.main import build_parser
from app.reporting.service import ReportService

from test_analysis import make_service


def test_report_is_deterministic_and_contains_only_safe_aggregates(tmp_path):
    service = make_service(tmp_path)
    output = tmp_path / "reports"

    first = ReportService(service).generate("2026-08", output)
    csv_first = first.csv_path.read_text()
    html_first = first.html_path.read_text()
    second = ReportService(service).generate("2026-08", output)

    assert csv_first == second.csv_path.read_text()
    assert html_first == second.html_path.read_text()
    assert "UNKNOWN SHOP" not in csv_first + html_first
    assert "description" not in csv_first.lower() + html_first.lower()
    assert "card_identifier" not in csv_first + html_first
    assert "fingerprint" not in csv_first + html_first
    assert "2026-08,summary,total_spending,160.00" in csv_first
    assert "LEGACY_UNVERIFIED" not in csv_first


def test_report_output_directory_must_be_directory_and_is_created(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(ValueError, match="output directory"):
        ReportService(service).generate("2026-08", tmp_path / "report.csv")

    output = tmp_path / "nested" / "reports"
    result = ReportService(service).generate("2026-08", output)
    assert result.csv_path == output / "2026-08.csv"
    assert result.html_path == output / "2026-08.html"


def test_report_does_not_follow_symlink_targets(tmp_path):
    service = make_service(tmp_path)
    output = tmp_path / "reports"
    output.mkdir()
    target = tmp_path / "outside.csv"
    target.write_text("keep me", encoding="utf-8")
    (output / "2026-08.csv").symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        ReportService(service).generate("2026-08", output)
    assert target.read_text(encoding="utf-8") == "keep me"


def test_report_and_dashboard_cli_arguments_are_real_commands():
    parser = build_parser()
    report = parser.parse_args(["report", "--month", "2026-08", "--output-dir", "reports"])
    dashboard = parser.parse_args(["dashboard"])
    assert (report.command, report.month, report.output_dir) == ("report", "2026-08", "reports")
    assert dashboard.command == "dashboard"


def test_dashboard_rendering_does_not_mutate_analysis_service(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    import app.dashboard as dashboard

    class FakeColumn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        metric = lambda self, *args, **kwargs: None
        subheader = lambda self, *args, **kwargs: None

    monkeypatch.setattr(dashboard.st, "selectbox", lambda *args, **kwargs: "2026-08")
    monkeypatch.setattr(dashboard.st, "columns", lambda count: [FakeColumn() for _ in range(count)])
    for name in ("metric", "warning", "dataframe", "plotly_chart", "subheader", "title", "caption"):
        monkeypatch.setattr(dashboard.st, name, lambda *args, **kwargs: None)
    before = service.database.connection.execute("SELECT count(*) FROM merchant_rules").fetchone()[0]
    dashboard.render_dashboard(service)
    after = service.database.connection.execute("SELECT count(*) FROM merchant_rules").fetchone()[0]
    assert before == after
