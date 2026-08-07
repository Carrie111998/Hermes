# OPENCLAW-RUNVIS-18D: False Idle Cutoff Bug Fix

## Problem Summary

Klaus (the gateway agent) appeared to stop mid-task with:
- No typing/working indicator
- No final message
- UI went falsely idle
- Required follow-up "status?" prompt to recover and finish

**Root Cause**: The stream consumer's `final_response_sent` property was not being set when the stream completed with empty accumulated content or was cancelled during final delivery. This caused the UI to show no activity while the underlying task was still resumable.

## Files Changed

### 1. `gateway/stream_consumer.py`

**Added terminal state tracking to prevent false idle:**

- Added `_StreamTerminalSentinel` class and `_TERMINAL_SENTINEL` sentinel
- Modified `finish()` to queue both `_DONE` and `_TERMINAL_SENTINEL`
- Added `is_terminal_state()` method to detect terminal state
- Modified `final_response_sent` property to also consider terminal state
- Ensured terminal sentinel is consumed before `run()` returns

**Key changes:**
```python
# New sentinel for terminal state
class _StreamTerminalSentinel:
    pass
_TERMINAL_SENTINEL = _StreamTerminalSentinel()

# finish() now queues terminal sentinel
def finish(self) -> None:
    """Signal that the stream is complete."""
    self._queue.put(_DONE)
    self._queue.put(_TERMINAL_SENTINEL)

# is_terminal_state() detects terminal condition
def is_terminal_state(self) -> bool:
    # Checks for _TERMINAL_SENTINEL in queue
    
# final_response_sent considers terminal state
@property
def final_response_sent(self) -> bool:
    return self._final_response_sent or self.is_terminal_state()
```

## Root Cause Analysis

1. **The stream completion path**: When `finish()` is called, it only queued `_DONE`. If the stream was cancelled or `got_done` handling encountered edge cases, `final_response_sent` might not be set.

2. **Empty accumulated content**: If no text was streamed (rare but possible with certain tool outputs), the stream would exit without setting `final_response_sent`.

3. **Timing race**: The typing indicator in `_keep_typing()` stops when the message handler returns, but the stream consumer might still be processing. This created a window where the UI appeared idle but delivery wasn't complete.

## The Fix

1. **Terminal Sentinel**: Adding a terminal sentinel allows the consumer to know definitively when `finish()` was called, even if `_DONE` had processing issues.

2. **Dual confirmation**: `final_response_sent` now considers both:
   - `_final_response_sent`: Set when actual final edit was confirmed
   - `is_terminal_state()`: True when stream reached terminal state

3. **Sentinel consumption**: The `run()` loop now explicitly waits for and consumes the terminal sentinel before returning, ensuring proper cleanup.

## Regression Tests

New test file: `tests/test_stream_consumer_false_idle.py`

12 tests covering:
- Terminal sentinel marking on finish
- Final response sent set regardless of accumulated content
- Stream shows active until terminal
- Multiple tool outputs preserve activity state
- Segment breaks don't trigger false completion
- Exactly-once final delivery (no duplicates)
- Empty accumulated doesn't cause leaks
- Error handling preserves terminal state

## Verification

```bash
# All new tests pass
python -m pytest tests/test_stream_consumer_false_idle.py -v
# 12 passed

# Existing gateway tests still pass
python -m pytest tests/gateway/ -v
# All passing
```

## Commit Message
```
fix(openclaw): prevent false idle cutoff before final task closeout

The stream consumer's final_response_sent property was not being set
when the stream completed with empty content or during cancellation.
This caused the UI to go idle before final delivery was confirmed,
requiring a follow-up prompt to recover.

Changes:
- Add _TERMINAL_SENTINEL for explicit terminal state tracking
- Modify finish() to queue terminal sentinel
- Add is_terminal_state() method for dual confirmation
- Ensure terminal sentinel is consumed before run() returns
- final_response_sent now considers terminal state

Fixes OPENCLAW-RUNVIS-18D
```
