"""One JSON document per human intervention (row 420)."""
import json
import os
import stat
import sys
import tempfile
import time
from functools import lru_cache
from pathlib import Path

from capsule import answer_base, answer_scope, row_schemas


def _next_name(d):
    high = 0
    for sub in (d, d / "done"):
        if sub.is_dir():
            for p in sub.iterdir():
                if p.stem.isdigit():
                    high = max(high, int(p.stem))
    return f"{high + 1}.json"


# Row 440: an intervention the handover itself forced (the old-system-blocked card the pilot
# starts from) is tagged, and the report counts it apart from the vs-U1 rate.
TAGS = ("migration-reset", "handover-route")
AUTHORS = ("owner", "cos")
LEGACY_OWNER = "pratik"      # stored files and old commands say this; it reads as "owner"
# row 454 item 4: `override` needs all four, checked here so a malformed CLI call fails at the
# writer, not silently at the next tick.
OVERRIDE_FIELDS = ("check", "ttl", "fix_card", "issuer")


@lru_cache(maxsize=None)
def _owner_name(env_value, home):
    """The configured owner name, as `host.owner_name` reads it (env `CAPSULE_OWNER_NAME`, else
    host.json `owner.name`, nested or flat), once per (env value, home). Stdlib only, so the Slack
    answer path stays on its protected closure; the rules come from answer_base. Anything unusable
    (not a name, a role, set twice, a non-regular or unreadable file) is the default "owner"."""
    def usable(v):
        return isinstance(v, str) and answer_base.OWNER_RE.fullmatch(v) and (
            v.strip().lower() not in answer_base.ROLES)
    path = Path(home) / "host.json"
    try:
        if not stat.S_ISREG(path.stat().st_mode):     # a FIFO would block the read for ever
            return "owner"
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        if path.is_symlink():     # a dangling link is a bad file for host, not a missing one
            return "owner"
        doc = {}
    except (OSError, ValueError):
        return "owner"        # host reads the file first: a bad file beats a good env value
    nested = doc.get("owner") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or ("owner" in doc and not isinstance(nested, dict)):
        return "owner"
    found = [doc["owner.name"]] if "owner.name" in doc else []
    found += [nested["name"]] if isinstance(nested, dict) and "name" in nested else []
    name = env_value or (found[0] if len(found) == 1 else None)
    return name if len(found) < 2 and usable(name) else "owner"


def _name():
    return _owner_name(os.environ.get("CAPSULE_OWNER_NAME"), str(answer_base.home()))


def canonical_author(author):
    """"owner" for the legacy value and the configured owner name (any case), else unchanged."""
    if not isinstance(author, str) or author.lower() == "cos":
        return author
    low = author.lower()
    return "owner" if low in ("owner", LEGACY_OWNER, _name().lower()) else author


def _agent_session():
    return any(os.environ.get(k, "") not in ("", "0") for k in ("CLAUDECODE", "AI_AGENT"))


def _ttys():
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):     # a closed or replaced stream is not a terminal
        return False


def _person_terminal():
    """A person typing: no agent marker, and stdin and stdout are both terminals."""
    return not _agent_session() and _ttys()


AGENT_AUTHORS = ("cos", "capsule")      # `capsule`: the patrol's own entries, never owner work


def is_cos(author):
    """True for the chief of staff's author value (or the patrol's, `capsule`) in any case, with
    stray whitespace ignored."""
    return isinstance(author, str) and author.strip().lower() in AGENT_AUTHORS


def _stored_author(author):
    """What a file records: the owner's configured name in lowercase when the pinned schema's
    author enum allows it (the owner's own name on the live host), else "owner"; any other author
    as given."""
    if canonical_author(author) != "owner":
        return author
    allowed = row_schemas.load("intervention.v1")["properties"]["author"]["enum"]
    return _name().lower() if _name().lower() in allowed else "owner"


def cli_author(author):
    """The author a CLI verb records: the flag, else owner for a person's terminal and `cos` for
    a non-agent pipe or script (only a person at a terminal defaults to owner). An agent
    session (CLAUDECODE or AI_AGENT set, and not "" or "0") must pass the flag, since an omitted
    one would read as the person. An explicit owner (or the legacy name) is accepted only from a
    person's terminal (no agent marker, stdin and stdout both TTYs); anything else must say
    `--author cos`. Only the CLI entry points call this; `write` and every in-process caller
    (the Slack answer path) keep their own default."""
    if author is not None:
        author = canonical_author(author)
        if author == "owner" and not _person_terminal():
            raise ValueError("--author owner is only accepted from the owner's own terminal (not"
                             " an agent session, a pipe or a script): pass `--author cos`."
                             " Nothing was changed")
        return author
    if _agent_session():
        raise ValueError("this is an agent session, so --author is required: pass `--author cos`;"
                         " `--author owner` is only for the owner typing the command themselves."
                         " Nothing was changed")
    if _person_terminal():
        return "owner"
    print("recorded as cos (not a terminal); the owner types it in a terminal", file=sys.stderr)
    return "cos"     # a pipe or a script is not the person


