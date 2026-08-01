"""Web UI serving and chat-bridge checks for server/WEBUI_CONNECTION_PRD.md."""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from server.agent_service import StubRunExecutor
from server.app import create_app
from server.config import Settings
from server.db import json_dump, new_id, now

# See tests/server/test_api_mvp.py: outbound email requires a signed opt-out link.
TEST_CREDENTIAL_KEY = "KJ9KmdJiLL6itiwlEGTvGQ4ptS4dnd1ZZPyRPTwmjs4="


def make_client(chat_agent_factory=None, **overrides):
    root = Path(tempfile.mkdtemp(prefix="interfaze-webui-test-"))
    settings = Settings(
        database_path=root / "test.db",
        upload_dir=root / "uploads",
        bootstrap_admin_email="admin@example.test",
        bootstrap_admin_password="correct-horse-battery",
        **{"credential_key": TEST_CREDENTIAL_KEY, **overrides},
    )
    app = create_app(settings, run_executor=StubRunExecutor(),
                     chat_agent_factory=chat_agent_factory)
    return app, TestClient(app)


def test_index_serves_spa_with_placeholders_substituted():
    _, client = make_client()
    res = client.get("/")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/html")
    assert res.headers["cache-control"] == "no-store"
    assert "interfaze-agent" in res.text
    assert "__MAX_UPLOAD_BYTES__" not in res.text
    assert "__CSRF_TOKEN_JSON__" not in res.text
    assert "__CHAT_ENABLED__" not in res.text
    # default 25 MB limit and a neutralized CSRF token (Bearer-only auth)
    assert "maxUploadBytes:26214400" in res.text
    assert 'csrfToken:""' in res.text
    assert "chatEnabled:true" in res.text
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]


def test_index_respects_max_upload_bytes_setting():
    _, client = make_client(max_upload_bytes=1024)
    assert "maxUploadBytes:1024" in client.get("/").text


def test_static_assets_resolve_from_relative_hrefs():
    # index.html references ./js/, ./css/, ./assets/ — with webui/ as the web
    # root these must resolve without any path remapping.
    _, client = make_client()
    for path in (
        "/js/main.js",
        "/js/adapters.js",
        "/js/oauth-popup.js",
        "/css/app.css",
        "/assets/world.svg",
    ):
        res = client.get(path)
        assert res.status_code == 200, path


def test_api_routes_win_over_static_mount():
    _, client = make_client()
    assert client.get("/health").json()["service"] == "interfaze-agent"
    # API auth answers, not the static catch-all
    assert client.get("/api/v1/company/profile").status_code in (401, 403)
    # everything unclaimed falls through to the mount and 404s
    assert client.get("/no-such-page").status_code == 404


def test_webui_can_be_disabled():
    _, client = make_client(webui_enabled=False)
    assert client.get("/").status_code == 404
    assert client.get("/health").status_code == 200


def test_chat_bridge_can_be_disabled():
    _, client = make_client(chat_enabled=False)
    assert "chatEnabled:false" in client.get("/").text
    assert client.get("/health").json()["chat_enabled"] is False
    # The route is absent; the last-registered static mount may answer POST
    # with 405 instead of its GET-style 404, but no chat handler is reachable.
    assert client.post("/api/session/new", json={"profile": "default"}).status_code in (404, 405)
    assert "/api/session/new" not in client.app.openapi()["paths"]


def test_company_profile_round_trip_with_data_envelope():
    # Phase 1 exit criterion: PATCH round-trips with the DataPatch envelope
    # (what webui/js/adapters.js sends) and hard-422s on the flat body
    # (what the UI would send without the adapter).
    _, client = make_client()
    login = client.post("/api/v1/auth/login", json={
        "email": "admin@example.test", "password": "correct-horse-battery",
    })
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    company = client.post("/api/v1/admin/companies", headers=headers, json={"name": "Acme"})
    assert company.status_code == 201, company.text
    headers["X-Company-ID"] = company.json()["id"]

    patched = client.patch("/api/v1/company/profile", headers=headers,
                           json={"data": {"name": "Acme", "website": "https://acme.test"}})
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["website"] == "https://acme.test"

    flat = client.patch("/api/v1/company/profile", headers=headers, json={"name": "Acme"})
    assert flat.status_code == 422


