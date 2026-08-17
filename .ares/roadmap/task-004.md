# Task 004: Skills Integration

## Goal
Integrate skills from .hermes/skills/ with agent definitions.

## Description
1. Load skills by name from .hermes/skills/
2. Apply skills to subagent spawned by delegate_task
3. Handle missing skills gracefully
4. Maintain backward compatibility

## Acceptance Criteria
- [ ] Load skills by name from .hermes/skills/
- [ ] Apply skills to subagent
- [ ] Handle missing skills gracefully
- [ ] Backward compatible

## Dependencies
- Task 003 (delegate_task Redesign)
- Existing skills infrastructure

## Technical Notes
- Use existing skill loading mechanisms
- Handle missing skills gracefully (warn, don't fail)
- Cache loaded skills for performance

## Status
pending
