# Task 006: Multi-Agent Spawn

## Goal
Enable orchestrator to spawn multiple agents simultaneously.

## Description
1. Orchestrator can call delegate_task multiple times
2. Each call spawns a new agent in background
3. Results come back when each finishes
4. No blocking between spawns

## Acceptance Criteria
- [ ] Multiple delegate_task calls work
- [ ] Each spawn is independent
- [ ] Results come back asynchronously
- [ ] No blocking between spawns

## Dependencies
- Task 003 (delegate_task Redesign)
- Task 005 (Session Tracking)
- Existing async delegation infrastructure

## Technical Notes
- Each delegate_task call is independent
- Background dispatch per call
- Results via completion queue
- No dependency between spawns

## Status
pending