def test_phase2_core_data_flow_with_stub_executor():
    """Phase 2 exit path: onboarding -> lead -> research/contact -> outreach -> run events."""
    app, client = make_client()
    login = client.post("/api/v1/auth/login", json={
        "email": "admin@example.test", "password": "correct-horse-battery",
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    company = client.post("/api/v1/admin/companies", headers=headers, json={"name": "Silverline"})
    headers["X-Company-ID"] = company.json()["id"]

    def wait_for_run(run_id: str):
        deadline = time.time() + 4
        while time.time() < deadline:
            run = client.get(f"/api/v1/agent-runs/{run_id}", headers=headers).json()
            if run["status"] in {"succeeded", "failed", "cancelled"}:
                return run
            time.sleep(0.01)
        raise AssertionError(f"run {run_id} did not finish")

    assert client.post("/api/v1/onboarding/start", headers=headers).status_code == 200
    steps = {
        "company-identity": {"name": "Silverline", "headquarters_country": "TR"},
        "positioning": {"main_value_proposition": "Export-ready kitchen appliances"},
        "products": {"catalog_confirmed": True},
        "internal-sales-data": {"sources_reviewed": True},
        "target-markets": {"target_markets": ["DE", "AE", "SA", "NL", "GB"]},
    }
    for step, data in steps.items():
        response = client.patch(f"/api/v1/onboarding/{step}", headers=headers, json={"data": data})
        assert response.status_code == 200, response.text
    completed = client.post("/api/v1/onboarding/complete", headers=headers)
    assert completed.status_code == 200 and completed.json()["status"] == "completed"

    selected = client.post("/api/v1/lead-map/selected-countries", headers=headers,
                           json={"countries": ["DE", "AE", "SA", "NL", "GB"]})
    assert selected.status_code == 200 and len(selected.json()) == 5
    scan = client.post("/api/v1/lead-scans", headers=headers, json={
        "countries": ["DE"], "product_ids": [], "industries": ["Appliance distributor"],
        "max_leads_per_country": 8, "scan_depth": "standard",
        "data_sources": ["web_search", "trade_data"],
    })
    assert scan.status_code == 201, scan.text
    scan_run = client.post(f"/api/v1/lead-scans/{scan.json()['id']}/start", headers=headers)
    assert wait_for_run(scan_run.json()["id"])["status"] == "succeeded"

    lead = client.post("/api/v1/leads", headers=headers, json={
        "company_name": "Kuechen Partner GmbH", "website": "https://buyer.example",
        "country": "DE", "city": "Berlin", "industry": "Appliance distributor",
    })
    assert lead.status_code == 201, lead.text
    contact = client.post("/api/v1/contacts", headers=headers, json={
        "lead_id": lead.json()["id"], "email": "buyer@example.com",
        "data": {"full_name": "Anna Mueller", "title": "Purchasing Manager"},
    })
    assert contact.status_code == 201 and contact.json()["data"]["full_name"] == "Anna Mueller"

    research = client.post(f"/api/v1/leads/{lead.json()['id']}/research", headers=headers)
    research_run = wait_for_run(research.json()["id"])
    assert research_run["status"] == "succeeded"
    insights = client.get(f"/api/v1/research/lead/{lead.json()['id']}/insights", headers=headers)
    assert insights.status_code == 200 and "score_inputs" in insights.json()

    generated = client.post(f"/api/v1/leads/{lead.json()['id']}/generate-outreach", headers=headers)
    generation_run = wait_for_run(generated.json()["id"])
    assert generation_run["status"] == "succeeded" and generation_run["output_ref"]
    message_id = generation_run["output_ref"]
    message = client.get(f"/api/v1/outreach/messages/{message_id}", headers=headers)
    assert message.status_code == 200 and message.json()["content"]["to"] == "buyer@example.com"
    assert client.post(f"/api/v1/outreach/messages/{message_id}/approve", headers=headers).status_code == 200

    stamp = now()
    app.state.db.execute("INSERT INTO integrations VALUES(?,?,?,?,?,?,?,?,?)", (
        new_id("int"), company.json()["id"], "email", "stub", "connected", None,
        json_dump({}), stamp, stamp,
    ))
    draft = client.post(f"/api/v1/outreach/messages/{message_id}/create-draft", headers=headers)
    assert draft.status_code == 200 and draft.json()["status"] == "draft"

    runs = client.get("/api/v1/agent-runs", headers=headers)
    assert runs.status_code == 200 and len(runs.json()) >= 4
    events = client.get(f"/api/v1/agent-runs/{generation_run['id']}/events", headers=headers)
    assert events.status_code == 200 and {event["kind"] for event in events.json()} >= {"created", "started", "succeeded"}


class FakeChatAgent:
    def __init__(self, factory, kwargs):
        self.factory = factory
        self.kwargs = kwargs
        self.history = None

    def run_conversation(self, message, conversation_history=None):
        self.history = conversation_history or []
        if self.factory.gate is not None:
            assert self.factory.gate.wait(timeout=3), "fake chat gate was not released"
        callback = self.kwargs["stream_delta_callback"]
        callback("Hello ")
        callback("from Hermes.")
        callback(None)
        return {
            "final_response": "Hello from Hermes.",
            "input_tokens": 11,
            "output_tokens": 4,
        }


class FakeChatFactory:
    def __init__(self, gate=None):
        self.gate = gate
        self.instances = []

    def __call__(self, **kwargs):
        agent = FakeChatAgent(self, kwargs)
        self.instances.append(agent)
        return agent


def chat_tenant(client, name="Acme"):
    login = client.post("/api/v1/auth/login", json={
        "email": "admin@example.test", "password": "correct-horse-battery",
    })
    auth_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    company = client.post("/api/v1/admin/companies", headers=auth_headers, json={"name": name})
    headers = {**auth_headers, "X-Company-ID": company.json()["id"]}
    client.patch("/api/v1/company/profile", headers=headers, json={"data": {"name": name}})
    return auth_headers, headers, company.json()["id"]


def sse_payload(response, event_name):
    blocks = [block for block in response.text.split("\n\n") if block.strip()]
    block = next(block for block in blocks if block.startswith(f"event: {event_name}\n"))
    return json.loads(next(line[6:] for line in block.splitlines() if line.startswith("data: ")))


def test_phase3_chat_streams_tokens_and_persists_strict_history():
    factory = FakeChatFactory()
    app, client = make_client(chat_agent_factory=factory, chat_enabled=True,
                              chat_model="test-model", chat_toolset="coding")
    _, headers, company_id = chat_tenant(client)

    created = client.post("/api/session/new", headers=headers, json={"profile": "default"})
    assert created.status_code == 201, created.text
    session = created.json()["session"]
    assert session["model"] == "test-model"
    assert client.get("/api/session", headers=headers, params={
        "session_id": session["session_id"], "messages": 0, "resolve_model": 0,
    }).status_code == 200

    started = client.post("/api/chat/start", headers=headers, json={
        "session_id": session["session_id"], "message": "What should we do next?",
        "model": "ignored-client-model", "workspace": "", "model_provider": "",
        "profile": "default",
    })
    assert started.status_code == 200, started.text
    stream_id = started.json()["stream_id"]
    streamed = client.get("/api/chat/stream", params={"stream_id": stream_id})
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert streamed.headers["cache-control"] == "no-cache"
    assert streamed.headers["x-accel-buffering"] == "no"
    assert streamed.text.index("event: token") < streamed.text.index("event: done")
    assert streamed.text.count("event: token") == 2
    done = sse_payload(streamed, "done")
    assert done == {
        "session": {"session_id": session["session_id"]},
        "usage": {"input_tokens": 11, "output_tokens": 4},
        "answer": "Hello from Hermes.",
    }
    assert client.get("/api/chat/stream", params={"stream_id": stream_id}).status_code == 404

    kwargs = factory.instances[0].kwargs
    assert kwargs["enabled_toolsets"] == []
    assert kwargs["skip_context_files"] is True and kwargs["skip_memory"] is True
    assert kwargs["max_iterations"] == 15
    assert "Acme" in kwargs["ephemeral_system_prompt"]

    second = client.post("/api/chat/start", headers=headers, json={
        "session_id": session["session_id"], "message": "And after that?",
    })
    assert second.status_code == 200
    assert client.get("/api/chat/stream", params={"stream_id": second.json()["stream_id"]}).status_code == 200
    assert [item["role"] for item in factory.instances[1].history] == ["user", "assistant"]

    row = app.state.db.one("SELECT history FROM chat_sessions WHERE id=? AND company_id=?",
                           (session["session_id"], company_id))
    history = json.loads(row["history"])
    assert [item["role"] for item in history] == ["user", "assistant", "user", "assistant"]


def test_phase3_rejects_concurrent_turns_and_unknown_capabilities():
    gate = threading.Event()
    factory = FakeChatFactory(gate)
    _, client = make_client(chat_agent_factory=factory, chat_enabled=True)
    _, headers, _ = chat_tenant(client)
    session = client.post("/api/session/new", headers=headers, json={"profile": "default"}).json()["session"]
    body = {"session_id": session["session_id"], "message": "Hold this response"}
    first = client.post("/api/chat/start", headers=headers, json=body)
    assert first.status_code == 200
    assert client.post("/api/chat/start", headers=headers, json=body).status_code == 409
    assert client.get("/api/chat/stream", params={"stream_id": "not-a-capability"}).status_code == 404
    gate.set()
    assert client.get("/api/chat/stream", params={"stream_id": first.json()["stream_id"]}).status_code == 200


def test_phase3_chat_sessions_fail_closed_across_tenants():
    factory = FakeChatFactory()
    _, client = make_client(chat_agent_factory=factory, chat_enabled=True)
    admin_headers, company_a_headers, _ = chat_tenant(client, "Tenant A")
    company_b = client.post("/api/v1/admin/companies", headers=admin_headers, json={"name": "Tenant B"})
    company_b_headers = {**admin_headers, "X-Company-ID": company_b.json()["id"]}

    session = client.post("/api/session/new", headers=company_a_headers,
                          json={"profile": "default"}).json()["session"]
    assert client.get("/api/session", headers=company_b_headers,
                      params={"session_id": session["session_id"]}).status_code == 404
    assert client.post("/api/chat/start", headers=company_b_headers, json={
        "session_id": session["session_id"], "message": "Leak tenant A context",
    }).status_code == 404
    assert client.get("/api/session", params={"session_id": session["session_id"]}).status_code == 401


def test_phase4_uploads_real_files_and_enforces_configured_limit():
    app, client = make_client(max_upload_bytes=32)
    _, headers, company_id = chat_tenant(client)

    uploaded = client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"document_type": "current_contacts"},
        files={"file": ("contacts.csv", b"name,email\nAda,ada@example.test\n", "text/csv")},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["name"] == "contacts.csv"
    assert uploaded.json()["size_bytes"] == 32
    row = app.state.db.one(
        "SELECT storage_path FROM documents WHERE id=? AND company_id=?",
        (uploaded.json()["id"], company_id),
    )
    assert row and Path(row["storage_path"]).read_bytes() == b"name,email\nAda,ada@example.test\n"

    rejected = client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"document_type": "other"},
        files={"file": ("too-large.txt", b"x" * 33, "text/plain")},
    )
    assert rejected.status_code == 413
    assert app.state.db.one(
        "SELECT id FROM documents WHERE company_id=? AND name='too-large.txt'", (company_id,),
    ) is None
    assert not list(app.state.settings.upload_dir.rglob("too-large.txt"))


