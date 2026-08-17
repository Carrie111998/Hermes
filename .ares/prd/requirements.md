# Requirements

## Mandatory
1. Agent definition files in `~/.hermes/agents/` (global) or `.agents/` (per-project)
2. Frontmatter config: name, model, base_url, provider, api_mode, api_key, reasoning, temperature, top_p, max_tokens, context_length, compression_threshold, compression_target_ratio, tools, skills, max_depth, timeout
3. Body: system prompt (markdown content)
4. Global config: `delegate:` section with agent name → file path mapping
5. Skills stay in `.hermes/skills/` (referenced by name in agent definition)
6. Enhanced `delegate_task` with `agent` parameter (required)
7. Background=True always (no parameter)
8. Notify=True always (no parameter)
9. Session tracking per delegate (new session per delegate)
10. Multi-agent spawn support (orchestrator can spawn multiple agents)
11. Config merging: agent defaults + parent defaults
12. Backward compatibility with existing delegate_task features

## Removed
1. ❌ `background` parameter (always True)
2. ❌ `notify` parameter (always True)

## Nice-to-Have
1. Agent listing: `hermes agent list`
2. Agent validation: `hermes agent validate <name>`
3. Hot-reload: changes to .md files take effect without restart
4. Agent templates: pre-built agent definitions
5. Agent inheritance: base agent + override
