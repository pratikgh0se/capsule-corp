"""A Korin fix card carries the text of each human file it names (inc_9a4137e9).

A builder run reads only the human files of its own card, so a file the post-mortem names by
number is unreadable to it. The tick copies the text into the card body when it files the card.
"""
import json
import re
from pathlib import Path

from capsule import human, paths

HEADING = "## Human files named by this card"
_NAME = re.compile(r"human/done/(\d+\.json)\b|(?<![\w./-])(\d+\.json)\b")


BODY_LIMIT = json.loads((paths.SCHEMAS / "postmortem.v1.json").read_text())[
    "properties"]["fix_card"]["properties"]["body"]["maxLength"]   # row 507(b): read live
ENTRY_CAP = 2000        # characters of one quoted human file
MIN_ENTRY = 200         # a smaller room is not worth an entry
TAIL_RESERVE = 40       # room kept for the "more human files" line
_MARKER = 40            # room a "[truncated: N more chars]" marker needs


def _cap(entry, cap):
    if len(entry) <= cap:
        return entry
    keep = cap - _MARKER
    return f"{entry[:keep]}\n[truncated: {len(entry) - keep} more chars]"


def _entry(human_dir, name):
    path = Path(human_dir) / "done" / name
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError(name)
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return f"{name}: no such human file"
    if not isinstance(doc, dict):
        return f"{name}: no such human file"
    author = human.canonical_author(doc.get("author") or "owner")
    return (f"### {name}\n- task_id: {doc.get('task_id')}\n- kind: {doc.get('kind')}\n"
            f"- author: {author}\n\n{doc.get('text')}")


def quoted(report, human_dir):
    """A copy of `report` whose fix_card body ends with the text of each human file it names
    (the body, root_cause and mitigation are scanned); `report` itself when it names none."""
    card = report["fix_card"]
    seen = {}
    for text in (card["body"], report["root_cause"], report["mitigation"]):
        for m in _NAME.finditer(text):
            seen.setdefault(m.group(1) or m.group(2))
    if not seen:
        return report
    entries = []
    used = len(card["body"]) + len(HEADING) + 2
    names = list(seen)
    for name in names:
        room = BODY_LIMIT - used - 2 - TAIL_RESERVE
        if room < MIN_ENTRY:
            break
        entry = _cap(_entry(human_dir, name), min(ENTRY_CAP, room))
        entries.append(entry)
        used += len(entry) + 2
    parts = [HEADING, *entries] if entries else []
    if len(entries) < len(names):
        parts.append(f"[{len(names) - len(entries)} more human files not quoted]")
    body = "\n\n".join([card["body"], *parts])
    if len(body) > BODY_LIMIT:
        return report
    return {**report, "fix_card": {**card, "body": body}}
