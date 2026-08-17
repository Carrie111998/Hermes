# Task 003: delegate_task Redesign

## Goal
Redesign delegate_task with agent-first approach, background always, session tracking.

## Description
1. Remove `background` parameter (always True)
2. Remove `notify` parameter (always True)
3. Make `agent` parameter required
4. Session tracking per delegate (new session per delegate)
5. Agent config injection (model, reasoning, temp, tools, skills, prompt)
6. Multi-agent spawn support

## Acceptance Criteria
- [ ] `agent` parameter is required
- [ ] `background` parameter removed (always True)
- [ ] `notify` parameter removed (always True)
- [ ] Each delegate gets a new session
- [ ] Session tracking in registry
- [ ] Agent config injection works
- [ ] Multi-agent spawn works

## Dependencies
- Task 001 (Agent Definition Parser)
- Task 002 (Agent Registry)
- Existing delegate_task infrastructure

## Technical Notes
- Remove `background` and `notify` from signature
- Always dispatch async (background=True)
- Create new session ID per delegate
- Track delegation_id → session_id mapping
- Agent config overrides parent config

## Status
pending
