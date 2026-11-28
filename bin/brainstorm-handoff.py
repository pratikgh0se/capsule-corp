#!/usr/bin/env python3
"""Brainstorm handoff block (capsule/brainstorm.py loads it). The session id is minted FIRST
(`str(uuid.uuid4())` in `main`), then handed to `render()`, so the note header, the card
comment and the message all carry the same id; the id is what
`claude --session-id <uuid> -n <name>` is started with.
Usage: bin/brainstorm-handoff.py "<title>" [--questions N] [--project-dir DIR]
A title with no Latin letters or digits becomes the name "brainstorm", so such sessions share
a `claude -r` name. Prints the message block."""
import argparse
import os
import re
import shlex
import sys
import uuid

MINUTES_PER_QUESTION = 5    # example: "3 open questions - ~15 min"
MAX_NAME = 40
BUTTONS = "[ Open the note ] [ Not now, queue it ]"
ONE_TERMINAL = "One terminal only: run this in a single terminal, never the same session in two."


def _name_from_title(title):
    """A shell-safe lowercase slug of the brainstorm title; 'brainstorm' when nothing is left."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:MAX_NAME].strip("-")
    return slug or "brainstorm"


def _cost_line(open_questions):
    if open_questions is None:    # a Capsule-started brainstorm: nothing has been asked yet
        return "unknown until the first turn"
    noun = "open question" if open_questions == 1 else "open questions"
    return f"{open_questions} {noun} - ~{open_questions * MINUTES_PER_QUESTION} min"


def render(title, open_questions, project_dir, session_id):
    """The three outputs that must agree on the id: note_header, card_comment, message."""
    uuid.UUID(session_id)    # ValueError unless a real uuid (`claude --session-id` needs one)
    name = _name_from_title(title)
    start = f"claude --session-id {session_id} -n {name}"
    later = f"Later: claude -r {name}"    # re-running --session-id with a used id fails
    return {
        "name": name,
        "session_id": session_id,
        "note_header": f"session_name: {name}\nsession_id: {session_id}",
        "card_comment": f"Brainstorm session {name} ({session_id}): {start}",
        "message": "\n".join([
            f"Session: {name}",
            f"Id: {session_id}",
            f"Cost: {_cost_line(open_questions)}",
            f"cd {shlex.quote(project_dir)}",
            start,
            later,
            ONE_TERMINAL,
            BUTTONS,
        ]),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("title")
    ap.add_argument("--questions", type=int, default=0, help="open question count")
    ap.add_argument("--project-dir", default=os.getcwd())
    a = ap.parse_args(argv)
    if a.questions < 0:
        ap.error("--questions must be 0 or more")
    print(render(a.title, a.questions, a.project_dir, str(uuid.uuid4()))["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
