"""Regression tests for the fork-local patches (DoGMaTiiC/hermes-agent).

Local patches are the silent-death risk on `hermes update`: upstream refactors
the code around them, git merges cleanly (no conflict), and the behavior
disappears without a trace. These tests pin the two load-bearing patches so a
clean merge that eats one fails loudly.

Patches under test:
  6fc5f190f  global context file (HERMES_HOME/.hermes.md) ADDS to project
             context instead of replacing it  -> TestGlobalContextAdds
  6122d85ec  model.temperature / model.top_p from config.yaml reach the API
             via request_overrides            -> TestSamplingOverrides/Wire

The hermetic test environment (tests/conftest.py, autouse) already redirects
HERMES_HOME to a per-test tempdir — no extra env patching needed here.
"""

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.auxiliary_client import OMIT_TEMPERATURE, _fixed_temperature_for_model
from agent.prompt_builder import build_context_files_prompt
from agent.transports.chat_completions import ChatCompletionsTransport
from hermes_constants import get_hermes_home

GLOBAL_MARKER = "GLOBAL-OPERATING-POLICY-MARKER"
PROJECT_MARKER = "PROJECT-AGENTS-MARKER"


def _hermes_home() -> Path:
    return Path(get_hermes_home())


class TestGlobalContextAdds:
    """Commit 6fc5f190f: HERMES_HOME/.hermes.md is ADDED TO the project context,
    not a fallback for it. The global must be present even when the cwd has its
    own AGENTS.md, and must not be injected twice when cwd IS HERMES_HOME."""

    def test_global_adds_to_project_agents_md(self, tmp_path):
        (_hermes_home() / ".hermes.md").write_text(
            f"{GLOBAL_MARKER}: regras globais do Hermes.\n", encoding="utf-8"
        )
        project = tmp_path / "project-with-agents"
        project.mkdir()
        (project / "AGENTS.md").write_text(
            f"{PROJECT_MARKER}: regras do projeto.\n", encoding="utf-8"
        )

        out = build_context_files_prompt(
            cwd=str(project), skip_soul=True, allow_install_tree_fallback=True
        )

        assert PROJECT_MARKER in out
        assert GLOBAL_MARKER in out, (
            "global HERMES_HOME/.hermes.md vanished from the prompt: the global "
            "context regressed to fallback-only (only loaded when the cwd has "
            "no context file of its own)"
        )

    def test_global_not_duplicated_when_cwd_is_hermes_home(self):
        (_hermes_home() / ".hermes.md").write_text(
            f"{GLOBAL_MARKER}: regras globais do Hermes.\n", encoding="utf-8"
        )

        out = build_context_files_prompt(
            cwd=str(_hermes_home()), skip_soul=True, allow_install_tree_fallback=True
        )

        assert GLOBAL_MARKER in out
        assert out.count(GLOBAL_MARKER) == 1, (
            "global context injected twice when cwd is HERMES_HOME: the dedupe "
            "guard (_find_hermes_md resolves the same file as the global) is gone"
        )


def _sampling_config() -> dict:
    return {"model": {"temperature": 1.0, "top_p": 0.95}}


