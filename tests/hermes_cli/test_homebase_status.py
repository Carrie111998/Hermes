from hermes_cli import homebase_status as status


def test_dashboard_renders_all_requested_sections(monkeypatch, tmp_path):
    monkeypatch.setattr(status, "HERMES_HOME", tmp_path)
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "fresh.zip").write_bytes(b"zip")
    monkeypatch.setattr(status, "_service_state", lambda *args, **kwargs: "✅ active")
    monkeypatch.setattr(status, "_ups", lambda: "✅ OL; battery 100%")
    monkeypatch.setattr(status, "_disk", lambda: "✅ /: 10 GiB free")
    monkeypatch.setattr(status, "_thermal", lambda: "✅ 45.0°C; throttling 0x0")
    monkeypatch.setattr(status, "_mainframe", lambda: "✅ reachable")
    monkeypatch.setattr(status, "_maintenance", lambda: "✅ success")

    report = status.format_homebase_status()

    for heading in ("Gateway:", "UPS:", "Disk:", "Thermal:", "Backup:", "Mainframe:", "Last maintenance:"):
        assert heading in report
