"""The SQL behind `capsule.card.review_rounds` in obs_probe, one statement per board
kind (row 583)."""
MARKER = ".capsule-owned"   # board_fence.OWNED_MARKER; a test holds the two equal

SQL = {
    True: (
        "select task_id, count(*) from task_runs where profile='implementer'"
        " and (outcome='completed' or id in (select run_id from task_events"
        " where task_id=task_runs.task_id and kind='request_review'))"
        " group by task_id"),
    False: (
        "select task_id, count(*) from task_events"
        " where kind='changes_requested' group by task_id"),
}