def test_phase4_filters_and_analytics_match_the_webui_contract():
    app, client = make_client()
    _, headers, company_id = chat_tenant(client)
    first = client.post("/api/v1/leads", headers=headers, json={
        "company_name": "Berlin Buyer GmbH", "country": "DE", "city": "Berlin",
        "industry": "Appliance distributor", "source": "trade_data",
    }).json()
    second = client.post("/api/v1/leads", headers=headers, json={
        "company_name": "Paris Retail SAS", "country": "FR", "city": "Paris",
        "industry": "Retail chain", "source": "web_search",
    }).json()
    stamp = now()
    high_scores = {
        key: 90 for key in (
            "product_fit_score", "market_fit_score", "company_quality_score",
            "intent_signal_score", "contactability_score", "insight_quality_score",
            "source_confidence_score",
        )
    }
    app.state.db.execute("INSERT INTO research VALUES(?,?,?,?,?,?,?,?)", (
        new_id("res"), company_id, first["id"], "succeeded",
        json_dump({"score_inputs": high_scores}), None, stamp, stamp,
    ))

    assert [item["id"] for item in client.get(
        "/api/v1/leads", headers=headers, params={"country": "DE", "q": "berlin"},
    ).json()] == [first["id"]]
    assert [item["id"] for item in client.get(
        "/api/v1/leads", headers=headers, params={"band": "high"},
    ).json()] == [first["id"]]
    assert [item["id"] for item in client.get(
        "/api/v1/leads", headers=headers, params={"country": "FR"},
    ).json()] == [second["id"]]

    pipeline = client.get("/api/v1/analytics/sales-pipeline", headers=headers)
    assert pipeline.status_code == 200
    assert {"leads_by_status", "emails_sent_weekly", "replies_weekly", "funnel"} <= pipeline.json().keys()
    assert sum(item["count"] for item in pipeline.json()["leads_by_status"]) == 2

    market = client.get("/api/v1/analytics/market-intelligence", headers=headers)
    assert market.status_code == 200
    assert {"country_scores", "top_industries", "source_performance", "product_market_fit"} <= market.json().keys()
    assert {item["country"] for item in market.json()["country_scores"]} == {"DE", "FR"}

    dashboard = client.get("/api/v1/analytics/dashboard", headers=headers)
    assert dashboard.status_code == 200
    assert dashboard.json()["sales"]["leads_found"] == 2
    assert set(dashboard.json()) >= {
        "sales", "sparks", "market", "recent_activity", "recommended_actions",
        "country_scores", "selected_countries",
    }


