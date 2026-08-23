"""Hermes-native per-turn model routing for Hermes Agent.

This module is deliberately small and declarative.  It reads the Hermes policy
files when explicitly enabled, classifies the incoming user turn locally, and
then uses Hermes' existing runtime-provider and model-switch machinery.  It
never probes provider health and it never mutates conversation history.

The route is applied once per user turn, after the normal Hermes fallback
runtime has been restored and before the system prompt is built.  Provider
failure during the turn is handled by Hermes' existing fallback loop; this
module only supplies the ordered, role-local candidate chain.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DEFAULT_REGISTRY_DIR = Path.home() / ".hermes" / "workflows" / "production-registry"
_HIGH_RISK = re.compile(
    r"投资|金融|财务|法律|医疗|安全|不可逆|删除|生产环境|production|investment|"
    r"financial|legal|medical|security|irreversible|delete|destroy|deploy",
    re.IGNORECASE,
)
_L2 = re.compile(
    r"代码|源码|配置|脚本|文件|目录|仓库|项目|表格|演示|文档|修改|创建|生成|"
    r"执行|运行|安装|调试|重构|多步|分析|code|config|script|file|repo|project|"
    r"spreadsheet|presentation|document|edit|create|generate|run|install|debug|"
    r"refactor|multi[- ]step|analy[sz]e",
    re.IGNORECASE,
)
_DEFAULT_L0 = re.compile(
    r"^(翻译|转换|排序|去重|格式化|提取|计算|translate|convert|sort|dedupe|"
    r"format|extract|calculate)\b",
    re.IGNORECASE,
)
# A pure explanatory / advisory question (asking "why / what is / is this right /
# what's the difference / explain ...") carries no execution intent and should be
# answered directly at L1 — it must not be escalated to L2/L3 DAG just because the
# words "investment", "project", or "analysis" appear in the sentence. Guarded by
# a negative execution-verb check so genuine high-risk / L2 instructions
# ("why ... then delete the file") still route up.
_EXPLANATORY = re.compile(
    r"为什么|为何|如何|怎么|是什么|什么区别|区别是|对不对|对吗|合理吗|是否正确|"
    r"是不是|还是|理解|解释一下|讲讲|说明一下|概念|口径|原因|对吗|为啥|"
    r"\bwhy\b|\bwhat is\b|\bexplain\b|\bdifference\b|\bconcept\b",
    re.IGNORECASE,
)
_EXECUTION_VERB = re.compile(
    r"修改|创建|生成|运行|执行|安装|调试|重构|删除|新建|部署|配置|实现|开发|"
    r"写|改|跑|做|弄|build|create|modify|run|delete|implement|write|edit|"
    r"install|deploy|refactor|generate",
    re.IGNORECASE,
)
_FILE_EDIT_EXT = re.compile(
    r"\.(?:xlsx?|pptx?|docx?|csv|pdf|json|md|txt)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_FILE_EDIT_INTENT = re.compile(
    r"修改|更新|修复|编辑|重写|格式化|润色|重塑|整理|校正|改写|"
    r"modify|update|fix|edit|rewrite|format|revise|restyle|clean",
    re.IGNORECASE,
)
_FILE_DECISION_INTENT = re.compile(
    r"投资建议|投资决策|是否值得|是否可行|该不该投|建议投资|"
    r"investment advice|investment decision|should invest|viability|recommend",
    re.IGNORECASE,
)
_HIGH_RISK_DECISION = re.compile(
    r"投资方案|投资决策|投资建议|分析风险|风险评估|是否值得|是否可行|该不该投|"
    r"financial decision|investment decision|risk assessment|should invest|recommend",
    re.IGNORECASE,
)
_EXPLICIT_WORKFLOW_DIRECTIVE = re.compile(
    r"(?im)(?:^|\s)@?(?:workflow|工作流)\s*[:=]\s*"
    r"(?P<name>local[_-](?:deterministic|visual[_-]extraction))\b"
)
_LOCAL_IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic",
}
_LOCAL_DETERMINISTIC_SYSTEM_MESSAGE = (
    "You are a local deterministic worker. Follow the user's request exactly. "
    "Do not call tools. Return only the requested result."
)
_LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
_RISK_RANK = {"low": 0, "medium": 1, "high": 2}
_WORKFLOW_RANK = {
    "direct": 0,
    "standard": 1,
    "controlled-execution": 2,
    "expert-reviewed": 3,
    "multimodal": 2,
}
_OUTPUT_POLICIES = frozenset({"concise_evidence", "standard", "full_evidence"})
_FULL_EVIDENCE_CONTRACTS = frozenset(
    {
        "independent-review",
        "reviewer",
        "publishing",
        "complex-execution",
        "high-stakes-decision",
        "high-stakes",
    }
)


class RoutingBlockedError(RuntimeError):
    """Raised when a controlled route cannot run under the active policy."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True)
class RouteDecision:
    """A serializable explanation of the route selected for one turn."""

    level: str
    risk: str
    workflow: str
    contract: str
    model_policy: str
    candidates: tuple[dict[str, str], ...] = field(default_factory=tuple)
    selected: dict[str, str] | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "risk": self.risk,
            "workflow": self.workflow,
            "contract": self.contract,
            "model_policy": self.model_policy,
            "candidates": [dict(item) for item in self.candidates],
            "selected": dict(self.selected) if self.selected else None,
            "reason": self.reason,
        }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pragma: no cover - defensive runtime path
        logger.warning("routing layer could not read %s: %s", path, exc)
        return {}
    return value if isinstance(value, dict) else {}


def _text_of(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts: list[str] = []
        for item in message:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)
    return str(message or "")


