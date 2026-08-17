# Task 001: Agent Definition Parser

## Goal
Implement markdown file parser for agent definitions with frontmatter support.

## Description
1. Create `agent/agent_definition.py` module
2. Parse YAML frontmatter (name, model, base_url, provider, api_mode, api_key, reasoning, temperature, top_p, max_tokens, context_length, compression_threshold, compression_target_ratio, tools, skills, max_depth, timeout)
3. Extract markdown body as system prompt
4. Validate required fields and types
5. Return structured AgentDefinition object

## Acceptance Criteria
- [x] Parse valid agent definition files
- [x] Extract frontmatter config
- [x] Extract markdown body as system prompt
- [x] Validate required fields
- [x] Handle invalid files gracefully

## Dependencies
- PyYAML for frontmatter parsing
- pathlib for file operations

## Technical Notes
- Use `yaml.safe_load` for frontmatter
- Handle missing frontmatter gracefully
- Support both global and project-level directories
- Cache parsed definitions for performance

## Status
done
