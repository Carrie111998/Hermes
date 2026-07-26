-- Message supersession.
--
-- Rewriting an outreach message previously left the original in place, so the
-- approval queue saw both the stale message and its replacement and could not
-- tell which was current. The frontend worked around this by remembering
-- superseded ids in browser storage, which meant the queue disagreed with
-- itself across devices and was lost whenever storage was cleared.
--
-- The relationship now lives where it belongs: a message points at whatever
-- replaced it. Only pending_approval/qa_failed messages are ever retired, and
-- only once the replacement exists, so a failed rewrite leaves the original
-- reviewable and delivered history is never rewritten.

alter table if exists outreach_messages
  add column if not exists superseded_by text references outreach_messages(id);

comment on column outreach_messages.superseded_by is
  'Message that replaced this one via rewrite. Non-null means hide from the approval queue.';

-- The approval queue filters on this constantly; the partial index keeps that
-- lookup cheap while indexing only the rows that are actually still reviewable.
create index if not exists outreach_messages_pending_review_idx
  on outreach_messages (company_id, status)
  where superseded_by is null;

insert into schema_migrations(version) values ('006_message_supersession')
on conflict (version) do nothing;
