-- Precomputed match text for candidate selection.
--
-- `select` reads a country's corpus rows, JSON-decodes each one, rebuilds the
-- haystack a product term matches against, and folds it — on every run. The
-- corpus is immutable, so all of that produces the same string every time. Ten
-- campaigns into Poland did the work ten times, and the work is the expensive
-- part: normalize_name runs an NFKD decomposition per field per row.
--
-- Written at import instead. NULL is a corpus imported before this column
-- existed: selection falls back to computing the value, so nothing breaks
-- without a backfill and an old corpus simply does not get the speedup until it
-- is re-imported or backfilled.
--
-- No index. The filter is a leading-wildcard LIKE, which no btree can serve,
-- and a trigram index on a table this size would cost more to maintain than the
-- scan costs to run. The win here is not doing the work, not finding the row
-- faster.
alter table candidate_records add column if not exists search_text text;

insert into schema_migrations(version) values ('011_candidate_search_text')
on conflict (version) do nothing;
