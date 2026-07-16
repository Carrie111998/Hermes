from tests.server.test_api_mvp import make_client


def test_research_modules_routes_and_evidence_copy_are_served():
    _, client, _, _ = make_client()
    for path in (
        "/js/pages/research.js", "/js/pages/research-editor.js", "/js/pages/research-detail.js",
        "/js/pages/research-source-picker.js", "/js/pages/research-scoring.js",
        "/js/pages/research-enrichment.js", "/js/pages/research-evidence.js", "/js/research-state.js",
    ):
        assert client.get(path).status_code == 200, path
    main = client.get("/js/main.js").text
    assert "/app/research" in main and "/app/research/new" in main
    leads = client.get("/js/pages/leads.js").text
    assert "Fit score" in leads and "Evidence confidence" in leads


def test_source_picker_is_catalog_driven_and_admin_copy_is_distinct():
    _, client, _, _ = make_client()
    picker = client.get("/js/pages/research-source-picker.js").text
    assert "un-comtrade" not in picker
    assert "companies-house" not in picker
    admin = client.get("/js/pages/admin.js").text
    assert "Stops future collection" in admin
    assert "Historical evidence" in admin
    assert "recalculates affected leads" in admin