def write(home, board, task_id, kind, text, *, now=None, tag=None,
          check=None, ttl=None, fix_card=None, issuer=None, assignee=None, author="owner",
          lint=True, via=None, ask=None):
    text = answer_scope.require(kind, text, lint)   # inc_8b4a0a4b: a narrowing answer
    author = canonical_author(author)   # the legacy name and the owner's name read as owner
    if author not in AUTHORS:   # who acted, so Q2b can leave the CoS's files out
        raise ValueError(f"unknown author {author!r} (known: {', '.join(AUTHORS)})")
    if tag is not None and tag not in TAGS:
        raise ValueError(f"unknown intervention tag {tag!r} (known: {', '.join(TAGS)})")
    if assignee is not None and kind != "epic":
        raise ValueError("--assignee applies to the epic verb only")
    if tag == "handover-route" and (kind != "epic" or not assignee):
        raise ValueError("handover-route needs kind epic and an assignee")
    d = answer_base.human_dir(home, board)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"task_id": task_id, "kind": kind, "text": text,
           "at": int(now if now is not None else time.time()), "author": author}
    if tag is not None:
        doc["tag"] = tag
    if kind == "epic" and assignee:   # route the branch and the assignee together
        doc["assignee"] = assignee
    if kind == "park" and not (text or "").strip():
        # the same guard as `override`'s below - a
        # whitespace-only (or, from a raw non-CLI file, missing/non-string) trigger must
        # not reach the tick as a valid park; `blocks.park` refuses it too (defense in
        # depth for a hand-written file).
        raise ValueError("park needs a non-empty trigger")
    if kind == "stop" and not (text or "").strip():
        raise ValueError("stop needs a non-empty trigger")
    if kind == "wait" and not (text or "").strip():
        raise ValueError("wait needs an epic branch or a commit sha")
    if kind == "close" and not (text or "").strip():
        raise ValueError("close needs a non-empty reason")
    if kind == "archive" and not (text or "").strip():
        raise ValueError("archive needs a non-empty reason")
    if kind == "override":
        # a whitespace-only reason must not reach the tick as valid
        if not (text or "").strip():
            raise ValueError("override needs a non-empty reason")
        values = {"check": check, "ttl": ttl, "fix_card": fix_card, "issuer": issuer}
        missing = [f for f in OVERRIDE_FIELDS if not values[f]]
        if missing:
            raise ValueError(f"override needs {', '.join(missing)}")
        doc.update(values)
        doc["text"] = text.strip()
    _add_provenance(doc, via, ask)
    # row 569: a second check beside the hand checks above
    row_schemas.require("intervention.v1", doc)
    return _land(d, doc)


def _add_provenance(doc, via, ask):
    """D225: via/ask are written only when the loaded schema has them (the schema change lands
    on its own; until then the file is exactly what it was)."""
    props = row_schemas.load("intervention.v1").get("properties", {})
    for field, val in (("via", via), ("ask", ask)):
        if val is not None and field in props:
            doc[field] = val


def _land(d, doc):
    """Write `doc` as the next numbered file without ever replacing one: a temp file is filled
    first (a reader never sees a half file), then hard-linked to the name, which fails when a
    concurrent writer took that number; the next number is tried."""
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".human.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False))
        for _ in range(1000):
            final = d / _next_name(d)
            try:
                os.link(tmp, final)
            except FileExistsError:
                continue
            return final
        raise OSError("could not allocate a human file number")
    finally:
        Path(tmp).unlink(missing_ok=True)


OWN_VERBS = ("amend", "assign", "override", "check", "size", "depends", "workload",
             "runner", "answer", "preview")
_CHK = "capsule human <board> <card> check '<cmd>'"
_OVR = "use override '<reason>' --check <id> --ttl <t> --fix-card <id>"
_SIZE = "use `capsule human <board> <card> size <tag> --risk-flags <a,b>`"
# flag -> (verbs that read it, "plain" = reset/epic/..., where it applies, what to do instead)
FLAG_USE = {
    "body_file": (("amend",), "the amend verb", "use amend '<reason>' --body-file <path>"),
    "file_set": (("amend",), "the amend verb", "use amend '<reason>' --file-set <paths>"),
    "no_lint": (("amend", "answer"), "amend and answer", "use one of those verbs"),
    "check": (("override",), "the override verb", f"to change a card's check use `{_CHK}`"),
    "expect": (("check",), "the check verb", f"use `{_CHK} --expect <x>`"),
    "risk_flags": (("size",), "the size verb", _SIZE),
    "ttl": (("override",), "the override verb", _OVR),
    "fix_card": (("override",), "the override verb", _OVR),
    "tag": (("plain", "answer"), "the plain verbs", "use a plain verb such as reset or epic"),
}
