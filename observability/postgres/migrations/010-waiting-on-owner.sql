-- down: re-apply the v_waiting_on_pratik definition from 008-waiting-view.sql, then drop view v_waiting_on_owner
-- 010 waiting on owner (D251, D269, D270): the view that lists what waits on the owner gets a neutral
-- name and reads waiting_on = 'owner' (the shipper now writes that value). Rows stored with the old value
-- refresh on the next full resync (hourly, or a collector restart); no data migration (D270). The retired app's board filter goes (D190).
-- The old view name is dropped; the Grafana dashboards read the new one.
create or replace view v_waiting_on_owner as
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
from cap_cards c
where c.waiting_on = 'owner' and c.phase <> 'archived'
  and c.status not in ('archived', 'parked')
  and (c.status <> 'done' or c.block_kind = 'first_screen_hold')
  and coalesce(c.block_kind, '') <> 'parked'
  and (c.status = 'triage' or c.block_kind in ('needs_input', 'decision_requested', 'capability',
       'first_screen_hold', 'contradiction_loop', 'review_infra'))
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

drop view if exists v_waiting_on_pratik;