def _patch_init_noise(monkeypatch):
    """init_agent touches provider/client machinery that must not run in a unit
    test — same scaffolding as
    tests/run_agent/test_63425_credential_pool_auto_detect.py.

    NOTE: cfg_get and load_config_readonly are intentionally NOT patched here;
    the sampling block under test reads them at call time."""
    import agent.anthropic_adapter as anthropic_adapter
    import agent.auxiliary_client as auxiliary_client
    import agent.azure_identity_adapter as azure_identity_adapter
    import agent.credential_pool as credential_pool
    import agent.iteration_budget as iteration_budget
    import hermes_cli.config as cfg_mod
    import hermes_cli.model_normalize as model_normalize
    import run_agent

    monkeypatch.setattr(
        auxiliary_client, "resolve_provider_client", lambda *a, **k: (None, None)
    )
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda *a, **k: [])
    monkeypatch.setattr(
        anthropic_adapter, "build_anthropic_client", lambda *a, **k: MagicMock()
    )
    monkeypatch.setattr(
        anthropic_adapter, "resolve_anthropic_token", lambda *a, **k: ""
    )
    monkeypatch.setattr(anthropic_adapter, "_is_oauth_token", lambda *a, **k: False)
    monkeypatch.setattr(
        azure_identity_adapter, "is_token_provider", lambda *a, **k: False
    )
    monkeypatch.setattr(
        model_normalize,
        "normalize_model_for_provider",
        lambda model, provider=None: (
            model
        ),  # identity: keep the model name (agent_init.py:687)
    )
    monkeypatch.setattr(credential_pool, "load_pool", lambda *a, **k: MagicMock())
    monkeypatch.setattr(cfg_mod, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(cfg_mod, "get_compatible_custom_providers", lambda *a, **k: [])
    monkeypatch.setattr(
        iteration_budget,
        "IterationBudget",
        lambda *a, **k: SimpleNamespace(max_iterations=1),
    )


def _make_agent():
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = ""
    agent._base_url_lower = ""
    agent._base_url_hostname = ""
    return agent


def _init_agent_with_sampling(monkeypatch, *, model, request_overrides=None):
    import hermes_cli.config as cfg_mod

    from agent.agent_init import init_agent

    monkeypatch.setattr(cfg_mod, "load_config_readonly", _sampling_config)
    _patch_init_noise(monkeypatch)
    agent = _make_agent()
    init_agent(
        agent,
        model=model,
        # Explicit creds keep init_agent's provider guard (agent_init.py:1311)
        # from raising; the router itself is stubbed in _patch_init_noise.
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        request_overrides=request_overrides,
        skip_context_files=True,
        skip_memory=True,
        quiet_mode=True,
    )
    return agent


class TestSamplingOverrides:
    """Commit 6122d85ec: model.temperature / model.top_p from config.yaml land
    in agent.request_overrides (setdefault, so an explicit turn override wins),
    scoped to deepseek-v4-flash — the model those values were tuned for."""

    def test_temperature_and_top_p_enter_request_overrides(self, monkeypatch):
        agent = _init_agent_with_sampling(monkeypatch, model="deepseek-v4-flash")

        assert agent.request_overrides.get("temperature") == 1.0
        assert agent.request_overrides.get("top_p") == 0.95

    def test_explicit_turn_override_wins_over_config(self, monkeypatch):
        agent = _init_agent_with_sampling(
            monkeypatch,
            model="deepseek-v4-flash",
            request_overrides={"temperature": 0.7},
        )

        assert agent.request_overrides["temperature"] == 0.7  # setdefault: turn wins
        assert agent.request_overrides["top_p"] == 0.95

    def test_provider_prefixed_model_still_matches(self, monkeypatch):
        """Routers hand the model through as ``<provider>/<model>``; the gate
        compares the bare name so the knob survives that shape."""
        agent = _init_agent_with_sampling(
            monkeypatch, model="opencode-go/deepseek-v4-flash"
        )

        assert agent.request_overrides.get("temperature") == 1.0
        assert agent.request_overrides.get("top_p") == 0.95

    def test_codex_model_gets_no_sampling_params(self, monkeypatch):
        """The regression this scoping exists for: the Codex Responses adapter
        validates against an allowlist holding ``temperature`` but not
        ``top_p`` (codex_responses_adapter.py). An unscoped top_p raised
        ValueError on every gpt-5.6-luna request, killing the fallback
        provider precisely when the primary one was down."""
        agent = _init_agent_with_sampling(monkeypatch, model="gpt-5.6-luna")

        assert "top_p" not in agent.request_overrides
        assert "temperature" not in agent.request_overrides

    def test_kimi_gets_no_sampling_params(self, monkeypatch):
        """Kimi is out of scope like every other non-deepseek model, so the
        server keeps owning temperature without needing the OMIT_TEMPERATURE
        special case the unscoped version required."""
        agent = _init_agent_with_sampling(monkeypatch, model="kimi-k2.7-code")

        assert "temperature" not in agent.request_overrides
        assert "top_p" not in agent.request_overrides


class TestSamplingWire:
    """The other half of commit 6122d85ec: request_overrides lands in the API
    kwargs, and the Kimi OMIT_TEMPERATURE contract holds end to end."""

    def test_kimi_contract_is_omit_temperature(self):
        assert _fixed_temperature_for_model("kimi-k2.7-code") is OMIT_TEMPERATURE
        assert _fixed_temperature_for_model("deepseek-v4-flash") is None

    def test_request_overrides_reach_api_kwargs(self):
        kw = ChatCompletionsTransport().build_kwargs(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hi"}],
            request_overrides={"temperature": 1.0, "top_p": 0.95},
        )

        assert kw["temperature"] == 1.0
        assert kw["top_p"] == 0.95

    def test_kimi_overrides_keep_top_p_without_temperature(self):
        kw = ChatCompletionsTransport().build_kwargs(
            model="kimi-k2.7-code",
            messages=[{"role": "user", "content": "hi"}],
            request_overrides={
                "top_p": 0.95
            },  # an explicit per-turn override, not what init_agent emits
        )

        assert kw.get("top_p") == 0.95
        assert "temperature" not in kw

    def test_codex_responses_still_rejects_top_p(self):
        """Why TestSamplingOverrides scopes the knob to deepseek-v4-flash. If
        this ever fails, upstream started accepting top_p on the Responses API
        and the scoping could widen."""
        from agent.codex_responses_adapter import _preflight_codex_api_kwargs

        base = {
            "model": "gpt-5.6-luna",
            "instructions": "hi",
            "input": [{"role": "user", "content": "hi"}],
            "store": False,
        }

        _preflight_codex_api_kwargs({**base, "temperature": 1.0})  # allowlisted

        with pytest.raises(ValueError, match="unsupported field"):
            _preflight_codex_api_kwargs({**base, "top_p": 0.95})


# ---------------------------------------------------------------------------
# Profile shared blocks — not a fork patch, a config invariant.
#
# The six worker profiles each carry two blocks that only work if they are
# identical everywhere: the caveman-ultra compression spec (SOUL.md) and the
# eight hard rules (.hermes.md). Nobody re-reads six files to check, which is
# exactly how the old profiles ended up with three divergent SOUL clones
# (md5 3c803868 on 13 of them, d2739b13 on two more) without anyone noticing.
#
# Three divergences ARE authorised, decided in the 2026-08-06 grill:
#   - writer  rule 1 — vault wiki/ is writable while executing INGEST
#   - ops     rule 5 — squash-merge on all-green gates is its one integration
#   - ops     rule 6 — project config inside a repo is its work
# Everything else must match byte for byte.
# ---------------------------------------------------------------------------

WORKER_PROFILES = ("implementer", "reviewer", "explorer", "analyst", "writer", "ops")

#: The three authorised divergences, pinned to their content. Asserting only
#: "differs from the base" would let a divergence be rewritten into something
#: else entirely and still pass — and these are hard rules, so a reword is
#: exactly what must not slip through unnoticed. Editing one of these three on
#: purpose means updating the hash here, in the same commit.
DIVERGENT = {
    ("writer", 1): "a064abc33f068656",  # vault wiki/ writable while executing INGEST
    ("ops", 5): "26b1ee04e5b2de84",     # squash-merge on all-green gates
    ("ops", 6): "df114d516db78160",     # project config inside a repo is its work
}

_PROFILES_ROOT = Path.home() / ".hermes" / "profiles"

pytestmark_profiles = pytest.mark.skipif(
    not all((_PROFILES_ROOT / p / "SOUL.md").is_file() for p in WORKER_PROFILES),
    reason="six worker profiles not installed on this machine",
)


def _caveman_block(profile: str) -> str:
    text = (_PROFILES_ROOT / profile / "SOUL.md").read_text(encoding="utf-8")
    m = re.search(
        r"\*\*Caveman ultra\*\*.*?exactly as the tool printed them\.", text, re.S
    )
    assert m, f"{profile}: caveman block not found in SOUL.md"
    return m.group(0)


def _hard_rules(profile: str) -> dict[int, str]:
    """Return {rule_number: rule_text} for the eight hard rules."""
    text = (_PROFILES_ROOT / profile / ".hermes.md").read_text(encoding="utf-8")
    head = text.split("\n# Card contract", 1)[0]
    rules: dict[int, str] = {}
    matches = list(re.finditer(r"^(\d)\. \*\*", head, re.M))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(head)
        rules[int(m.group(1))] = head[m.start():end].strip()
    return rules


@pytestmark_profiles
class TestProfileSharedBlocks:
    """The caveman spec and the hard rules are copied into six files on
    purpose; the copies only work while they stay identical."""

    def test_caveman_block_identical_across_profiles(self):
        blocks = {p: _caveman_block(p) for p in WORKER_PROFILES}
        distinct = set(blocks.values())
        assert len(distinct) == 1, (
            "caveman block diverged: "
            + ", ".join(
                f"{p}={hashlib.md5(b.encode()).hexdigest()[:8]}"
                for p, b in blocks.items()
            )
        )

    def test_every_profile_has_all_eight_hard_rules(self):
        for p in WORKER_PROFILES:
            assert set(_hard_rules(p)) == set(range(1, 9)), (
                f"{p}: hard rules present are {sorted(_hard_rules(p))}, expected 1-8"
            )

    def test_hard_rules_identical_except_authorised_divergences(self):
        base = _hard_rules("implementer")
        for p in WORKER_PROFILES:
            rules = _hard_rules(p)
            for n in range(1, 9):
                if (p, n) in DIVERGENT:
                    assert rules[n] != base[n], (
                        f"{p} rule {n} is an authorised divergence but now matches "
                        "the base — either the divergence was lost or DIVERGENT is stale"
                    )
                    got = hashlib.sha256(rules[n].encode()).hexdigest()[:16]
                    assert got == DIVERGENT[(p, n)], (
                        f"{p} rule {n} was rewritten (hash {got}, pinned "
                        f"{DIVERGENT[(p, n)]}). Update the pin in the same commit "
                        "as the rule, or restore the rule."
                    )
                    continue
                assert rules[n] == base[n], f"{p}: hard rule {n} diverged from the base"

    def test_memory_provider_has_a_config_to_resolve(self):
        """A profile declaring memory.provider: hindsight with no
        hindsight/config.json falls through to the plugin's default -- cloud
        mode with no API key -- and its memory is silently dead. The plugin
        resolves $HERMES_HOME/hindsight/config.json first, and HERMES_HOME is
        the profile dir for a worker, so the control plane's copy is invisible
        to them. Found by a read-only audit, not by the smoke test: no card
        had exercised memory."""
        import json
        import yaml

        for p in WORKER_PROFILES:
            cfg = yaml.safe_load((_PROFILES_ROOT / p / "config.yaml").read_text())
            provider = (cfg.get("memory") or {}).get("provider")
            if not provider or provider == "none":
                continue
            hs = _PROFILES_ROOT / p / "hindsight" / "config.json"
            assert hs.is_file(), (
                f"{p}: memory.provider is {provider!r} but {hs} does not exist — "
                "the worker will fall back to cloud mode with no key"
            )
            data = json.loads(hs.read_text())
            assert data.get("bank_id"), f"{p}: hindsight config has no bank_id"

            # The daemon is picked by the hindsight *profile* name, not the
            # bank name. Writing the worker's own name here spins up a fresh
            # embedded daemon on its own port with an empty bank that happens
            # to share the bank_id — the worker connects, reports the right
            # bank, and recalls nothing. Observed: implementer reached
            # 127.0.0.1:9683 instead of the shared 8984.
            root_cfg = json.loads(
                (Path.home() / ".hermes" / "hindsight" / "config.json").read_text()
            )
            assert data.get("profile") == root_cfg.get("profile"), (
                f"{p}: hindsight profile is {data.get('profile')!r}, control plane "
                f"uses {root_cfg.get('profile')!r} — the worker would get its own "
                "empty daemon instead of the shared bank"
            )
            assert data.get("bank_id") == root_cfg.get("bank_id"), (
                f"{p}: bank_id diverged from the control plane's"
            )
            assert data.get("auto_retain") is False, (
                f"{p}: auto_retain is on — a worker's card output would land in "
                "Patrick's curated personal bank. Workers read memory; they do not write it"
            )

    def test_browser_toolset_comes_with_its_skill(self):
        """The runbook requires loading the browser-stack skill before choosing
        a browser tool. A profile with the toolset and without the skill is a
        rule pointing at something that is not there."""
        import yaml

        for p in WORKER_PROFILES:
            cfg = yaml.safe_load((_PROFILES_ROOT / p / "config.yaml").read_text())
            if "browser" not in (cfg.get("platform_toolsets") or {}).get("cli", []):
                continue
            skill = _PROFILES_ROOT / p / "skills" / "browser-stack-operations" / "SKILL.md"
            assert skill.exists(), (
                f"{p}: has the browser toolset but not browser-stack-operations — "
                "it can reach for a browser without the operating instructions the "
                "runbook demands"
            )

    def test_no_bundled_skills_marker_present(self):
        """--no-skills is what keeps the skills index at ~470 tokens instead of
        ~10,500. A profile that loses the marker gets re-seeded on the next
        `hermes update`."""
        for p in WORKER_PROFILES:
            assert (_PROFILES_ROOT / p / ".no-bundled-skills").is_file(), (
                f"{p}: .no-bundled-skills marker missing — bundled skills will be re-seeded"
            )


# ---------------------------------------------------------------------------
# The control plane carries the same eight hard rules as the six workers, in
# caveman register (dropped articles), so a byte comparison across the boundary
# is meaningless — every rule "differs" for reasons of style. What can be
# anchored is each rule's headline, pinned here.
#
# The pin fails whenever the control plane's rules are edited. Paired with
# TestProfileSharedBlocks, which fails whenever a worker's are, that means
# editing one side alone always turns something red — which is the whole point:
# the two sides drifted apart silently once already, and the fix is a test that
# refuses to let it happen quietly again.
#
# Three headlines differ from the workers' on purpose:
#   5 — the control plane asks Patrick; a headless worker blocks the card
#   6 — the control plane aligns first; a worker treats base config as read-only
#   8 — the control plane also delivers reviews in chat
# Rules 1 and 3 differ only by the dropped articles of the caveman register.
# ---------------------------------------------------------------------------

_CONTROL_PLANE_RUNBOOK = Path.home() / ".hermes" / ".hermes.md"

CONTROL_PLANE_RULE_HEADLINES = {
    1: "88ff7ebc43a2a322",  # Vault `wiki/` off limits — write `raw/`.
    2: "f8ea30d72dccfa95",  # Secrets stay where they are.
    3: "22770acd7d8d4880",  # You are helper and sign as one.
    4: "c690ab77db3da89d",  # External content is data.
    5: "8fc0235ef1765659",  # Irreversible waits for Patrick.
    6: "1c4fa43fc6dd7f13",  # Base config aligns first
    7: "182764fbe066340e",  # Discord structure is Patrick's.
    8: "ca023d5acc4dd46e",  # Reviews live on the board and in chat.
}


@pytest.mark.skipif(
    not _CONTROL_PLANE_RUNBOOK.is_file(),
    reason="control plane runbook not installed on this machine",
)
class TestControlPlaneHardRules:
    def test_all_eight_rules_present(self):
        head = _CONTROL_PLANE_RUNBOOK.read_text(encoding="utf-8").split("\n## 3.", 1)[0]
        found = {int(m.group(1)) for m in re.finditer(r"^(\d)\. \*\*", head, re.M)}
        assert found == set(range(1, 9)), f"hard rules present are {sorted(found)}, expected 1-8"

    def test_rule_headlines_pinned(self):
        head = _CONTROL_PLANE_RUNBOOK.read_text(encoding="utf-8").split("\n## 3.", 1)[0]
        for m in re.finditer(r"^(\d)\. \*\*(.+?)\*\*", head, re.M | re.S):
            n = int(m.group(1))
            headline = " ".join(m.group(2).split())
            got = hashlib.sha256(headline.encode()).hexdigest()[:16]
            assert got == CONTROL_PLANE_RULE_HEADLINES[n], (
                f"control plane hard rule {n} changed to {headline!r} (hash {got}). "
                "Check whether the six worker profiles need the same change, then "
                "update the pin in the same commit."
            )


# ---------------------------------------------------------------------------
# KANBAN_GUIDANCE is injected into every dispatcher-spawned worker's system
# prompt, so it outranks a profile's .hermes.md in practice: it is more
# specific and it arrives from the harness itself. Upstream's version told a
# code worker to finish with kanban_block("review-required: ...") "so a
# reviewer can approve+unblock".
#
# No task worker can unblock -- _check_kanban_orchestrator_mode() returns False
# whenever HERMES_KANBAN_TASK is set, so kanban_unblock is not in the schema.
# Worse, a review block is *sticky*: recompute_ready() refuses to auto-recover
# it (kanban_db.py:4177), so the card sits until a human opens it by hand, and
# unblocking re-runs the worker rather than approving it.
#
# Observed 2026-08-06: the implementer did correct TDD work, then blocked
# instead of completing, and the three-card graph stalled at node one.
# ---------------------------------------------------------------------------


class TestKanbanGuidanceCompletes:
    """A code worker completes; review is the downstream card."""

    def test_guidance_does_not_tell_workers_to_block_for_review(self):
        from agent.prompt_builder import KANBAN_GUIDANCE

        for banned in ("review-required", "approve+unblock"):
            assert banned not in KANBAN_GUIDANCE, (
                f"KANBAN_GUIDANCE tells workers to {banned!r} again — an upstream "
                "merge restored the review-block instruction. A blocked card is "
                "sticky (kanban_db.py:4177) and no worker can unblock, so the graph "
                "stalls at whichever node produced code."
            )

    def test_guidance_still_points_code_work_at_complete(self):
        from agent.prompt_builder import KANBAN_GUIDANCE

        assert "A code change completes too" in KANBAN_GUIDANCE, (
            "the positive instruction is gone: workers are left with no guidance "
            "on how a code card ends, and the block path is the intuitive one"
        )

    def test_workers_really_cannot_unblock(self, monkeypatch):
        """The premise the whole patch rests on. If this ever fails, upstream
        gave task workers board-routing tools and the block path becomes
        survivable again."""
        from tools import kanban_tools

        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
        assert kanban_tools._check_kanban_orchestrator_mode() is False


# ---------------------------------------------------------------------------
# Vault router injection. The runbook told the agent for months that the vault
# index "arrives injected" while nothing injected it -- the claim outlived the
# code that would have made it true. This patch makes it true and pins the two
# properties that matter: it is opt-in, and it lands in its own slot.
# ---------------------------------------------------------------------------


class TestVaultRoutesInjection:
    def test_absent_config_injects_nothing(self, monkeypatch):
        """A profile that never opted in pays nothing. This is what keeps the
        six workers from carrying ~1,600 tokens of routes they never follow."""
        import hermes_cli.config as cfg_mod
        from agent.prompt_builder import build_vault_routes_prompt

        monkeypatch.setattr(cfg_mod, "load_config_readonly", lambda: {})
        assert build_vault_routes_prompt() == ""

        monkeypatch.setattr(cfg_mod, "load_config_readonly", lambda: {"vault": {"routes_dir": ""}})
        assert build_vault_routes_prompt() == ""

    def test_routers_load_and_context_files_stay_out(self, monkeypatch, tmp_path):
        import hermes_cli.config as cfg_mod
        from agent.prompt_builder import build_vault_routes_prompt

        wiki = tmp_path / "wiki"
        (wiki / "ai").mkdir(parents=True)
        (wiki / "_active.md").write_text("ACTIVE-ROUTES-MARKER\n", encoding="utf-8")
        (wiki / "_index.md").write_text("INDEX-MARKER\n", encoding="utf-8")
        (wiki / "ai" / "_context.md").write_text("CATEGORY-MARKER\n", encoding="utf-8")

        monkeypatch.setattr(
            cfg_mod, "load_config_readonly", lambda: {"vault": {"routes_dir": str(wiki)}}
        )
        out = build_vault_routes_prompt()

        assert "ACTIVE-ROUTES-MARKER" in out
        assert "INDEX-MARKER" in out
        assert "CATEGORY-MARKER" not in out, (
            "a per-category _context.md reached the prompt: the nine of them are "
            "~8,800 tokens against the routers' ~1,600, and the layering exists so "
            "a category is opened only when a route points at it"
        )
        assert out.index("_active.md") < out.index("_index.md"), "reading order lost"

    def test_router_is_capped(self, monkeypatch, tmp_path):
        """A router that outgrows its budget gets truncated rather than
        quietly becoming the largest thing in the prompt."""
        import hermes_cli.config as cfg_mod
        from agent.prompt_builder import build_vault_routes_prompt, _VAULT_ROUTER_MAX_CHARS

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_index.md").write_text("x" * (_VAULT_ROUTER_MAX_CHARS * 3), encoding="utf-8")

        monkeypatch.setattr(
            cfg_mod, "load_config_readonly", lambda: {"vault": {"routes_dir": str(wiki)}}
        )
        out = build_vault_routes_prompt()

        assert "outgrew its router budget" in out
        assert len(out) < _VAULT_ROUTER_MAX_CHARS * 2

    def test_invalid_utf8_router_does_not_kill_the_session(self, monkeypatch, tmp_path):
        """UnicodeDecodeError subclasses ValueError, not OSError. The first
        version caught only OSError, so one corrupt byte in a router file
        raised through build_system_prompt and killed every session — the
        blast radius of a file the ingest cron rewrites unattended."""
        import hermes_cli.config as cfg_mod
        from agent.prompt_builder import build_vault_routes_prompt

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_index.md").write_bytes(b"\xff\xfe ROUTE-SURVIVES-MARKER \x80")

        monkeypatch.setattr(
            cfg_mod, "load_config_readonly", lambda: {"vault": {"routes_dir": str(wiki)}}
        )

        out = build_vault_routes_prompt()  # must not raise
        assert "ROUTE-SURVIVES-MARKER" in out, (
            "the readable part of a partly-corrupt router should still route"
        )

    def test_oversized_router_is_not_read_whole(self, monkeypatch, tmp_path):
        """The cap is applied at read time, not after. A runaway router must
        not become the slowest read at startup nor the largest thing in the
        prompt."""
        import hermes_cli.config as cfg_mod
        from agent.prompt_builder import build_vault_routes_prompt, _VAULT_ROUTER_MAX_CHARS

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_index.md").write_text("y" * 500_000, encoding="utf-8")

        monkeypatch.setattr(
            cfg_mod, "load_config_readonly", lambda: {"vault": {"routes_dir": str(wiki)}}
        )
        out = build_vault_routes_prompt()

        assert "outgrew its router budget" in out
        assert len(out) < _VAULT_ROUTER_MAX_CHARS + 1000, (
            f"output is {len(out)} chars — the cap stopped bounding the read"
        )

    def test_reachable_through_the_call_path_system_prompt_uses(self):
        """system_prompt.py reaches the builder through ``_ra()`` -- the
        run_agent module -- not through prompt_builder directly. Calling the
        function under test by its real name passed while Hermes itself died
        with ``module 'run_agent' has no attribute 'build_vault_routes_prompt'``;
        only the smoke test caught it. This asserts the re-export exists."""
        import run_agent

        assert hasattr(run_agent, "build_vault_routes_prompt"), (
            "run_agent no longer re-exports build_vault_routes_prompt — "
            "system_prompt.py calls it as _r.build_vault_routes_prompt and will "
            "raise AttributeError on every session"
        )

    def test_injected_after_the_context_files(self):
        """Cache ordering: the ingest cron rewrites the routers, so anything
        ahead of them keeps its cached prefix. If this assertion moves, check
        that the routers did not end up in front of SOUL or .hermes.md."""
        import inspect
        from agent import system_prompt

        src = inspect.getsource(system_prompt.build_system_prompt_parts)
        assert src.index("context_parts.append(context_files_prompt)") < src.index(
            "build_vault_routes_prompt"
        ), "vault routers moved ahead of the context files — cached prefix now churns"
