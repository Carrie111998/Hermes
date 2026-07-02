"""Private persistence metadata stamped on live message dicts.

The incremental SessionDB flush (``AIAgent._flush_messages_to_session_db``)
tracks durability directly on the message dicts it writes, so repeated
flushes stay idempotent without positional slices or ``id()``-keyed sets.
These keys are private wire metadata: the API payload build strips every
top-level ``_``-prefixed key before a request leaves the process, and the
JSON session snapshot / context compressor strip them before reusing a
message. Define them ONCE here — every producer (steer injection), consumer
(flush), and stripper (compressor, session log) must agree on the exact
string or markers silently leak or stop being honoured.
"""

# Stamped by the flush on each message dict it has written to state.db.
_DB_PERSISTED_MARKER = "_db_persisted"

# Stamped by intentional post-INSERT content mutations (mid-turn /steer
# appending its marker to an already-flushed tool result) so the next flush
# updates the durable row in place instead of skipping the dict.
_DB_CONTENT_UPDATE_PENDING = "_db_content_update_pending"
