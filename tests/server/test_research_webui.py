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
    # Phase 5 moved research configuration behind the admin guard: scoring weights,
    # enrichment and model profiles are operator machinery, not customer controls.
    assert "/admin/research" in main and "/admin/research/new" in main
    # The customer-facing surface keeps fit and evidence confidence distinct
    # (research-page-UI-guidelines.md §3.4), now rendered by the shared company card.
    components = client.get("/js/pages/_components.js").text
    assert "fit_score" in components and "evidence_confidence" in components
    assert "based on verified sources" in components and "partly estimated" in components


def test_production_webui_has_no_mock_runtime():
    _, client, _, _ = make_client()
    for path in ("/js/main.js", "/js/api.js", "/js/shell.js", "/js/real-state.js"):
        text = client.get(path).text
        assert "./mocks/" not in text
        assert "config.mode" not in text
    assert client.get("/js/mocks/handlers.js").status_code == 404


def test_source_picker_is_catalog_driven_and_admin_copy_is_distinct():
    _, client, _, _ = make_client()
    picker = client.get("/js/pages/research-source-picker.js").text
    assert "un-comtrade" not in picker
    assert "companies-house" not in picker
    admin = client.get("/js/pages/admin.js").text
    assert "Stops future collection" in admin
    assert "Historical evidence" in admin
    assert "recalculates affected leads" in admin
