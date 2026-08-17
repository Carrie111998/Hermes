# Task 007: CLI Commands

## Goal
Implement CLI commands for agent management.

## Description
1. `hermes agent new <name>` — create new agent from template
2. `hermes agent list` — list available agents
3. `hermes agent show <name>` — show agent details
4. `hermes agent validate <name>` — validate agent definition
5. `hermes agent test <name> [prompt]` — one-shot test
6. `hermes agent add <name> <path>` — add existing file
7. `hermes agent remove <name>` — remove agent

## Acceptance Criteria
- [x] `hermes agent new` creates file + adds to config
- [x] `hermes agent list` shows all agents
- [x] `hermes agent show` shows details
- [x] `hermes agent validate` checks validity
- [x] `hermes agent test` runs one-shot test
- [x] `hermes agent add` adds existing file
- [x] `hermes agent remove` removes agent

## Dependencies
- Task 001 (Agent Definition Parser)
- Task 002 (Agent Registry)
- Existing CLI infrastructure

## Technical Notes
- Use argparse for CLI commands
- Integrate with existing hermes CLI structure
- Clear error messages
- Helpful output format

## Status
done
