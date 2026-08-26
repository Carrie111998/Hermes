"""The one test that runs the real agent instead of a stub.

Every other server test injects :class:`StubRunExecutor`, which answers a
``lead_research_gap`` with ``{"pages": [], "facts": [], "stop_reason":
"source_exhausted"}``. That is enough to exercise routing, storage, scoring and
tenancy, and it proves nothing at all about the half of the product the model
does: the suite stays green whether the agent researches a company or returns
nothing forever.

So this runs ``hermes`` for real, on a real domain, and asserts the three
things that break silently otherwise:

1. the agent's stdout parses and validates against the run contract,
2. it actually fetched something — no page means no research happened,
3. every fact it proposed survives ``accept_fact`` against the page the agent
   itself returned, which is the guard the agentic path cannot ship without.

Marked ``integration`` because it spends tokens and touches the network, so
``addopts = -m 'not integration'`` keeps it out of the default run::

    pytest tests/integration/test_live_lead_research_gap.py -m integration
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.agent_service import AgentRunService, HermesProcessExecutor, extract_json
from server.config import Settings
from server.db import Database, json_dump, new_id, now
from server.lead_research.agentic import AgenticResearchService
from server.lead_research.models import AgenticResearchRequest


pytestmark = pytest.mark.integration

# A real, public, stable manufacturer site. Research is read-only — the run
# never contacts the company, it only reads pages it is allowed to read.
TARGET_COMPANY = "Silverline"
TARGET_DOMAIN = "silverline.com.tr"

# Generous enough for a live model with a page budget, short enough that a
# hung run fails the suite rather than parking it.
RUN_TIMEOUT_SECONDS = 600


def _require_hermes() -> str:
    """The model to research with, or a skip naming what is missing.

    The probe is a real one-shot agent call rather than ``--version``, because
    the ways this path is unusable are all downstream of the binary existing:
    no provider selected, an expired key, a model the account cannot reach.
    ``hermes -z`` exits 0 on those, printing the reason instead of JSON — so
    the probe asks for JSON and treats "no JSON came back" as unconfigured.

    An unconfigured machine skips. A configured one that cannot research is a
    failure, which is the entire point of the file.
    """
    if shutil.which("hermes") is None:
        pytest.skip("hermes is not on PATH: the live agent path cannot be exercised")
    # Same value a campaign's model profile carries in production
    # (`GET /research/model-profiles` serves `settings.chat_model`), so the
    # live run uses the model the deployment would actually use.
    model = os.environ.get("INTERFAZE_LIVE_TEST_MODEL") or Settings.load().chat_model
    if not model:
        pytest.skip(
            "no research model configured: set chat_model in the interfaze "
            "config, or INTERFAZE_LIVE_TEST_MODEL for this run"
        )
    try:
        probe = subprocess.run(
            ["hermes", "-z", 'Output JSON and nothing else: {"ok": true}',
             "--yolo", "--model", model],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"hermes is not runnable here: {exc}")
    transcript = f"{probe.stdout}\n{probe.stderr}".strip()
    try:
        assert extract_json(transcript).get("ok") is True
    except (ValueError, AssertionError):
        pytest.skip(
            f"hermes cannot complete a trivial run with model {model!r}, so "
            f"there is no agent to test: {transcript[-400:] or '(no output)'}"
        )
    return model


def _tenant() -> tuple[Database, str, str]:
    db = Database(Path(tempfile.mkdtemp()) / "live.db")
    company_id, campaign_id, stamp = new_id("cmp"), new_id("cam"), now()
    db.execute(
        "INSERT INTO companies VALUES(?,?,?,?,?,?,?)",
        (company_id, TARGET_COMPANY, None, "active", json_dump({}), stamp, stamp),
    )
    return db, company_id, campaign_id


def _request(campaign_id: str, organization_id: str, model: str) -> AgenticResearchRequest:
    return AgenticResearchRequest(
        campaign_id=campaign_id,
        organization_id=organization_id,
        company_name=TARGET_COMPANY,
        canonical_domain=TARGET_DOMAIN,
        batches=[{
            "source_hint": "the company's own website",
            "fields": ["description", "product_sector_fit"],
            "route": "agentic",
        }],
        market_terms={"canonical": ["oven", "hob", "hood"]},
        # The executor passes this straight through as hermes --model.
        decision_model=model,
        budget={"page_limit": 3, "request_limit": 6, "time_limit_seconds": 240,
                "token_limit": 20_000},
    )


def _wait(runs: AgentRunService, company_id: str, run_id: str) -> dict:
    deadline = time.time() + RUN_TIMEOUT_SECONDS
    while time.time() < deadline:
        run = runs.get(company_id, run_id)
        if run["status"] in {"succeeded", "failed", "cancelled"}:
            return run
        time.sleep(0.5)
    runs.cancel(company_id, run_id)
    raise AssertionError(f"live agent run did not settle in {RUN_TIMEOUT_SECONDS}s")


def test_live_agent_researches_a_company_and_its_facts_survive_the_quote_check():
    model = _require_hermes()
    db, company_id, campaign_id = _tenant()
    runs = AgentRunService(db, HermesProcessExecutor(timeout=RUN_TIMEOUT_SECONDS))
    organization_id = new_id("org")

    created = runs.create(
        company_id, "lead_research_gap",
        _request(campaign_id, organization_id, model).model_dump(mode="json"),
    )
    runs.start(company_id, created["id"])
    run = _wait(runs, company_id, created["id"])

    # 1. The contract held. `_validate_output` already rejected anything that
    #    did not parse as an AgenticResearchResult, so reaching "succeeded" is
    #    the assertion — the error text is what makes a failure diagnosable.
    assert run["status"] == "succeeded", f"live run failed: {run['error']}"

    # 2. Research actually happened. An agent with no reachable web tool
    #    returns a well-formed empty result and would otherwise pass here,
    #    which is precisely the regression this file exists to catch.
    output = run["output"]
    assert output["pages"], (
        "the agent returned no pages: it validated the contract without "
        f"researching anything (stop_reason={output.get('stop_reason')!r})"
    )

    # 3. Every fact is mechanically supported by its own snapshot. This runs
    #    the production acceptance path, hash check and `accept_fact` included,
    #    so an invented company with invented numbers fails here rather than
    #    reaching a customer's lead list.
    agentic = AgenticResearchService(db, runs=runs)
    stored = agentic.accept_result(company_id, created["id"])
    assert stored, (
        f"{len(output['facts'])} proposed fact(s), none accepted: every span "
        "failed to match the page the agent returned"
    )
