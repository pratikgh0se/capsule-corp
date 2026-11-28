"""One Slack message per problem that edits itself (visibility spec section 6, card 8)."""
HANDLED = "✔"
REMIND = "remind:"       # prefix of the outbox key of a reminder thread reply


def needs_key(task_id):
    return f"needs:{task_id}"


def broken_key(fingerprint):
    return f"broken:{fingerprint}"


def edit_target(conn, key):
    """{"channel", "ts"} of the open message `key` posted, else None."""
    row = conn.execute("SELECT channel, ts FROM slack_msg WHERE key=? AND state='open'",
                       (key,)).fetchone()
    return {"channel": row["channel"], "ts": row["ts"]} if row is not None else None

BRAINSTORM_REPLY = "brainstorm-reply:"   # outbox key prefix of a brainstorm reply (D229)
