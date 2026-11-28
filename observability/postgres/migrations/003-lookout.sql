-- irreversible: row 478 (The Lookout, additive DDL; rollback is `drop table cap_*`, see spec 9.1 M1)
-- 003 lookout (the Lookout design spec in the notes vault, sections 5.3-5.5).
-- Record tables are kept forever (D5). Every snapshot table carries board/snap_ts/pass_id;
-- the shipper's upsert only moves a row forward (`where excluded.snap_ts >= t.snap_ts`).
-- BEGIN LOOKOUT DDL
create table if not exists cap_cards (
  board text not null, task_id text not null,
  snap_ts timestamptz not null, pass_id text not null,
  title text, status text not null,
  product text not null, epic text, repo text,
  phase text not null,
  role_now text, model_now text, run_id_now text,
  block_kind text, block_reason text,
  waiting_on text, waiting_words text, waiting_since timestamptz,
  failure_count int not null default 0, consecutive_failures int not null default 0,
  risk_class text, tier text,
  created_at timestamptz, started_at timestamptz, completed_at timestamptz,
  commits int not null default 0, review_rounds int not null default 0,
  primary key (board, task_id));

create table if not exists cap_runs (
  board text not null, run_id text not null,
  snap_ts timestamptz not null, pass_id text not null,
  task_id text not null, role text,
  model text, provider text, account text, runner text,
  status text, outcome text, error_class text, attempts int,
  started_at timestamptz, ended_at timestamptz, last_heartbeat_at timestamptz,
  tokens_in bigint, tokens_out bigint, tokens_cache_read bigint,
  tokens_cache_write bigint,
  era text not null check (era in ('capsule','hermes')),
  primary key (board, run_id));
create index if not exists cap_runs_task on cap_runs (board, task_id);

create table if not exists cap_blocks (
  board text not null, task_id text not null, opened_at timestamptz not null,
  snap_ts timestamptz not null, pass_id text not null,
  kind text not null, reason text, waiting_on text not null, opened_event_id text,
  closed_at timestamptz,
  closed_by text check (closed_by in ('unblocked','completed','reset','archived','promoted','screen_answered')),
  primary key (board, task_id, opened_at));

create table if not exists cap_epics (
  board text not null, epic text not null,
  snap_ts timestamptz not null, pass_id text not null,
  product text not null, repo text, spec text, decision_rows text[],
  cards_total int, cards_done int, cards_blocked int,
  head_sha text, ahead_of_main int,
  pr_number int, pr_url text,
  publish_state text check (publish_state in ('none','pending','ok','refused')),
  publish_reason text, publish_state_since timestamptz,
  primary key (board, epic));

create table if not exists cap_prs (
  repo text not null, pr_number int not null,
  snap_ts timestamptz not null, pass_id text not null,
  board text not null, epic text not null, url text not null,
  state text not null check (state in ('draft','open','merged','closed')),
  checks text check (checks in ('pass','fail','pending','none')),
  review_decision text, head_sha text, cards_in_pr int,
  opened_at timestamptz, ready_at timestamptz, merged_at timestamptz,
  merge_sha text, closed_at timestamptz, updated_at timestamptz,
  primary key (repo, pr_number));

create table if not exists cap_commits (
  repo text not null, sha text not null,
  snap_ts timestamptz not null, pass_id text not null,
  board text not null, epic text, task_id text not null, run_id text,
  committed_at timestamptz not null,
  validator text, reviewer text, guide text,
  review_rounds int, overrides text[],
  pushed_at timestamptz, pr_number int, pr_comment_url text,
  primary key (repo, sha));

create table if not exists cap_incidents (
  board text not null, id text not null,
  snap_ts timestamptz not null, pass_id text not null,
  task_id text, run_id text, kind text, status text, role text,
  opened_at timestamptz, closed_at timestamptz,
  root_cause text,
  fix_card text, repeat_of text,
  primary key (board, id));

create table if not exists cap_tick (
  board text primary key,
  snap_ts timestamptz not null, pass_id text not null,
  host text, pid int, harness_sha text,
  last_pass_at timestamptz not null, pass_ms int, lookout_ms int,
  counts jsonb,
  running int, ready int, blocked int, paused_accounts text[],
  spool_bytes_unshipped bigint,
  last_ship_ok_at timestamptz);

create table if not exists prices (
  model text not null, valid_from date not null,
  usd_in_per_mtok numeric not null, usd_out_per_mtok numeric not null,
  usd_cache_read_per_mtok numeric not null, usd_cache_write_per_mtok numeric,
  primary key (model, valid_from));

