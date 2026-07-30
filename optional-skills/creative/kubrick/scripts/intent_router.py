# scripts/intent_router.py
"""Operator intent registry and legacy alias tables for Kubrick CLI routing."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


@dataclass(frozen=True)
class IntentCall:
    intent: str
    action: str
    script: Path
    argv: list[str]
    legacy_name: str | None = None


@dataclass(frozen=True)
class AliasSpec:
    intent: str
    action: str
    # When True, first remainder token may be treated as action (ledger, operator)
    passthrough_action: bool = False
    fixed_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntentSpec:
    description: str
    actions: dict[str, str]  # action -> script filename
    default_action: str | None = None
    # Optional specialized argv builder name keys later


INTENT_REGISTRY: dict[str, IntentSpec] = {
    "compile": IntentSpec(
        "Full brief-to-packet compile",
        {"run": "kubrick_compile.py"},
        "run",
    ),
    "retrieve": IntentSpec(
        "Registry-aware pattern retrieval",
        {"retrieve": "retrieve_symbolic_patterns_registry.py"},
        "retrieve",
    ),
    "ledger": IntentSpec(
        "Project symbolic ledger",
        {
            "init": "symbolic_ledger.py",
            "audit": "symbolic_ledger.py",
            "mutate": "symbolic_ledger.py",
            "rehydrate": "symbolic_ledger.py",
            "apply-forge": "symbolic_ledger.py",
            "export-retrieval": "symbolic_ledger.py",
            "record-pattern": "symbolic_ledger.py",
        },
        None,
    ),
    "design": IntentSpec(
        "Design-specification compilation",
        {"build": "generate_design_spec.py"},
        "build",
    ),
    "storyboard": IntentSpec(
        "Multi-frame state",
        {
            "propagate": "propagate_graph_state.py",
            "compare": "compare_frame_state.py",
        },
        None,
    ),
    "adapt": IntentSpec(
        "Neutral packet and provider adaptation",
        {
            "build": "build_model_adapter_packet.py",
            "provider": "adapt_provider.py",
        },
        "build",
    ),
    "visual": IntentSpec(
        "Visual QA loop",
        {
            "normalize": "normalize_visual_observation.py",
            "compare": "compare_visual_observation.py",
            "correct": "build_visual_correction_packet.py",
            "govern": "govern_correction_iteration.py",
            "closed-loop": "closed_loop_visual_qa.py",
        },
        None,
    ),
    "learn": IntentSpec(
        "Outcomes and multi-signal evolution",
        {
            "outcome": "record_pattern_outcome.py",
            "evolve": "propose_pattern_evolution.py",
            "forge-signals": "extract_forge_signals.py",
        },
        None,
    ),
    "check": IntentSpec(
        "Validation and regression",
        {
            "skill": "validate_hermes_skill.py",
            "corpus": "validate_pattern_corpus.py",
            "coverage": "audit_corpus_coverage.py",
            "artifact": "validate_artifact.py",
            "repeatability": "check_repeatability.py",
            "eval": "run_hermes_evals.py",
            # smoke is composite (skill + corpus); script is primary entry for registry
            "smoke": "validate_hermes_skill.py",
        },
        "smoke",
    ),
    "operate": IntentSpec(
        "Graph/ledger operators",
        {
            "saturation-score": "graph_operators.py",
            "counterpoint": "graph_operators.py",
            "convergence-lock": "graph_operators.py",
            "surface-occult-audit": "graph_operators.py",
            "symbolic-architecture-export": "graph_operators.py",
            "motif-mutation": "graph_operators.py",
        },
        None,
    ),
    "mcp": IntentSpec(
        "Optional MCP server",
        {"serve": "mcp_kubrick_server.py"},
        "serve",
    ),
    "bundle": IntentSpec(
        "Grok review bundle",
        {"build": "build_grok_review_bundle.py"},
        "build",
    ),
}


ALIAS_TABLE: dict[str, AliasSpec] = {
    "compile": AliasSpec("compile", "run"),
    "retrieve": AliasSpec("retrieve", "retrieve"),
    "ledger": AliasSpec("ledger", "init", passthrough_action=True),
    "design-build": AliasSpec("design", "build"),
    "storyboard-propagate": AliasSpec("storyboard", "propagate"),
    "storyboard-compare": AliasSpec("storyboard", "compare"),
    "adapter-build": AliasSpec("adapt", "build"),
    "adapt-provider": AliasSpec("adapt", "provider"),
    "adapt-grok": AliasSpec(
        "adapt", "provider", fixed_flags=("--provider", "grok-imagine")
    ),
    "adapt-flux": AliasSpec("adapt", "provider", fixed_flags=("--provider", "flux")),
    "adapt-sd3": AliasSpec("adapt", "provider", fixed_flags=("--provider", "sd3")),
    "adapt-midjourney": AliasSpec(
        "adapt", "provider", fixed_flags=("--provider", "midjourney")
    ),
    "visual-normalize": AliasSpec("visual", "normalize"),
    "visual-compare": AliasSpec("visual", "compare"),
    "visual-correct": AliasSpec("visual", "correct"),
    "correction-govern": AliasSpec("visual", "govern"),
    "closed-loop-qa": AliasSpec("visual", "closed-loop"),
    "outcome-record": AliasSpec("learn", "outcome"),
    "evolution-propose": AliasSpec("learn", "evolve"),
    "forge-signals": AliasSpec("learn", "forge-signals"),
    "validate-skill": AliasSpec("check", "skill"),
    "validate-corpus": AliasSpec("check", "corpus"),
    "coverage": AliasSpec("check", "coverage"),
    "artifact-validate": AliasSpec("check", "artifact"),
    "repeatability": AliasSpec("check", "repeatability"),
    "eval": AliasSpec("check", "eval"),
    "operator": AliasSpec("operate", "saturation-score", passthrough_action=True),
    "mcp-server": AliasSpec("mcp", "serve"),
    "grok-review-bundle": AliasSpec("bundle", "build"),
}


def all_legacy_commands() -> set[str]:
    """Return the set of legacy top-level command names."""
    return set(ALIAS_TABLE)


# Human sugar: expand to full ``do …`` argv for resolve() (not shell).
RECIPES: dict[str, list[str]] = {
    "storyboard-example": [
        "do",
        "compile",
        "--brief",
        "examples/authority-transfer-storyboard/brief.yaml",
        "--ledger",
        "examples/authority-transfer-storyboard/symbolic-ledger.yaml",
        "--mode",
        "storyboard",
        "--storyboard-plan",
        "examples/authority-transfer-storyboard/storyboard-plan.yaml",
        "--provider",
        "grok-imagine",
        "--out",
        "out/kubrick/authority-transfer",
    ],
    "verify": ["do", "check", "--action", "smoke"],
}


class RouterError(Exception):
    """CLI router failure (unknown intent/action, missing command). Exit code 2."""

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def format_top_level_help() -> str:
    """List the 12 intents only; aliases are not first-class."""
    lines = [
        "usage: kubrick do <intent> [--action <action>] [flags]",
        "",
        "Intents:",
    ]
    for name in sorted(INTENT_REGISTRY):
        lines.append(f"  {name:12} {INTENT_REGISTRY[name].description}")
    lines += [
        "",
        "Sugar:  kubrick recipe <name>",
        "        kubrick help <intent>",
        "        kubrick aliases",
        "Legacy command names still work as aliases.",
    ]
    return "\n".join(lines) + "\n"


def format_intent_help(intent: str) -> str:
    """Actions and routing notes for a single intent."""
    if intent not in INTENT_REGISTRY:
        raise RouterError(
            f"unknown intent {intent!r}; valid intents: {_valid_intents_msg()}"
        )
    spec = INTENT_REGISTRY[intent]
    lines = [
        f"usage: kubrick do {intent} [--action <action>] [flags]",
        f"       kubrick help {intent}",
        "",
        f"{intent}: {spec.description}",
        "",
        "Actions:",
    ]
    for action in sorted(spec.actions):
        script = spec.actions[action]
        default_mark = " (default)" if action == spec.default_action else ""
        lines.append(f"  {action:28} → {script}{default_mark}")
    if intent == "check":
        lines.append(
            "  note: action 'smoke' runs validate_hermes_skill.py then "
            "validate_pattern_corpus.py"
        )
    if intent in {"ledger", "operate"}:
        lines += [
            "",
            "Pass-through: first positional token is the action and is forwarded",
            "as the first argv token to the implementation script.",
            f"  example: kubrick do {intent} <action> [flags]",
        ]
    elif spec.default_action:
        lines += [
            "",
            f"Default action: {spec.default_action}",
        ]
    if intent == "adapt":
        lines += [
            "",
            "Defaults: --provider set → provider; else build.",
            "Provider grok-imagine routes to adapt_grok_imagine.py.",
        ]
    if intent == "visual":
        lines += [
            "",
            "Defaults: --expected and --observation-input → closed-loop;",
            "else --action is required.",
        ]
    if intent == "check":
        lines += [
            "",
            "Default action: smoke (skill + corpus).",
        ]
    lines += [
        "",
        "Legacy aliases: kubrick aliases",
    ]
    return "\n".join(lines) + "\n"


def format_aliases() -> str:
    """Human-readable legacy alias → intent/action map."""
    lines = [
        "Legacy command aliases (soft cutover; prefer kubrick do <intent>):",
        "",
    ]
    for name in sorted(ALIAS_TABLE):
        alias = ALIAS_TABLE[name]
        if alias.passthrough_action:
            target = f"{alias.intent} <subcommand>"
        else:
            target = f"do {alias.intent} --action {alias.action}"
            if alias.fixed_flags:
                target += " " + " ".join(alias.fixed_flags)
        lines.append(f"  {name:24} → {target}")
    lines += [
        "",
        f"{len(ALIAS_TABLE)} aliases. Intents: {', '.join(sorted(INTENT_REGISTRY))}",
    ]
    return "\n".join(lines) + "\n"


def resolve_recipe(name: str) -> list[str]:
    """Expand a recipe name to argv suitable for ``resolve()`` (not a shell line)."""
    if name not in RECIPES:
        raise RouterError(
            f"unknown recipe {name!r}; valid recipes: {', '.join(sorted(RECIPES))}"
        )
    return list(RECIPES[name])


def _flag_value(flags: list[str], name: str) -> str | None:
    """Return the value of ``--flag`` or ``--flag=value`` if present."""
    n = len(flags)
    i = 0
    while i < n:
        tok = flags[i]
        if tok == name and i + 1 < n:
            return flags[i + 1]
        prefix = name + "="
        if tok.startswith(prefix):
            return tok[len(prefix) :]
        i += 1
    return None


def _has_flag(flags: list[str], name: str) -> bool:
    if name in flags:
        return True
    prefix = name + "="
    return any(tok.startswith(prefix) for tok in flags)


def _strip_flag_with_value(flags: list[str], name: str) -> tuple[str | None, list[str]]:
    """Remove the first occurrence of ``--flag value`` or ``--flag=value``."""
    out: list[str] = []
    value: str | None = None
    i = 0
    n = len(flags)
    while i < n:
        tok = flags[i]
        if value is None and tok == name and i + 1 < n:
            value = flags[i + 1]
            i += 2
            continue
        prefix = name + "="
        if value is None and tok.startswith(prefix):
            value = tok[len(prefix) :]
            i += 1
            continue
        out.append(tok)
        i += 1
    return value, out


def _valid_intents_msg() -> str:
    return ", ".join(sorted(INTENT_REGISTRY))


def _valid_actions_msg(intent: str) -> str:
    return ", ".join(sorted(INTENT_REGISTRY[intent].actions))


def _script_for(
    intent: str,
    action: str,
    flags: list[str],
    *,
    legacy_name: str | None = None,
) -> Path:
    """Map intent/action (+ optional legacy name) to implementation script path.

    Locked routing:
    - ``adapt-grok`` alias or ``--provider grok-imagine`` → ``adapt_grok_imagine.py``
    - other provider adapters → ``adapt_provider.py``
    """
    if intent == "adapt" and action == "provider":
        if legacy_name == "adapt-grok" or _flag_value(flags, "--provider") == "grok-imagine":
            return SCRIPTS / "adapt_grok_imagine.py"
        return SCRIPTS / "adapt_provider.py"
    filename = INTENT_REGISTRY[intent].actions[action]
    return SCRIPTS / filename


def _select_default_action(intent: str, flags: list[str], spec: IntentSpec) -> str | None:
    """Apply default-action rules when ``--action`` is omitted."""
    if intent == "adapt":
        if _has_flag(flags, "--provider"):
            return "provider"
        return "build"
    if intent == "visual":
        # v1: closed-loop only when expected + observation-input present
        if _has_flag(flags, "--expected") and _has_flag(flags, "--observation-input"):
            return "closed-loop"
        return None
    if intent == "check":
        return "smoke"
    return spec.default_action


def _resolve_passthrough(
    intent: str,
    rest: list[str],
    *,
    legacy_name: str | None = None,
) -> IntentCall:
    """ledger / operate: first positional is action and stays first in argv."""
    if not rest or rest[0].startswith("-"):
        raise RouterError(
            f"missing subcommand for {intent!r}; valid actions: {_valid_actions_msg(intent)}"
        )
    action = rest[0]
    actions = INTENT_REGISTRY[intent].actions
    if action not in actions:
        raise RouterError(
            f"unknown action {action!r} for intent {intent!r}; "
            f"valid actions: {_valid_actions_msg(intent)}"
        )
    return IntentCall(
        intent=intent,
        action=action,
        script=SCRIPTS / actions[action],
        argv=list(rest),
        legacy_name=legacy_name,
    )


def _resolve_do(argv: list[str]) -> IntentCall:
    """Resolve ``do <intent> …`` (or bare intent as argv[0])."""
    if not argv:
        raise RouterError(
            f"missing intent after 'do'; valid intents: {_valid_intents_msg()}"
        )
    intent = argv[0]
    if intent not in INTENT_REGISTRY:
        raise RouterError(
            f"unknown intent {intent!r}; valid intents: {_valid_intents_msg()}"
        )
    rest = list(argv[1:])
    spec = INTENT_REGISTRY[intent]

    # ledger / operate: subcommand passthrough (first positional = action)
    if intent in {"ledger", "operate"}:
        return _resolve_passthrough(intent, rest)

    action_flag, flags = _strip_flag_with_value(rest, "--action")
    action = action_flag
    if action is None:
        action = _select_default_action(intent, flags, spec)
    if action is None:
        raise RouterError(
            f"missing --action for intent {intent!r}; "
            f"valid actions: {_valid_actions_msg(intent)}"
        )
    if action not in spec.actions:
        raise RouterError(
            f"unknown action {action!r} for intent {intent!r}; "
            f"valid actions: {_valid_actions_msg(intent)}"
        )

    script = _script_for(intent, action, flags)
    argv = list(flags)
    # adapt_grok_imagine.py does not accept --provider; strip if present
    if script.name == "adapt_grok_imagine.py":
        _, argv = _strip_flag_with_value(argv, "--provider")
    return IntentCall(
        intent=intent,
        action=action,
        script=script,
        argv=argv,
        legacy_name=None,
    )


def _resolve_alias(name: str, rest: list[str]) -> IntentCall:
    """Resolve a legacy top-level command name to IntentCall."""
    if name not in ALIAS_TABLE:
        raise RouterError(
            f"unknown command {name!r}; valid intents: {_valid_intents_msg()}"
        )
    alias = ALIAS_TABLE[name]
    intent = alias.intent

    if alias.passthrough_action:
        return _resolve_passthrough(intent, rest, legacy_name=name)

    # adapt-grok keeps the dedicated script path; do not inject --provider
    # (adapt_grok_imagine.py does not accept that flag).
    if name == "adapt-grok":
        return IntentCall(
            intent=intent,
            action=alias.action,
            script=SCRIPTS / "adapt_grok_imagine.py",
            argv=list(rest),
            legacy_name=name,
        )

    flags = list(alias.fixed_flags) + list(rest)
    # Allow optional --action override on aliases; strip router-only flag
    action_flag, flags = _strip_flag_with_value(flags, "--action")
    action = action_flag or alias.action
    if action not in INTENT_REGISTRY[intent].actions:
        raise RouterError(
            f"unknown action {action!r} for intent {intent!r}; "
            f"valid actions: {_valid_actions_msg(intent)}"
        )
    script = _script_for(intent, action, flags, legacy_name=name)
    return IntentCall(
        intent=intent,
        action=action,
        script=script,
        argv=flags,
        legacy_name=name,
    )


def resolve(argv: list[str]) -> IntentCall:
    """Resolve full CLI argv (after program name) to an IntentCall.

    Accepts:
    - ``do <intent> …``
    - bare intent name (same as ``do``)
    - legacy alias names from ``ALIAS_TABLE``
    """
    if not argv:
        raise RouterError("missing command; try: kubrick do <intent> …")
    head = argv[0]
    if head == "do":
        return _resolve_do(argv[1:])
    if head in {"help", "--help", "-h"} and len(argv) == 1:
        # Entrypoint handles help specially.
        raise RouterError("HELP")
    if head in ALIAS_TABLE:
        return _resolve_alias(head, argv[1:])
    if head in INTENT_REGISTRY:
        return _resolve_do(argv)
    raise RouterError(
        f"unknown command {head!r}; valid intents: {_valid_intents_msg()}"
    )