def test_phase4_promoted_routes_and_exports_are_tenant_safe():
    app, client = make_client()
    admin_headers, headers, company_id = chat_tenant(client)
    company_b = client.post(
        "/api/v1/admin/companies", headers=admin_headers, json={"name": "Tenant B"},
    ).json()
    company_b_headers = {**admin_headers, "X-Company-ID": company_b["id"]}

    for step in ("current-contacts", "integrations", "brain-review"):
        response = client.patch(
            f"/api/v1/onboarding/{step}", headers=headers,
            json={"data": {"confirmed": True}},
        )
        assert response.status_code == 200, response.text
        assert step in response.json()["completed_steps"]

    profile = client.put("/api/v1/integrations/whatsapp/profile", headers=headers, json={
        "business_name": "Acme Export",
        "whatsapp_business_account_id": "waba-1",
        "phone_number_id": "phone-1",
        "display_phone_number": "+1 555 0100",
        "business_country": "US",
        "default_language": "en",
    })
    assert profile.status_code == 200, profile.text
    assert profile.json()["profile_state"] == "saved"
    # No credentials are connected in this fixture, so verification must report
    # "incomplete". Only a successful Meta Graph call may report "verified".
    verified = client.post("/api/v1/integrations/whatsapp/profile/verify", headers=headers)
    assert verified.status_code == 200 and verified.json()["status"] == "incomplete"
    assert client.get(
        "/api/v1/integrations/whatsapp/profile", headers=company_b_headers,
    ).json() is None

    client.post("/api/v1/leads", headers=headers, json={
        "company_name": "Export Buyer", "country": "DE",
    })
    created = client.post(
        "/api/v1/exports/leads", headers=headers,
        json={"format": "csv", "filters": {}},
    )
    assert created.status_code == 201, created.text
    downloaded = client.get(
        f"/api/v1/exports/{created.json()['id']}/download", headers=headers,
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("text/csv")
    assert b"Export Buyer" in downloaded.content
    assert client.get(
        f"/api/v1/exports/{created.json()['id']}/download", headers=company_b_headers,
    ).status_code == 404

    failed = app.state.runs.create(company_id, "analytics_refresh", {})
    app.state.db.execute(
        "UPDATE agent_runs SET status='failed',error=?,completed_at=?,updated_at=? WHERE id=?",
        ("qualification failure", now(), now(), failed["id"]),
    )
    errors = client.get("/api/v1/admin/errors", headers=admin_headers)
    logs = client.get("/api/v1/admin/logs", headers=admin_headers, params={"limit": 2})
    assert errors.status_code == 200
    assert any(item["message"] == "qualification failure" for item in errors.json())
    assert logs.status_code == 200 and len(logs.json()) <= 2


def test_real_mode_has_no_mock_routes_or_tenant_seed():
    source = (ROOT / "server" / "webui" / "js" / "api.js").read_text(encoding="utf-8")
    assert "mode: 'real'" in source
    assert "export const MOCK_ROUTES = new Set();" in source
    assert "requestBody instanceof FormData" in source

    onboarding = (ROOT / "server" / "webui" / "js" / "pages" / "onboarding.js").read_text(
        encoding="utf-8",
    )
    assert "form.append('file', selected, selected.name)" in onboarding
    helpers = (ROOT / "server" / "webui" / "js" / "pages" / "_page-utils.js").read_text(
        encoding="utf-8",
    )
    assert "call('exports.download'" in helpers
    main = (ROOT / "server" / "webui" / "js" / "main.js").read_text(encoding="utf-8")
    assert "'X-Company-ID': companyId" in main
    assert "else resetReal()" in main
    assert "homeRoute(session)" in main
    assert "config.refreshAuth" in main
    state = (ROOT / "server" / "webui" / "js" / "real-state.js").read_text(encoding="utf-8")
    assert "payload.status === 'completed'" in state and "? 'complete'" in state


def test_login_rate_limit_fails_closed():
    _, client = make_client(auth_max_attempts=2, auth_window_seconds=60)
    for _ in range(2):
        assert client.post("/api/v1/auth/login", json={
            "email": "admin@example.test", "password": "wrong-password",
        }).status_code == 401
    blocked = client.post("/api/v1/auth/login", json={
        "email": "admin@example.test", "password": "wrong-password",
    })
    assert blocked.status_code == 429
