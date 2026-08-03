from tools.registry import registry, tool_error, tool_result

DELEGATE_COMPLETION_SCHEMA = {
    "name": "delegate_completion",
    "description": (
        "One-shot LLM completion for a named, operator-configured task. "
        "Pass only `task` (must be one of the pre-approved task names below) "
        "and `prompt` (what to send). The provider and model for each task "
        "are fixed by the operator in config.yaml — this tool has no "
        "provider/model/base_url parameter and never accepts one. "
        "(task enum rebuilt at every get_definitions() call from "
        "config.yaml's delegate_completion_tasks block.)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "enum": [],
                "description": "Name of a pre-configured task. (rebuilt at get_definitions() time)",
            },
            "prompt": {
                "type": "string",
                "description": "The content to send to the model for this task.",
            },
        },
        "required": ["task", "prompt"],
    },
}

_INTERNAL_ONLY_TASK_NAMES = {"approval", "mcp"}


def _delegate_completion_allowlist() -> list:
    from hermes_cli.config import load_config
    config = load_config()
    names = config.get("delegate_completion_tasks", []) or []
    auxiliary = config.get("auxiliary", {}) or {}
    return [
        n for n in names
        if isinstance(n, str) and n.strip()
        and n not in _INTERNAL_ONLY_TASK_NAMES
        and n in auxiliary
    ]


def check_delegate_completion_requirements() -> bool:
    return len(_delegate_completion_allowlist()) > 0


def _build_delegate_completion_schema_overrides() -> dict:
    allowed = _delegate_completion_allowlist()
    overrides_params = {**DELEGATE_COMPLETION_SCHEMA["parameters"]}
    overrides_params["properties"] = {
        k: dict(v) for k, v in DELEGATE_COMPLETION_SCHEMA["parameters"]["properties"].items()
    }
    overrides_params["properties"]["task"]["enum"] = allowed
    overrides_params["properties"]["task"]["description"] = (
        f"Name of a pre-configured task. Must be one of: {', '.join(allowed)}"
        if allowed else "No delegate_completion_tasks configured — this tool is currently unusable."
    )
    return {"parameters": overrides_params}


def delegate_completion(task: str, prompt: str, **kw) -> str:
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    allowed = _delegate_completion_allowlist()
    if not isinstance(task, str) or task not in allowed:
        return tool_error(
            f"Unknown task '{task}'. Must be one of: {', '.join(allowed) or '(none configured)'}"
        )
    if not isinstance(prompt, str) or not prompt.strip():
        return tool_error("prompt is required")

    try:
        response = call_llm(
            task=task,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        return tool_error(f"delegate_completion task '{task}' failed: {e}")

    text = extract_content_or_reasoning(response)
    return tool_result(task=task, content=text)


registry.register(
    name="delegate_completion",
    toolset="delegate_completion",
    schema=DELEGATE_COMPLETION_SCHEMA,
    handler=lambda args, **kw: delegate_completion(
        task=args.get("task"),
        prompt=args.get("prompt"),
    ),
    check_fn=check_delegate_completion_requirements,
    emoji="🤖",
    dynamic_schema_overrides=_build_delegate_completion_schema_overrides,
)