def _normalize_workflow_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _specialized_workflow_spec(
    route_policy: Mapping[str, Any], workflow_name: Any
) -> dict[str, Any] | None:
    """Resolve a specialized workflow by registry key or template name."""
    target = _normalize_workflow_name(workflow_name)
    if not target:
        return None
    raw = route_policy.get("specialized_workflows")
    specialized = raw if isinstance(raw, Mapping) else {}
    for key, value in specialized.items():
        if not isinstance(value, Mapping):
            continue
        aliases = {
            _normalize_workflow_name(key),
            _normalize_workflow_name(value.get("workflow")),
        }
        if target in aliases:
            return dict(value)
    return None


def _explicit_workflow_name(message: Any) -> str:
    """Read the narrow user-facing ``workflow: NAME`` override syntax."""
    match = _EXPLICIT_WORKFLOW_DIRECTIVE.search(_text_of(message))
    return str(match.group("name")) if match else ""


def _local_image_paths(message: Any) -> list[str]:
    """Recover local image paths retained by text-mode attachment surfaces."""
    text = _text_of(message)
    if not text:
        return []

    candidates: list[str] = []
    try:
        from agent.image_routing import extract_image_refs

        candidates.extend(extract_image_refs(text)[0])
    except Exception:
        pass

    quoted = r"`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'"
    patterns = (
        re.compile(rf"(?im)@image:(?P<value>{quoted}|\S+)"),
        re.compile(
            rf"(?im)(?:image_url|image attached at)\s*:\s*"
            rf"(?P<value>{quoted}|[^\]\n]+)"
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            value = str(match.group("value") or "").strip().rstrip(".,;:!?")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "`\"'":
                value = value[1:-1]
            candidates.append(value)

    resolved: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        path = Path(str(raw)).expanduser()
        if path.suffix.lower() not in _LOCAL_IMAGE_SUFFIXES:
            continue
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        value = str(path)
        if value not in seen:
            seen.add(value)
            resolved.append(value)
    return resolved


def _is_standalone_file_edit(message: Any) -> bool:
    """Detect bounded edits to an explicitly supplied standalone file.

    This is an execution-shape decision, not a risk upgrade: simple file
    edits stay at L1 but use the bounded ``worker`` role instead of the
    top-level responder. Investment advice or other decision requests remain
    outside this specialized path.
    """
    text = _text_of(message).strip()
    if not text:
        return False
    return bool(
        _FILE_EDIT_EXT.search(text)
        and _FILE_EDIT_INTENT.search(text)
        and not _FILE_DECISION_INTENT.search(text)
    )


def _routing_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    value = (config or {}).get("routing")
    return value if isinstance(value, dict) else {}


def resolve_output_policy(contract: str, contract_def: Mapping[str, Any] | None) -> str:
    """Resolve a safe per-turn response style without shaping token limits."""
    if contract in _FULL_EVIDENCE_CONTRACTS:
        return "full_evidence"
    policy = contract_def.get("output_policy") if isinstance(contract_def, Mapping) else None
    return policy if isinstance(policy, str) and policy in _OUTPUT_POLICIES else "standard"


def _config_dir(config: Mapping[str, Any] | None) -> Path:
    raw = _routing_config(config).get("source_dir") or str(_DEFAULT_REGISTRY_DIR)
    return Path(str(raw)).expanduser()


def _load_policy(
    config: Mapping[str, Any] | None,
    bundle: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    if bundle is not None:
        return (
            bundle.get("route_policy", {}),
            bundle.get("capability_contracts", {}),
            bundle.get("model_policies", {}),
            bundle.get("execution_roles", {}),
        )
    directory = _config_dir(config)
    return (
        _read_json(directory / "route-policy.json"),
        _read_json(directory / "capability-contracts.json"),
        _read_json(directory / "model-policies.json"),
        _read_json(directory / "execution-roles.json"),
    )


def _runtime_policy_bundle(agent: Any) -> tuple[Mapping[str, Any] | None, bool]:
    """Return the immutable policy bundle when the runtime exposes one.

    A real Hermes runtime must never fall back to raw policy files after
    registry load failure. Small unit-test fakes without registry attributes
    retain the fixture-based path.
    """
    state = getattr(agent, "runtime_registry", None)
    if state is None:
        return None, False
    status = str(getattr(state, "status", "inactive") or "inactive")
    snapshot = getattr(agent, "runtime_snapshot", None)
    bundle = getattr(snapshot, "bundle", None) if snapshot is not None else None
    if status not in {"active", "candidate"} or not isinstance(bundle, Mapping):
        return None, True
    return bundle, True


def classify_turn(message: Any, route_policy: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> tuple[str, str, str]:
    """Classify one turn without making a model or network call."""
    text = _text_of(message).strip()
    routing_cfg = _routing_config(config)
    if _is_standalone_file_edit(message):
        # Bounded file edits remain L1 but use the worker workflow.
        level, risk = "L1", "low"
    elif _HIGH_RISK.search(text) and _HIGH_RISK_DECISION.search(text):
        level, risk = "L3", "high"
    elif _EXPLANATORY.search(text) and not _EXECUTION_VERB.search(text):
        # Pure "why / explain / is this right" question with no execution intent:
        # answer directly at L1 instead of spinning up the L2/L3 DAG. (A finance
        # IRR-explanation question was being routed to L2 and returning empty when
        # the DAG's auxiliary models all failed.)
        level, risk = "L1", "low"
    elif _HIGH_RISK.search(text):
        level, risk = "L3", "high"
    elif _L2.search(text):
        level, risk = "L2", "medium"
    elif (
        bool(routing_cfg.get("enable_l0", True))
        and len(text) <= int(routing_cfg.get("l0_max_chars", 240))
        and _DEFAULT_L0.search(text)
    ):
        level, risk = "L0", "low"
    else:
        level, risk = "L1", "low"

    workflows = route_policy.get("level_workflows")
    if _is_standalone_file_edit(message):
        workflow = "file-edit"
    else:
        workflow = workflows.get(level) if isinstance(workflows, Mapping) else None
    return level, risk, str(workflow or {"L0": "direct", "L1": "standard", "L2": "controlled-execution", "L3": "expert-reviewed"}[level])


def _is_multimodal(message: Any) -> bool:
    """Detect whether a message contains images or multimodal content."""
    if isinstance(message, list):
        for item in message:
            if isinstance(item, Mapping):
                if item.get("type") == "image" or item.get("type") == "image_url":
                    return True
                if isinstance(item.get("image_url"), Mapping):
                    return True
    return False


def prepare_specialized_message(
    message: Any, decision: RouteDecision | Mapping[str, Any] | None
) -> Any:
    """Preserve pixels when a local visual workflow was selected.

    Native multimodal payloads already carry their image parts unchanged. Text
    mode surfaces retain the local path in an ``image_url:``/``@image:`` hint;
    rehydrate those paths into OpenAI-style image parts before the Ollama call.
    """
    if isinstance(decision, RouteDecision):
        workflow = decision.workflow
    elif isinstance(decision, Mapping):
        workflow = str(decision.get("workflow") or "")
    else:
        return message
    if _normalize_workflow_name(workflow) != "local_visual_extraction":
        return message
    if _is_multimodal(message):
        return message

    image_paths = _local_image_paths(message)
    if not image_paths or not isinstance(message, str):
        return message
    try:
        from agent.image_routing import build_native_content_parts

        parts, _skipped = build_native_content_parts(message, image_paths)
    except Exception:
        logger.warning("local visual workflow could not attach image paths", exc_info=True)
        return message
    if any(isinstance(part, Mapping) and part.get("type") == "image_url" for part in parts):
        logger.info("local visual workflow attached %s local image(s)", len(image_paths))
        return parts
    return message


def specialized_system_message(
    decision: RouteDecision | Mapping[str, Any] | None,
) -> str | None:
    """Return the compact system contract for opt-in local workflows."""
    if isinstance(decision, RouteDecision):
        policy = decision.model_policy
    elif isinstance(decision, Mapping):
        policy = str(decision.get("model_policy") or decision.get("contract") or "")
    else:
        return None
    if policy == "local-deterministic":
        return _LOCAL_DETERMINISTIC_SYSTEM_MESSAGE
    return None


def _should_use_semantic_router(
    message: Any, route_policy: Mapping[str, Any], config: Mapping[str, Any] | None = None
) -> bool:
    """Check whether the semantic router should be invoked for this turn."""
    routing_cfg = _routing_config(config)
    raw_sr = route_policy.get("semantic_router")
    sr: Mapping[str, Any] = raw_sr if isinstance(raw_sr, Mapping) else {}
    if not bool(sr.get("enabled", False)):
        return False
    text = _text_of(message).strip()
    triggers = sr.get("triggers", [])
    if not isinstance(triggers, list):
        return False
    # A pure explanatory / advisory question was already classified L1 by the
    # Fast Gate; the LLM classifier tends to over-escalate it (finance words
    # like "investment"/"IRR" read as high-risk) and spin up the DAG, which can
    # block for minutes and return an empty response. Never re-classify these.
    if _EXPLANATORY.search(text) and not _EXECUTION_VERB.search(text):
        return False
    if _is_standalone_file_edit(message):
        return False
    if "multimodal" in triggers and _is_multimodal(message):
        return True
    if "high_risk" in triggers and _HIGH_RISK.search(text):
        return True
    if "uncertain" in triggers:
        # Uncertain: not clearly L0/L1/L2/L3 by Fast Gate patterns
        is_l0 = _DEFAULT_L0.search(text) and len(text) <= 240
        is_l2 = _L2.search(text)
        is_l3 = _HIGH_RISK.search(text)
        if not is_l0 and not is_l2 and not is_l3:
            return True
    return False


def _conservative_route(
    level: str,
    risk: str,
    workflow: str,
    route_policy: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Apply the registry's risk floor after any semantic-router result.

    The semantic router may refine an uncertain Fast Gate result, but it may
    never downgrade a deterministic high-risk signal or select a workflow
    below the risk gate's minimum. This is the fail-closed boundary between
    probabilistic classification and executable policy.
    """

    level = level if level in _LEVEL_RANK else "L1"
    risk = risk.lower() if risk.lower() in _RISK_RANK else "low"
    workflow = str(workflow or "standard")
    gate_map = route_policy.get("risk_gates")
    gates = gate_map if isinstance(gate_map, Mapping) else {}
    gate = gates.get(risk) if isinstance(gates.get(risk), Mapping) else {}
    minimum = str(gate.get("min_workflow") or "")
    if minimum and _WORKFLOW_RANK.get(workflow, 0) < _WORKFLOW_RANK.get(minimum, 0):
        workflow = minimum
    if risk == "high":
        level = "L3"
        workflow = "expert-reviewed"
    elif risk == "medium":
        level = max((level, "L2"), key=lambda item: _LEVEL_RANK.get(item, 0))
        if _WORKFLOW_RANK.get(workflow, 0) < _WORKFLOW_RANK["controlled-execution"]:
            workflow = "controlled-execution"
    return level, risk, workflow


def _semantic_classify(
    message: Any,
    config: Mapping[str, Any] | None = None,
    bundle: Mapping[str, Any] | None = None,
) -> tuple[str, str, str] | None:
    """Run the remote semantic classifier (routing-classifier policy).

    Called only when the Fast Gate cannot determine the route with confidence.
    On failure, returns None so the caller falls back to the Fast Gate result.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly()
        except Exception:
            config = {}
    directory = _config_dir(config)
    routing_cfg = _routing_config(config)

    # Load the routing-classifier policy
    model_policies = (
        bundle.get("model_policies", {})
        if bundle is not None
        else _read_json(directory / "model-policies.json")
    )
    policies = model_policies.get("policies") if isinstance(model_policies.get("policies"), Mapping) else {}
    classifier_policy = policies.get("routing-classifier")
    if not isinstance(classifier_policy, Mapping):
        return None

    provider_map = routing_cfg.get("provider_map")
    provider_map = provider_map if isinstance(provider_map, Mapping) else {}
    candidates = [
        candidate
        for model_id in _candidate_ids(classifier_policy)
        for candidate in [_split_model_id(model_id, provider_map)]
        if candidate is not None
    ]
    if not candidates:
        return None

    # Load the semantic router prompt
    if bundle is not None:
        prompt = str(bundle.get("semantic_router_prompt") or "")
    else:
        prompt = ""
        prompt_path = directory / "semantic-router-prompt.md"
        try:
            prompt = prompt_path.read_text(encoding="utf-8")
        except Exception:
            return None

    user_text = _text_of(message)[:12000]
    full_prompt = f"{prompt}\n\nUSER REQUEST:\n{user_text}"

    route_policy = (
        bundle.get("route_policy", {})
        if bundle is not None
        else _read_json(directory / "route-policy.json")
    )
    sr_cfg = route_policy.get("semantic_router")
    sr_cfg_map: Mapping[str, Any] = sr_cfg if isinstance(sr_cfg, Mapping) else {}
    timeout = float(sr_cfg_map.get("timeout_ms", 6000)) / 1000.0

    failures: list[str] = []
    max_calls = 1
    try:
        max_calls = max(1, int(sr_cfg_map.get("max_calls_per_task", 1)))
    except (TypeError, ValueError):
        max_calls = 1

    for candidate in candidates[:max_calls]:
        try:
            from agent.auxiliary_client import call_llm

            response = call_llm(
                task="semantic_router",
                provider=candidate["provider"],
                model=candidate["model"],
                messages=[{"role": "user", "content": full_prompt}],
                temperature=0.0,
                max_tokens=512,
                timeout=timeout,
                route_info={},
            )
            choices = getattr(response, "choices", None) or []
            if not choices:
                failures.append(f"{candidate['provider']}/{candidate['model']}: empty")
                continue
            content = str(getattr(getattr(choices[0], "message", None), "content", "") or "")
            if not content.strip():
                failures.append(f"{candidate['provider']}/{candidate['model']}: empty_content")
                continue

            # Parse JSON response
            import re as _re
            json_match = _re.search(r"\{[^{}]*\}", content, _re.DOTALL)
            if not json_match:
                failures.append(f"{candidate['provider']}/{candidate['model']}: no_json")
                continue
            result = json.loads(json_match.group(0))
            level = str(result.get("level") or "").strip()
            risk = str(result.get("risk") or "").strip()
            workflow = str(result.get("workflow") or "").strip()
            if level not in ("L0", "L1", "L2", "L3"):
                failures.append(f"{candidate['provider']}/{candidate['model']}: invalid_level")
                continue
            logger.info(
                "Hermes semantic router: level=%s risk=%s workflow=%s via %s/%s",
                level, risk, workflow, candidate["provider"], candidate["model"],
            )
            return level, risk, workflow
        except Exception as exc:
            failures.append(f"{candidate['provider']}/{candidate['model']}: {type(exc).__name__}")

    logger.warning("Hermes semantic router exhausted all candidates: %s", "; ".join(failures))
    return None


def _role_policy_name(role: str, execution_roles: Mapping[str, Any]) -> str:
    """Resolve a role to its model_policy from execution-roles.json.
    
    Falls back to the hardcoded mapping for backwards compatibility
    when execution_roles is not available.
    """
    roles_map = execution_roles.get("roles") if isinstance(execution_roles.get("roles"), Mapping) else {}
    role_def = roles_map.get(role) if isinstance(roles_map, Mapping) else None
    if isinstance(role_def, Mapping) and role_def.get("model_policy"):
        return str(role_def["model_policy"])
    # Hardcoded fallback matching Hermes' canonical execution-roles.json
    return _HARDCODED_ROLE_POLICY.get(role, "complex-execution")


_HARDCODED_ROLE_POLICY: dict[str, str] = {
    "responder": "route",
    "worker": "fast-economy",
    "worker-pro": "complex-execution",
    "planner": "complex-execution",
    "explorer": "multimodal-extraction",
    "publisher": "publishing",
    "reviewer": "independent-review",
    "local-worker": "local-deterministic",
    "local-extractor": "local-deterministic",
}


def _load_profiles(
    config: Mapping[str, Any] | None,
    bundle: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    if bundle is not None:
        return bundle.get("model_profiles", {})
    directory = _config_dir(config)
    return _read_json(directory / "model-profiles.json")


def resolve_thinking(
    contract_name: str,
    model_id: str,
    *,
    contracts: Mapping[str, Any],
    profiles: Mapping[str, Any],
) -> str:
    """Resolve reasoning_effort through Hermes' thinking_map chain.

    contract → reasoning_intent (capability-contracts.json)
    model → thinking_map (model-profiles.json)
    thinking_map[intent] → actual reasoning_effort

    Returns the resolved reasoning level (off/low/medium/high/max), or
    an empty string when the profile is unavailable.
    """
    contracts_map = contracts.get("contracts") if isinstance(contracts.get("contracts"), Mapping) else {}
    contract = contracts_map.get(contract_name)
    if not isinstance(contract, Mapping):
        return ""
    intent = str(contract.get("reasoning_intent") or "off").strip()
    if not intent:
        return ""

    profiles_map = profiles.get("profiles") if isinstance(profiles.get("profiles"), Mapping) else {}
    profile = profiles_map.get(model_id)
    if not isinstance(profile, Mapping):
        # Try to match by stripping known provider prefix
        for key, value in profiles_map.items():
            if isinstance(value, Mapping) and key.endswith("/" + model_id.rsplit("/", 1)[-1] if "/" in model_id else model_id):
                profile = value
                break
    if not isinstance(profile, Mapping):
        return ""

    thinking_map = profile.get("thinking_map")
    if not isinstance(thinking_map, Mapping):
        return ""

    resolved = thinking_map.get(intent)
    if isinstance(resolved, str) and resolved.strip():
        return resolved.strip()

    # Hermes decision C: if the provider only supports one legal level, use it.
    supported = profile.get("supported_reasoning")
    if isinstance(supported, list) and len(supported) == 1 and isinstance(supported[0], str):
        return supported[0]

    return ""


def get_role_candidates(
    role: str,
    config: Mapping[str, Any] | None = None,
    output_vendor_family: str | None = None,
    bundle: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Resolve a execution role to its ordered model candidate chain.

    This is the public bridge between Hermes' role-based model selection and
    Hermes' delegation / subagent system.  Call it from any delegation
    path to get the correct model candidates for a given role.

    Args:
        role: execution role name (e.g. "worker-pro", "planner", "reviewer").
        config: Optional config dict (loads from disk if None).
        output_vendor_family: Required for "reviewer" role — the vendor family
            of the output being reviewed (e.g. "deepseek", "openai").

    Returns:
        Ordered list of {provider, model} candidates, or empty list.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            config = {}
    routing_cfg = _routing_config(config)
    directory = _config_dir(config)
    execution_roles = (
        bundle.get("execution_roles", {})
        if bundle is not None
        else _read_json(directory / "execution-roles.json")
    )
    policy_name = _role_policy_name(role, execution_roles)
    if policy_name == "route":
        return []  # "route" means follow the top-level contract, not a fixed policy

    model_policies = (
        bundle.get("model_policies", {})
        if bundle is not None
        else _read_json(directory / "model-policies.json")
    )
    policies = model_policies.get("policies") if isinstance(model_policies.get("policies"), Mapping) else {}
    policy = policies.get(policy_name)
    if not isinstance(policy, Mapping):
        return []

    provider_map = routing_cfg.get("provider_map")
    provider_map = provider_map if isinstance(provider_map, Mapping) else {}

    # Reviewer: cross-vendor candidates resolved from rules[output_vendor_family]
    if role == "reviewer" and output_vendor_family:
        rules = policy.get("rules") if isinstance(policy.get("rules"), Mapping) else {}
        model_ids = rules.get(output_vendor_family, [])
        if isinstance(model_ids, list):
            return [
                candidate
                for model_id in model_ids
                for candidate in [_split_model_id(str(model_id), provider_map)]
                if candidate is not None
            ]
        return []

    return [
        candidate
        for model_id in _candidate_ids(policy)
        for candidate in [_split_model_id(model_id, provider_map)]
        if candidate is not None
    ]


def _candidate_ids(policy: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("primary_pool", "primary", "intermediate_failover", "soft_failover", "hard_failover", "failover"):
        raw = policy.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, Iterable) or isinstance(raw, (bytes, str, Mapping)):
            continue
        for value in raw:
            if isinstance(value, str) and value.strip() and value not in values:
                values.append(value.strip())
    return values


def _split_model_id(model_id: str, provider_map: Mapping[str, Any]) -> dict[str, str] | None:
    if "/" not in model_id:
        return None
    provider, model = model_id.split("/", 1)
    provider = str(provider_map.get(provider, provider)).strip()
    model = model.strip()
    if not provider or not model:
        return None
    return {"provider": provider, "model": model}


def _build_decision(
    message: Any,
    config: Mapping[str, Any] | None,
    bundle: Mapping[str, Any] | None = None,
) -> RouteDecision | None:
    route_policy, contracts, model_policies, _execution_roles = _load_policy(config, bundle)
    if not route_policy or not model_policies:
        return None

    specialized = _specialized_workflow_spec(
        route_policy, _explicit_workflow_name(message)
    )
    route_reason = ""
    if specialized is not None:
        level = str(specialized.get("level") or "L0")
        risk = str(specialized.get("risk") or "low")
        workflow = str(specialized.get("workflow") or "direct")
        route_reason = "explicit_specialized_workflow"
    else:
        level, risk, workflow = classify_turn(message, route_policy, config)
        fast_level, fast_risk = level, risk

        # Hermes semantic router: when the Fast Gate is uncertain, multimodal,
        # or high-risk, call the classifier for a more accurate route. A
        # registry-declared specialized workflow selects its own contract;
        # ordinary L0-L3 output continues through level_contracts unchanged.
        if _should_use_semantic_router(message, route_policy, config):
            try:
                sr_result = _semantic_classify(message, config, bundle)
                if sr_result is not None:
                    sr_level, sr_risk, sr_workflow = sr_result
                    if (sr_level, sr_risk, sr_workflow) != (level, risk, workflow):
                        logger.info(
                            "Hermes semantic router override: %s/%s/%s -> %s/%s/%s",
                            level, risk, workflow, sr_level, sr_risk, sr_workflow,
                        )
                    semantic_specialized = _specialized_workflow_spec(
                        route_policy, sr_workflow
                    )
                    if semantic_specialized is not None:
                        specialized = semantic_specialized
                        semantic_level = str(specialized.get("level") or sr_level or "L0")
                        semantic_risk = str(specialized.get("risk") or sr_risk or "low")
                        semantic_workflow = str(
                            specialized.get("workflow") or sr_workflow or "direct"
                        )
                        level, risk, workflow = _conservative_route(
                            max(
                                (fast_level, semantic_level),
                                key=lambda item: _LEVEL_RANK.get(item, 0),
                            ),
                            max(
                                (fast_risk, semantic_risk),
                                key=lambda item: _RISK_RANK.get(str(item).lower(), 0),
                            ),
                            semantic_workflow,
                            route_policy,
                        )
                        route_reason = "semantic_specialized_workflow"
                    else:
                        level, risk, workflow = _conservative_route(
                            max(
                                (level, sr_level),
                                key=lambda item: _LEVEL_RANK.get(item, 0),
                            ),
                            max(
                                (risk, sr_risk),
                                key=lambda item: _RISK_RANK.get(str(item).lower(), 0),
                            ),
                            sr_workflow,
                            route_policy,
                        )
            except Exception:
                logger.info("Hermes semantic router failed, falling back to Fast Gate")

    contracts_map = contracts.get("contracts") if isinstance(contracts.get("contracts"), Mapping) else {}
    use_specialized_contract = bool(
        specialized is not None
        and str(specialized.get("level") or "L0") == level
        and str(specialized.get("risk") or "low").lower() == risk.lower()
    )
    if use_specialized_contract:
        contract = str(specialized.get("contract") or "")
    elif _is_standalone_file_edit(message):
        # L1 file editing uses the bounded worker contract (MiniMax primary),
        # not the responder's standard-reasoning contract.
        contract = "fast-economy"
    else:
        level_contracts = route_policy.get("level_contracts")
        contract = str(level_contracts.get(level) if isinstance(level_contracts, Mapping) else "")
        if not contract:
            contract = {"L0": "fast-economy", "L1": "standard-reasoning", "L2": "complex-execution", "L3": "high-stakes-decision"}[level]
    if contract not in contracts_map:
        logger.warning("routing layer contract %r is not declared", contract)
    policies = model_policies.get("policies")
    policy = policies.get(contract) if isinstance(policies, Mapping) else None
    if not isinstance(policy, Mapping):
        return RouteDecision(level, risk, workflow, contract, contract, reason="missing_model_policy")
    provider_map = _routing_config(config).get("provider_map")
    provider_map = provider_map if isinstance(provider_map, Mapping) else {}
    candidates = tuple(
        candidate
        for model_id in _candidate_ids(policy)
        for candidate in [_split_model_id(model_id, provider_map)]
        if candidate is not None
    )
    return RouteDecision(
        level=level,
        risk=risk,
        workflow=workflow,
        contract=contract,
        model_policy=contract,
        candidates=candidates,
        reason=route_reason or ("high_risk" if level == "L3" else "complex_execution" if level == "L2" else "deterministic_transform" if level == "L0" else "ordinary_turn"),
    )


def _runtime_for(candidate: Mapping[str, str]) -> dict[str, Any]:
    from hermes_cli.runtime_provider import resolve_runtime_provider

    return resolve_runtime_provider(
        requested=candidate["provider"],
        target_model=candidate["model"],
    )


def _provider_family(model_id: str, profiles: Mapping[str, Any]) -> str:
    profile_map = profiles.get("profiles") if isinstance(profiles.get("profiles"), Mapping) else {}
    profile = profile_map.get(model_id) if isinstance(profile_map, Mapping) else None
    if isinstance(profile, Mapping):
        family = str(profile.get("vendor_family") or "").strip()
        if family:
            return family
    return model_id.split("/", 1)[0].strip()


def review_high_risk_result(
    route: Mapping[str, Any] | None,
    *,
    user_message: Any,
    answer: str,
    config: Mapping[str, Any] | None = None,
    bundle: Mapping[str, Any] | None = None,
    required: bool = False,
) -> dict[str, Any] | None:
    """Run the Hermes L3 reviewer synchronously, returning bounded evidence.

    Review is intentionally separate from Hermes' background memory review:
    this call is a release gate for high-risk answers, not a best-effort
    housekeeping fork.  It is opt-in under ``routing.review.enabled``.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            config = {}
    routing_cfg = _routing_config(config)
    raw_review_cfg = routing_cfg.get("review")
    review_cfg: Mapping[str, Any] = raw_review_cfg if isinstance(raw_review_cfg, Mapping) else {}
    if not bool(review_cfg.get("enabled", False)) and not required:
        return None
    if not isinstance(route, Mapping) or route.get("level") != "L3":
        return None

    directory = _config_dir(config)
    policies = (
        bundle.get("model_policies", {})
        if bundle is not None
        else _read_json(directory / "model-policies.json")
    )
    profiles = (
        bundle.get("model_profiles", {})
        if bundle is not None
        else _read_json(directory / "model-profiles.json")
    )
    review_policy = (policies.get("policies") or {}).get("independent-review")
    raw_selected = route.get("selected")
    selected: Mapping[str, Any] = raw_selected if isinstance(raw_selected, Mapping) else {}
    output_model_id = f"{selected.get('provider', '')}/{selected.get('model', '')}".strip("/")
    output_family = _provider_family(output_model_id, profiles)
    rules = review_policy.get("rules") if isinstance(review_policy, Mapping) else {}
    model_ids = rules.get(output_family, []) if isinstance(rules, Mapping) else []
    raw_provider_map = routing_cfg.get("provider_map")
    provider_map: Mapping[str, Any] = raw_provider_map if isinstance(raw_provider_map, Mapping) else {}
    candidates = [
        candidate
        for model_id in model_ids
        for candidate in [_split_model_id(str(model_id), provider_map)]
        if candidate is not None
    ]
    if not candidates:
        return {
            "status": "blocked",
            "verdict": None,
            "reason": "no_cross_vendor_reviewer_candidate",
            "output_vendor_family": output_family,
        }

    prompt = (
        "You are an independent reviewer for a high-risk answer. Review the "
        "candidate answer against the user request. Return exactly one first-line "
        "verdict: PASS or REVISE. Then give concise, evidence-based reasons. "
        "Do not call tools, do not rewrite the full answer, and do not add a "
        "verdict other than PASS or REVISE.\n\n"
        f"USER REQUEST:\n{_text_of(user_message)[:12000]}\n\n"
        f"CANDIDATE ANSWER:\n{str(answer)[:24000]}"
    )
    failures: list[str] = []
    for candidate in candidates:
        try:
            from agent.auxiliary_client import call_llm

            response = call_llm(
                task="review",
                provider=candidate["provider"],
                model=candidate["model"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=int(review_cfg.get("max_tokens", 1200)),
                timeout=float(review_cfg.get("timeout", 90)),
                route_info={},
            )
            content = ""
            choices = getattr(response, "choices", None) or []
            if choices:
                content = str(getattr(getattr(choices[0], "message", None), "content", "") or "")
            verdict_match = re.match(r"^\s*(PASS|REVISE)\b", content, re.IGNORECASE)
            if not verdict_match:
                failures.append(f"{candidate['provider']}/{candidate['model']}: invalid_verdict")
                continue
            verdict = verdict_match.group(1).upper()
            return {
                "status": "passed" if verdict == "PASS" else "revise",
                "verdict": verdict,
                "provider": candidate["provider"],
                "model": candidate["model"],
                "output_vendor_family": output_family,
                "review": content[:8000],
                "degraded": False,
            }
        except Exception as exc:  # pragma: no cover - provider-specific errors
            failures.append(f"{candidate['provider']}/{candidate['model']}: {type(exc).__name__}")

    return {
        "status": "blocked",
        "verdict": None,
        "reason": "cross_vendor_review_unavailable",
        "output_vendor_family": output_family,
        "attempts": failures,
    }


def review_fail_closed(config: Mapping[str, Any] | None = None) -> bool:
    """Return the configured L3 review gate posture (default: fail closed)."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            config = {}
    routing_cfg = _routing_config(config)
    raw_review_cfg = routing_cfg.get("review")
    review_cfg: Mapping[str, Any] = raw_review_cfg if isinstance(raw_review_cfg, Mapping) else {}
    return bool(review_cfg.get("fail_closed", True))


def enforce_release_gates(
    agent: Any,
    *,
    user_message: Any,
    answer: str | None,
    config: Mapping[str, Any] | None = None,
) -> str | None:
    """Enforce registry-declared review and verification before delivery."""

    if answer is None:
        return answer
    route = getattr(agent, "_route_decision", None)
    if not isinstance(route, Mapping):
        return answer
    if config is None:
        config = getattr(agent, "_route_config", None)
    if not isinstance(config, Mapping):
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            config = {}

    if bool(getattr(agent, "_workflow_review", False)):
        evidence = review_high_risk_result(
            route,
            user_message=user_message,
            answer=answer,
            config=config,
            bundle=getattr(agent, "_route_policy_bundle", None),
            required=True,
        )
        agent._review_evidence = evidence
        if not evidence or evidence.get("status") != "passed":
            reason = (evidence or {}).get("reason", "review_unavailable")
            return (
                "Delivery blocked: the routed high-risk workflow did not pass its "
                f"independent review gate ({reason})."
            )

    if bool(getattr(agent, "_workflow_verify", False)):
        changed = sorted(getattr(agent, "_turn_file_mutation_paths", set()) or [])
        if changed:
            try:
                from agent.verification_evidence import verification_status

                status = verification_status(
                    session_id=getattr(agent, "session_id", None),
                    cwd=getattr(agent, "terminal_cwd", None) or config.get("terminal", {}).get("cwd"),
                )
            except Exception:
                status = {"status": "unverified"}
            if status.get("status") in {"unverified", "stale", "failed"}:
                return (
                    "Delivery blocked: the routed workflow changed files but has "
                    "no fresh passing verification evidence."
                )
    return answer


def apply_route(agent: Any, message: Any, config: Mapping[str, Any] | None = None) -> RouteDecision | None:
    """Apply one route to a live AIAgent, returning its evidence record."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            config = {}
    routing_cfg = _routing_config(config)
    policy_bundle, registry_bound = _runtime_policy_bundle(agent)
    agent._route_policy_bundle = policy_bundle
    agent._route_config = config
    if not hasattr(agent, "_route_base_cached_system_prompt"):
        agent._route_base_cached_system_prompt = getattr(
            agent, "_cached_system_prompt", None
        )
        agent._route_base_cached_system_prompt_static = getattr(
            agent, "_cached_system_prompt_static", None
        )
    # Restore Hermes' native prompt baseline before applying a specialized
    # local contract for this turn. The gateway reuses the agent object.
    agent._cached_system_prompt = getattr(
        agent, "_route_base_cached_system_prompt", None
    )
    agent._cached_system_prompt_static = getattr(
        agent, "_route_base_cached_system_prompt_static", None
    )
    # Per-turn request shaping; a cached agent must not carry a prior local
    # workflow's no-tools contract into the next cloud turn.
    agent._route_disable_tools = False
    agent._route_output_policy = None
    agent._route_decision = None
    agent._review_evidence = None
    agent._workflow_dag_evidence = None
    # The gateway reuses one AIAgent across turns. Clear coordinated-workflow
    # guards and role metadata before classifying the new turn so an earlier
    # L2/L3 request cannot constrain a later direct L0/L1 request.
    agent._direct_execution_guard = False
    agent._direct_execution_guard_tools = set()
    agent._workflow_roles = []
    agent._workflow_verify = False
    agent._workflow_review = False
    agent._route_delegate_role = None
    if not bool(routing_cfg.get("enabled", False)):
        return None
    # A cached gateway agent survives multiple turns; reviewer evidence belongs
    # to exactly one turn and must never be carried into the next result.
    agent._review_evidence = None
    agent._workflow_dag_evidence = None

    if registry_bound and policy_bundle is None:
        raise RoutingBlockedError(
            "runtime_registry_inactive",
            "Route blocked because the Hermes runtime registry is inactive.",
        )

    decision = _build_decision(message, config, policy_bundle)
    if decision is None:
        raise RoutingBlockedError(
            "route_policy_unavailable",
            "Route blocked because the active registry has no usable route policy.",
        )

    contracts = _load_policy(config, policy_bundle)[1]
    contracts_map = contracts.get("contracts") if isinstance(contracts.get("contracts"), Mapping) else {}
    contract_def = contracts_map.get(decision.contract) if isinstance(contracts_map, Mapping) else None
    agent._route_output_policy = resolve_output_policy(
        decision.contract,
        contract_def if isinstance(contract_def, Mapping) else None,
    )
    agent._route_disable_tools = bool(
        isinstance(contract_def, Mapping) and contract_def.get("tools") == "none"
    )

    # Load workflow templates and store the role sequence for this route.
    # L1 standalone file edits use the bounded worker role; ordinary L1 turns
    # remain on the top-level responder path.
    try:
        directory = _config_dir(config)
        templates = (
            policy_bundle.get("workflow_templates", {})
            if policy_bundle is not None
            else _read_json(directory / "workflow-templates.json")
        )
        templates_map = templates.get("templates") if isinstance(templates.get("templates"), Mapping) else {}
        workflow_name = decision.workflow or {"L2": "controlled-execution", "L3": "expert-reviewed"}.get(decision.level, "standard")
        template = templates_map.get(workflow_name)
        agent._workflow_roles = list(template.get("roles", [])) if isinstance(template, Mapping) else []
        agent._workflow_verify = bool(template.get("verify", False)) if isinstance(template, Mapping) else False
        agent._workflow_review = bool(template.get("review_required", False)) if isinstance(template, Mapping) else False
    except Exception:
        agent._workflow_roles = []
        agent._workflow_verify = False
        agent._workflow_review = False

    if not decision.candidates:
        agent._route_decision = decision.as_dict()
        logger.warning("routing layer has no usable candidates for policy %s", decision.model_policy)
        raise RoutingBlockedError(
            "route_candidates_unavailable",
            f"Route blocked because policy {decision.model_policy!r} has no usable model candidates.",
        )

    # L2/L3 are coordinated workflows: the top-level responder may reason,
    # delegate, and synthesize, but must not execute project tools directly.
    # Worker/worker-pro subagents remain allowed to execute their assigned steps.
    agent._direct_execution_guard = decision.level in {"L2", "L3"}
    agent._direct_execution_guard_tools = (
        {
            "read_file", "search_files", "write_file", "patch", "execute_code", "terminal",
            "computer_use", "process", "open_preview", "close_preview", "project_create",
            "project_switch", "project_list",
        }
        if agent._direct_execution_guard
        else set()
    )
    agent._route_delegate_role = "worker-pro" if decision.level == "L3" else "worker"

    selected: dict[str, str] | None = None
    runtime: dict[str, Any] | None = None
    for candidate in decision.candidates:
        try:
            runtime = _runtime_for(candidate)
            selected = dict(candidate)
            break
        except Exception as exc:
            logger.info(
                "routing layer candidate unavailable: %s/%s (%s)",
                candidate["provider"],
                candidate["model"],
                exc,
            )

    if selected is None or runtime is None:
        agent._route_decision = decision.as_dict()
        logger.warning("routing layer exhausted candidates for policy %s", decision.model_policy)
        raise RoutingBlockedError(
            "route_candidates_unavailable",
            f"Route blocked because all candidates for policy {decision.model_policy!r} failed runtime resolution.",
        )

    current = {
        "provider": str(getattr(agent, "provider", "") or ""),
        "model": str(getattr(agent, "model", "") or ""),
    }
    if current != selected:
        agent.switch_model(
            selected["model"],
            runtime.get("provider") or selected["provider"],
            runtime.get("api_key") or "",
            runtime.get("base_url") or "",
            runtime.get("api_mode") or "",
        )

    # Resolve local Ollama's request context from the selected registry profile.
    # switch_model() normally discovers this only when Ollama was the session's
    # startup provider; a per-turn route switch would otherwise omit num_ctx and
    # Ollama would fall back to 4096, which cannot carry a normal image turn.
    model_id = f"{selected['provider']}/{selected['model']}"
    profiles = _load_profiles(config, policy_bundle)
    if selected["provider"] == "ollama":
        profile_map = profiles.get("profiles") if isinstance(profiles.get("profiles"), Mapping) else {}
        profile = profile_map.get(model_id) if isinstance(profile_map, Mapping) else None
        raw_context = profile.get("context_window") if isinstance(profile, Mapping) else None
        try:
            local_context = int(raw_context) if isinstance(raw_context, (int, str)) else 0
        except ValueError:
            local_context = 0
        if local_context > 0:
            agent._ollama_num_ctx = local_context
            logger.info("local route will request Ollama num_ctx=%s", local_context)

    # Resolve reasoning_effort through Hermes' thinking_map chain.
    # contract → reasoning_intent → model.thinking_map → actual effort.
    try:
        contracts = _load_policy(config, policy_bundle)[1]
        resolved_effort = resolve_thinking(
            decision.contract,
            model_id,
            contracts=contracts,
            profiles=profiles,
        )
        if resolved_effort and hasattr(agent, "reasoning_effort"):
            agent.reasoning_effort = resolved_effort
            if (
                resolved_effort == "off"
                and decision.model_policy == "local-deterministic"
                and hasattr(agent, "reasoning_config")
            ):
                agent.reasoning_config = {"enabled": False}
            logger.info(
                "thinking resolution resolved: contract=%s intent→%s model=%s",
                decision.contract,
                resolved_effort,
                model_id,
            )
    except Exception:
        pass

    # Reuse Hermes' existing real-failure fallback machinery for this route.
    # Entries carry no credentials; the host resolves them only if a real
    # provider failure causes a transition.
    fallback_chain = [dict(item) for item in decision.candidates if item != selected]
    agent._fallback_chain = fallback_chain
    agent._fallback_index = 0
    agent._fallback_activated = False
    agent._fallback_model = fallback_chain[0] if fallback_chain else None
    agent._unavailable_fallback_keys = set()

    applied = RouteDecision(
        level=decision.level,
        risk=decision.risk,
        workflow=decision.workflow,
        contract=decision.contract,
        model_policy=decision.model_policy,
        candidates=decision.candidates,
        selected=selected,
        reason=decision.reason,
    )
    agent._route_decision = applied.as_dict()
    logger.info(
        "route selected: level=%s risk=%s workflow=%s policy=%s model=%s/%s candidates=%s",
        applied.level,
        applied.risk,
        applied.workflow,
        applied.model_policy,
        selected["provider"],
        selected["model"],
        len(applied.candidates),
    )
    return applied
