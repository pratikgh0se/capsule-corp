-- irreversible: t_853ff9d7 (additive seed rows; rollback is deleting the two kinds)
-- 004 park labels (follow-up to the card state machine, t_3e108a93). The event `parked` was
-- renamed `parked_by_human` and a `failure_limit` park exists; 003 labelled only `parked`.
-- Idempotent: the upsert matches 003's seed. init.sql carries the same statement.
insert into cap_event_words (kind, words, actor) values
('parked_by_human', 'parked by you', 'pratik'),
('failure_limit', 'failed too often, needs a reset', 'tick')
on conflict (kind) do update set words = excluded.words, actor = excluded.actor;
