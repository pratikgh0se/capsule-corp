-- down: drop the new check, move 'owner' rows back to the legacy value, re-add the old check (003-lookout.sql has its text)
-- 011 actor owner (b114): the label table's actor value for the person becomes 'owner'. The check
-- keeps accepting the legacy value so old rows and old readers stay valid; nothing writes it again.
-- db_migrate runs this file and its ledger row in one transaction. init.sql carries the same statements.
set local lock_timeout = '5s';
alter table cap_event_words drop constraint if exists cap_event_words_actor_check;
alter table cap_event_words add constraint cap_event_words_actor_check
  check (actor in ('agent','tick','pratik','owner','guide','korin','publisher'));
update cap_event_words set actor = 'owner' where actor = 'pratik';