create table if not exists cap_event_words (
  kind text primary key, words text not null,
  actor text not null check (actor in ('agent','tick','pratik','guide','korin','publisher')));

insert into cap_event_words (kind, words, actor) values
('created', 'card created', 'tick'), ('claimed', 'agent picked it up', 'agent'),
('claim_rejected', 'pickup refused', 'tick'), ('claim_extended', 'lease extended', 'agent'),
('reclaim_deferred', 'reclaim deferred', 'tick'), ('reclaimed', 'taken back from a stalled agent', 'tick'),
('promoted', 'moved to ready', 'tick'), ('request_review', 'sent for review', 'agent'),
('review_started', 'review started', 'agent'), ('review_retry', 'review retried', 'tick'),
('completed', 'committed', 'tick'), ('blocked', 'blocked', 'tick'), ('unblocked', 'unblocked', 'pratik'),
('decision_requested', 'needs your answer', 'agent'), ('first_screen_hold', 'screen to approve', 'tick'),
('failure_reset', 'failures reset', 'pratik'), ('artefact', 'agent produced a file', 'agent'),
('invalid_document', 'agent report was malformed', 'tick'), ('crashed', 'agent crashed', 'tick'),
('timed_out', 'agent ran out of time', 'tick'), ('spawn_failed', 'agent could not start', 'tick'),
('stale_run', 'run went stale', 'tick'), ('rate_limited', 'paused by rate limit', 'tick'),
('tick_error', 'factory error on this card', 'tick'), ('contradiction', 'rules disagree', 'guide'),
('contradiction_loop', 'rules keep disagreeing', 'guide'), ('ruling_started', 'guide started a ruling', 'guide'),
('guide_ruling', 'guide ruled', 'guide'), ('ruling_failed', 'guide ruling failed', 'guide'),
('aligned_uncited', 'aligned without a citation', 'guide'), ('checks_overridden', 'checks overridden', 'pratik'),
('intervention', 'you stepped in', 'pratik'), ('accept_refused', 'acceptance refused', 'tick'),
('screen_refused', 'screen refused', 'pratik'), ('override', 'override applied', 'pratik'),
('override_refused', 'override refused', 'tick'), ('override_expired', 'override expired', 'tick'),
('contract_amended', 'contract amended', 'pratik'), ('checkpoint', 'checkpoint saved', 'agent'),
('checkpoint_restart', 'restarted from a checkpoint', 'tick'),
('incident_opened', 'Korin opened an incident', 'korin'), ('incident_closed', 'Korin closed an incident', 'korin'),
('fix_card_routed', 'fix card created', 'korin'), ('korin_started', 'Korin started', 'korin'),
('korin_applied', 'Korin applied a fix', 'korin'), ('korin_failed', 'Korin''s fix failed', 'korin'),
('korin_gave_up', 'Korin gave up', 'korin'),
('parked', 'failed too often, parked', 'tick'), ('failed', 'run failed', 'tick'),
('dependency_wait', 'waiting for another card', 'tick'),
('block_loop_detected', 'block keeps coming back', 'tick'),
('first_screen_ok', 'screen approved', 'pratik'), ('first_screen_fix', 'screen sent back', 'pratik'),
('pushed', 'pushed to GitHub', 'publisher'), ('pr_opened', 'PR opened', 'publisher'),
('pr_comment', 'card posted to the PR', 'publisher'), ('pr_review_posted', 'branch review posted', 'publisher'),
('pr_ready', 'PR ready for your merge', 'publisher'), ('pr_checks', 'PR checks changed', 'publisher'),
('pr_merged', 'PR merged', 'publisher'), ('pr_closed', 'PR closed', 'publisher'),
('publish_refused', 'PR could not be published', 'publisher'),
('publish_ok', 'PR publishing recovered', 'publisher'),
('archived', 'archived', 'tick'), ('closed_stale', 'closed as stale (Hermes era)', 'tick')
on conflict (kind) do update set words = excluded.words, actor = excluded.actor;

