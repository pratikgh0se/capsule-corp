function add_labels(tag, ts, record)
  local p = record["path"] or ""
  local job, board, task, profile, vj, run = "other", "", "", "", "", ""
  local b, t = string.match(p, "/hermes/kanban/boards/([^/]+)/logs/([^/]+)%.log$")
  if b then job, board, task = "worker", b, t end
  local pr = string.match(p, "/hermes/profiles/([^/]+)/logs/")
  if pr then job, profile = "hermes", pr end
  local v = string.match(p, "/vault%-logs/([%a%-]+)%-%d")
  if v then job, vj = "vault", v end
  local cb, cr = string.match(p, "/capsule/boards/([^/]+)/spool/([^/]+)/worker%.log$")
  if cb then job, board, run = "capsule-worker", cb, cr end
  if string.match(p, "^/capsule/logs/tick%.log$") then job = "capsule-tick" end
  local ob = string.match(p, "^/capsule/boards/([^/]+)/outbox%.log$")
  if ob then job, board = "capsule-outbox", ob end
  record["job"], record["board"], record["task_id"] = job, board, task
  record["profile"], record["vault_job"], record["run_id"] = profile, vj, run
  return 2, ts, record
end
