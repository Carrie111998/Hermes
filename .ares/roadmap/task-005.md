# Task 005: Session Tracking

## Goal
Implement session tracking per delegate for clean isolation.

## Description
1. Create new session ID per delegate
2. Track delegation_id → session_id mapping
3. Store session metadata (agent, goal, status, timestamps)
4. Provide API to query delegate sessions
5. Clean up completed sessions

## Acceptance Criteria
- [ ] Each delegate gets a new session ID
- [ ] delegation_id → session_id mapping stored
- [ ] Session metadata tracked
- [ ] Query API works
- [ ] Cleanup works

## Dependencies
- Task 003 (delegate_task Redesign)
- Existing session infrastructure

## Technical Notes
- Use UUID for session IDs
- Store in memory registry
- Thread-safe access
- Auto-cleanup after timeout

## Status
pending
