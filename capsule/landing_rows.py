"""The rows `landing._candidates` reads (split out of landing.py, which is at its size cap)."""

# One row (id, branch, latest `completed` payload) per `done` card with a branch whose newest
# `completed` or `unblocked` event is not older than the one parameter. The `unblocked` part is
# a lander wait lifted (`blocks.unblock`): the card is checked again on the same commit, however
# old its completion is (blocks.APPROVAL_MAX_AGE sends a much older one to `ready` instead).
# A card a person closed after its latest completion is listed however old it is: its owed push
# is still recorded (`landing._candidates`).
DONE_CARDS = (
    "SELECT t.id, t.branch_name, e.payload FROM tasks t JOIN task_events e ON e.task_id=t.id "
    "AND e.kind='completed' WHERE t.status='done' AND t.branch_name IS NOT NULL AND "
    "((SELECT MAX(created_at) FROM task_events WHERE task_id=t.id AND kind IN ('completed', "
    "'unblocked'))>=? OR EXISTS (SELECT 1 FROM task_events c WHERE c.task_id=t.id AND "
    "c.kind='closed' AND c.id>e.id)) AND e.id=(SELECT MAX(id) FROM task_events WHERE "
    "task_id=t.id AND kind='completed') ORDER BY e.id")

# One row for a `done` card (parameter 1) whose newest `completed` or `unblocked` event is older
# than parameter 2: past the tick's window (`landing._stale_notice`).
STALE_DONE = (
    "SELECT 1 FROM tasks t WHERE t.id=? AND t.status='done' AND COALESCE((SELECT MAX(created_at) "
    "FROM task_events WHERE task_id=t.id AND kind IN ('completed', 'unblocked')), 0)<?")
