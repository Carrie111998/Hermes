# Session Bridge fixtures

Every provider fixture in this directory is synthetic and exists only for isolated
test coverage. No fixture may contain, reproduce, or be derived from a real Claude,
Codex, Hermes, or other provider transcript.

Tests must copy these fixtures into a temporary directory before modifying them and
must inject all provider roots, databases, tokens, and service clients. A test must
never discover or write through the current user's live provider roots.
