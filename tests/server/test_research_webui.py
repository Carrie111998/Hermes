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


def test_customer_research_results_workspace_is_served_and_routed():
    _, client, _, _ = make_client()
    results = client.get("/js/pages/research-results.js")
    assert results.status_code == 200

    main = client.get("/js/main.js").text
    shell = client.get("/js/shell.js").text
    api = client.get("/js/api.js").text
    source = results.text

    assert "import * as researchResults from './pages/research-results.js'" in main
    assert "{ path: '/app/research'" in main
    assert "{ path: '/app/buyers'," in main and "to: () => '/app/research'" in main
    assert "path: '/app/research', label: 'Research'" in shell
    assert "researchCampaigns.results" in api
    assert "researchResults.claims" in api
    assert all(label in source for label in (
        "Active", "Rejected", "Fit", "Confidence", "Country", "Buyer role", "Sources",
        "Why this verdict", "Supporting claims", "Conflicting claims", "Missing evidence",
        "Snapshot", "SHA-256",
    ))
    assert "outreach" not in source.casefold()


def test_production_webui_has_no_mock_runtime():
    _, client, _, _ = make_client()
    for path in ("/js/main.js", "/js/api.js", "/js/shell.js", "/js/real-state.js"):
        text = client.get(path).text
        assert "./mocks/" not in text
        assert "config.mode" not in text
    assert "mode: 'mock'" not in client.get("/js/api.js").text
    assert client.get("/js/mocks/handlers.js").status_code == 404
    assert client.get("/js/mocks/seed.js").status_code == 404


def test_served_index_does_not_advertise_mock_mode():
    _, client, _, _ = make_client()
    index = client.get("/")
    assert index.status_code == 200
    assert "mock mode" not in index.text.casefold()


def test_source_picker_is_catalog_driven_and_admin_copy_is_distinct():
    _, client, _, _ = make_client()
    picker = client.get("/js/pages/research-source-picker.js").text
    assert "un-comtrade" not in picker
    assert "companies-house" not in picker
    admin = client.get("/js/pages/admin.js").text
    assert "Stops future collection" in admin
    assert "Historical evidence" in admin
    assert "recalculates affected leads" in admin
