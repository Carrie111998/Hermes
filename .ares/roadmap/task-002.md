# Task 002: Agent Registry

## Goal
Implement agent registry to load, store, and manage agent definitions.

## Description
1. Create `agent/agent_registry.py` module
2. Load agents from config.yaml `delegate:` section
3. Load and parse agent definition files
4. Store in memory registry with lookup by name
5. Provide API for other modules to access agents

## Acceptance Criteria
- [x] Load agents from config.yaml delegate section
- [x] Parse agent definition files
- [x] Provide lookup by agent name
- [x] List all available agents
- [x] Handle missing files gracefully

## Dependencies
- Task 001 (Agent Definition Parser)
- PyYAML for config parsing

## Technical Notes
- Use pathlib for file operations
- Cache parsed definitions
- Thread-safe registry access
- Handle file not found gracefully

## Status
done