create or replace view v_floor as
select c.board as product, c.epic, c.repo, c.task_id, c.title, c.phase,
       case c.phase
         when 'implementing' then 'building'
         when 'validating' then 'running the checks'
         when 'reviewing' then 'in code review'
         when 'guide_ruling' then 'guide is ruling'
         when 'korin' then 'Korin is investigating'
         when 'waiting' then coalesce(c.waiting_words, 'waiting')
         else coalesce(c.waiting_words, 'queued') end as phase_words,
       coalesce(r.role, c.role_now) as role, coalesce(r.model, c.model_now) as model, r.runner,
       c.run_id_now as run_id,
       extract(epoch from now() - coalesce(r.started_at, c.waiting_since, c.started_at, c.created_at))::bigint
         as elapsed_s,
       c.waiting_on, c.waiting_words
from cap_cards c
left join cap_runs r on r.board = c.board and r.run_id = c.run_id_now
where c.phase not in ('done', 'archived');

create or replace view v_waiting_on_pratik as
select c.board as product, c.epic, c.task_id || ': ' || coalesce(c.title, '') as item,
       c.waiting_words as why, c.block_reason as excerpt, c.waiting_since as since,
       'python3 -m capsule human ' || c.board || ' ' || c.task_id || ' ' ||
       case
         when c.block_kind = 'first_screen_hold' then 'screen-ok "<note>"  or  screen-fix "<what to change>"'
         when c.block_kind = 'contradiction_loop' then 'amend "<reason>" --body-file <file>  or  override "<reason>" --check <id> --ttl <t>'
         when c.block_kind = 'parked' then 'reset "<note>"'
         when c.block_kind = 'review_infra' then 'accept "<note>"  or  reset "<note>"'
         when c.status = 'triage' then 'reset "<note>"  or  accept "<note>"'
         when c.block_kind = 'capability' then 'unblock "<what was granted>"'
         else 'unblock "<answer>"' end as how_to_answer,
       '/d/lookout-trace?var-product=' || c.board || '&var-task_id=' || c.task_id as link
from cap_cards c where c.waiting_on = 'pratik' and c.phase <> 'archived'
union all
select e.board, e.epic, 'epic ' || e.epic, 'PR could not be published: ' || coalesce(e.publish_reason, '?'),
       null, e.publish_state_since,
       case e.publish_reason
         when 'gh_auth' then 'run: gh auth login   (the publisher retries within 5 min)'
         when 'repo_unknown' then 'fix repo_url in $CAPSULE_HOME/config.json'
         when 'secret_found' then 'remove the secret from the epic branch; the publisher retries within 5 min'
         when 'diverged' then 'reconcile the GitHub branch with the epic clone by hand (never force-push)'
         else 'read $CAPSULE_HOME/lookout/publish.log' end,
       e.pr_url
from cap_epics e where e.publish_state = 'refused'
union all
select p.board, p.epic, 'PR #' || p.pr_number || ' ' || p.epic, 'PR ready for your merge', null, p.ready_at,
       'review and merge it on GitHub', p.url
from cap_prs p where p.state = 'open' and p.ready_at is not null;

create or replace view v_run_cost as
select r.*,
       (coalesce(r.tokens_in, 0) * p.usd_in_per_mtok + coalesce(r.tokens_out, 0) * p.usd_out_per_mtok
        + coalesce(r.tokens_cache_read, 0) * p.usd_cache_read_per_mtok
        + coalesce(r.tokens_cache_write, 0) * coalesce(p.usd_cache_write_per_mtok, 0)) / 1e6 as usd_est
from cap_runs r
left join lateral (select * from prices p where p.model = r.model and p.valid_from <= r.started_at::date
                   order by p.valid_from desc limit 1) p on true;

create or replace view v_card_story as
select e.ts, e.board, e.task_id, e.source, coalesce(w.words, e.payload->>'event') as what,
       coalesce(w.actor, 'tick') as actor, e.payload->>'run_id' as run_id, e.link
from events e left join cap_event_words w on w.kind = e.payload->>'event'
where e.kind = 'card' and e.source in ('capsule', 'lookout-publisher');

create or replace view v_since as
select coalesce(m.ts, now() - interval '24 hours') as since_ts,
       case when m.ts is null then 'fallback_24h' else 'morning' end as source
from (select max(ts) as ts from events
      where kind = 'job' and payload->>'job' = 'morning' and payload->>'phase' = 'end'
        and ts > now() - interval '36 hours') m;

create or replace view v_factory as
select board, extract(epoch from now() - last_pass_at)::bigint as age_s,
       case when now() - last_pass_at > interval '5 minutes' then 'stopped'
            when coalesce(array_length(paused_accounts, 1), 0) > 0 then 'paused (budget)'
            when now() - last_pass_at < interval '90 seconds' then 'running'
            else 'late' end as state_words
from cap_tick;
