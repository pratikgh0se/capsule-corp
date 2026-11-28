#!/usr/bin/env python3
"""intake.py - Boards/Intake.md is the owner's file; the bridge reads it once and clears what it
processed.

Spec: docs/superpowers/specs/2026-09-07-board-files-and-ledger-design.md 3.

Line-oriented and tiny because he types it on a phone keyboard as often as on the laptop. Only
`## New work` and `## Approvals` are consumable; an unknown heading is treated as `## Notes` (never
touched), so a typo in a heading can never make the bridge run a line he meant as prose.

`parse_line`/`parse`/`render` are pure and hold the whole grammar; `consume` is the only part that
writes anything. The one other writer is a CLI, not a tool permission: `intake.py add-finding
<board> <risk> <title...> --source "<label>"` appends exactly one line under `## Findings` for the
two review jobs, which are NOT allowed to Edit this file at all (see `add_finding`). `--boards-dir`
(or $AGENT_OPS_BOARDS_DIR) overrides the boards directory the board name is checked against; both
default to None (board.intake_board_names()), so this is purely a
test-isolation hook.
"""
import datetime as _dt
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board      # noqa
import events     # noqa
import ledger     # noqa
import vault_git  # noqa

NEW_WORK = "## New work"
APPROVALS = "## Approvals"
CONSUMABLE = (NEW_WORK, APPROVALS)
# Written by the two review jobs (maintenance/security.md nightly, maintenance/agent-review.md
# monthly) and consumed by NOTHING: it is absent from CONSUMABLE, so a finding line is pass-through
# like `## Notes`. A finding becomes a card when the owner moves the line under `## New work`.
FINDINGS = "## Findings"
# The owner's own override of the sprint planner's pick order: `- <board> <task_id>` or `- <board>
# <task_id> | <note>`, one per line, top to bottom. Absent from CONSUMABLE like FINDINGS: it names
# which Backlog card the planner specs next, it is not work for the bridge to create. The planner
# (maintenance/sprint-plan.md) is the one reader (`intake.py plan-next`) and the one writer of
# removals (`intake.py plan-next-remove <board> <task_id>`, via Bash: the planner's Write/Edit is
# clamped to Specs/** by hooks/sprint-plan-guard.py, so this section can only be cleared through a
# CLI verb, the shape FINDINGS uses).
PLAN_NEXT = "## Plan next"
NOTES = "## Notes"
PROCESSED = "## Processed"
REJECTED = "## Rejected"
RECEIPT_CAP = 20
ERROR_TICKS = 5          # a line that errors this many ticks running moves under REJECTED
RISKS = ("low", "med", "high")
DEFAULT_RISK = "med"

# The guard on every card id this module takes from a line. The canonical pattern lives on
# board.TASK_ID_RE; this name stays for every existing caller in this module.
TASK_ID_RE = board.TASK_ID_RE
_ERROR_RE = re.compile(r"\s*<!--\s*error:.*?-->\s*$")
_NEW_RE = re.compile(r"^-\s*repo:\s*([^|]+?)\s*\|\s*(.*)$")
_RISK_RE = re.compile(r"^risk:\s*([a-z]+)\s*(?:\|\s*(.*))?$")
# An OPTIONAL third field, after `risk:` and before the title: `- repo: my-app | risk: med |
# bet: <slug> | <title>`.
#
# Flag-gated on board.BET_FLAG (OPS_BET_FIELD, default off): with the flag off the segment is NOT
# grammar and falls into the title, the byte-identity fence test_intake asserts both ways.
#
# This is a FIELD, never a verb. The `bet:` APPROVAL verb is parked, and `approve pr:` was dropped
# for good: a bare `- bet: x` line stays unparseable, so nothing the phone can write gains a
# privilege here.
_BET_RE = re.compile(r"^bet:\s*([^|]*?)\s*(?:\|\s*(.*))?$")
# The capture was `\S+`, which let any string reach the slug check as a "spec slug". The grammar now
# admits only the slug charset (merge_gate.SLUG_RE, still re-checked in parse_line: a second fence).
_APPROVE_SPEC_RE = re.compile(r"^-\s*approve\s+spec:\s*([a-z0-9][a-z0-9-]{0,80})\s*$")
# `approve spec:` is the only approval the file grammar knows. Everything else beginning `approve`
# is an ordinary malformed approval line: a merge approval is never taken from a file any agent with
# a vault connection can write, and no reserved verb is left that a future change could quietly
# start honouring. PR approval is the owner's GitHub merge of the epic PR.
_APPROVE_RE = re.compile(r"^-\s*approve\b")
UNKNOWN_APPROVAL = "unknown approval; PR approval is the GitHub merge of the epic PR"
_DROP_RE = re.compile(r"^-\s*drop:\s*(\S+)\s+(\S+)\s*$")


def _slug_re():
    import merge_gate   # lazy: merge_gate imports board, board imports nothing of ours
    return merge_gate.SLUG_RE


def strip_error(line):
    """Drop a trailing `<!-- error: ... -->`. Fixing `potato` -> `my-app` is then enough, and an
    unfixed line never accumulates a second comment (replace, never append)."""
    return _ERROR_RE.sub("", line)


def line_key(line):
    """The line's durable identity: sha256 of the error-stripped, whitespace-collapsed text, first
    16 hex. It is the `Board.create` idempotency key, the `source: intake:<sha>` body marker and the
    per-line message throttle key: one identity, so a line processed twice resolves to one card."""
    text = " ".join(strip_error(line).split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _err(why):
    return {"verb": "error", "why": why}


def parse_line(line, *, boards):
    """One line -> one instruction, or an `error` carrying the reason that goes verbatim into the
    inline comment. Never raises: a phone keyboard produces anything, and a malformed line must
    become a comment, not a traceback."""
    text = strip_error(line).strip()
    if not text.startswith("-"):
        return _err("not a list line")
    slug_re = _slug_re()

    m = _APPROVE_SPEC_RE.match(text)
    if m:
        if not slug_re.fullmatch(m.group(1)):
            return _err(f"bad spec slug {m.group(1)!r}")
        return {"verb": "approve_spec", "slug": m.group(1)}

    if _APPROVE_RE.match(text):
        return _err(UNKNOWN_APPROVAL)

    for regex, verb in ((_DROP_RE, "drop"),):
        m = regex.match(text)
        if not m:
            continue
        name, tid = m.group(1), m.group(2)
        if name not in tuple(boards):
            return _err(f"no board named {name!r}")
        if not TASK_ID_RE.fullmatch(tid):
            return _err(f"bad task id {tid!r}")
        return {"verb": verb, "board": name, "task_id": tid}

    m = _NEW_RE.match(text)
    if m:
        repo, rest = m.group(1).strip(), m.group(2)
        if not board.BOARD_RE.fullmatch(repo) or repo not in tuple(boards):
            return _err(f"no board named {repo!r}")
        risk = DEFAULT_RISK
        rm = _RISK_RE.match(rest)
        if rm:
            risk, rest = rm.group(1), rm.group(2) or ""
            if risk not in RISKS:
                return _err(f"risk must be one of {', '.join(RISKS)}, not {risk!r}")
        bets = board.bet_enabled()
        bet = ""
        if bets:
            bm = _BET_RE.match(rest)
            if bm:
                bet, rest = bm.group(1), bm.group(2) or ""
                if not board.BET_SLUG_RE.fullmatch(bet):
                    return _err(f"bad bet slug {bet!r}")
        title = rest.strip()
        if not title:
            return _err("no title")
        out = {"verb": "new", "repo": repo, "risk": risk, "title": title}
        if bets:
            # The key is absent entirely with the flag off, so a downstream `entry.get("bet", "")`
            # and the old dict are the same shape.
            out["bet"] = bet
        return out

    return _err("not an instruction the bridge understands "
                "(repo: / approve spec: / drop:)")


def parse(text, *, boards):
    """Every consumable line, in file order, each carrying its line index, raw text and section.
    Lines outside CONSUMABLE are never returned: that is the protection `## Notes` needs."""
    out = []
    section = None
    for i, line in enumerate(text.splitlines()):
        if line.startswith("## "):
            section = line.strip() if line.strip() in CONSUMABLE else None
            continue
        if section is None or not line.strip().startswith("- "):
            continue
        entry = {"index": i, "raw": line, "section": section, "key": line_key(line)}
        entry.update(parse_line(line, boards=boards))
        out.append(entry)
    return out


def _receipt_block(text, receipts):
    """The newest RECEIPT_CAP receipts, newest first: new ones ahead of the
    ones already in the file."""
    lines = text.splitlines()
    existing = []
    if PROCESSED in lines:
        start = lines.index(PROCESSED)
        end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        existing = [l for l in lines[start + 1:end] if l.startswith("- ")]
    return (list(reversed(list(receipts))) + existing)[:RECEIPT_CAP]


def render(text, *, consumed, errors, receipts, rejected, now):
    """The file written back: consumed lines gone, error lines annotated in place, rejected lines
    moved, receipts refreshed. Frontmatter, the header comment, empty headings and everything under
    `## Notes` are untouched. `consumed`/`rejected` are line indices from `parse`; `errors` maps a
    line index to its reason."""
    lines = text.splitlines()
    consumed = set(consumed)
    rejected = set(rejected)
    moved = []
    out = []
    for i, line in enumerate(lines):
        if i in consumed:
            continue
        if i in rejected:
            moved.append(strip_error(line).rstrip())
            continue
        if i in errors:
            out.append(strip_error(line).rstrip() + f"   <!-- error: {errors[i]} ({now}) -->")
            continue
        out.append(line)

    text2 = "\n".join(out).rstrip("\n") + "\n"
    block = _receipt_block(text, receipts)
    if block:
        text2 = _replace_section(text2, PROCESSED, block)
    if moved:
        text2 = _replace_section(text2, REJECTED, moved, append=True)
    return text2


def _replace_section(text, heading, block, *, append=False):
    """Replace `heading`'s list body with `block`, appending the section at the very end of the
    file when absent (after `## Notes`, so nothing lands ahead of the owner's prose). With
    `append=True` the existing lines are kept and `block` added after them."""
    lines = text.splitlines()
    if heading in lines:
        start = lines.index(heading)
        end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        body = [l for l in lines[start + 1:end] if l.startswith("- ")] if append else []
        return "\n".join(lines[:start + 1] + body + list(block) + lines[end:]).rstrip("\n") + "\n"
    return "\n".join(lines + [heading] + list(block)).rstrip("\n") + "\n"


# A reserved pseudo-task on a reserved pseudo-board: the file is not a card, but the lock path is
# the exclusion mechanism intake and the sync tick share (the approve scripts do not take it).
INTAKE_LOCK_BOARD = "vault"
INTAKE_LOCK_TASK = "intake"


def _body_for(entry):
    """The card body an intake line produces. `source: intake:<key>` is the durable half of the
    idempotency key: the create flag makes the board dedupe, and this makes the reason legible on
    the card. `bet:` is the reason the card exists, not just where it came from; `board.bet_line`
    returns "" when OPS_BET_FIELD is off or the entry carries no bet, so the body is then unchanged.

    Careful: `line_key` hashes the whole line, so adding `bet:` to a line that already produced a
    card yields a new key and therefore a SECOND card (RUNBOOK, "The `bet:` card field").
    """
    return (f"<!-- intake\nrisk: {entry['risk']}\n"
            f"{board.bet_line(entry.get('bet', ''))}"
            f"source: intake:{entry['key']}\n-->\n\n"
            f"From Boards/Intake.md on {_dt.date.today().isoformat()}.\n")


def _execute(entry, *, boards_dir, runner, now):
    """Run one parsed line. Returns (receipt_text, None) on success or (None, reason) on a failure
    that should become an inline error comment. Every verb is keyed, so running one twice is
    indistinguishable from once."""
    import merge_gate
    verb = entry["verb"]
    if verb == "new":
        b = board.open_board(entry["repo"], runner=runner, boards_dir=boards_dir)
        tid = b.create(board.NewCard(title=entry["title"], body=_body_for(entry),
                                     idempotency_key=f"intake:{entry['key']}",
                                     created_by="intake"))
        events.log("card", "intake", entry["title"], board=entry["repo"], task_id=tid,
                   title=entry["title"], payload={"event": "created", "state": "open"})
        b.append_ledger(tid, ledger.make_entry(
            entry["repo"], tid, "created", source="intake",
            summary=entry["title"], dedupe=f"intake:{entry['key']}",
            payload={"risk": entry["risk"], "intake_key": entry["key"]}))
        return f"created {entry['repo']}/{tid} · {entry['title']}", None
    if verb == "approve_spec":
        merge_gate.write_spec_approval(entry["slug"], via="intake")
        events.log("card", "intake", f"approved spec {entry['slug']}", slug=entry["slug"],
                   payload={"event": "approved", "state": "closed"})
        return f"approved spec {entry['slug']}", None
    if verb == "drop":
        b = board.open_board(entry["board"], runner=runner, boards_dir=boards_dir)
        card = b.try_show(entry["task_id"])
        if card is not None and card.legacy_status == "archived":
            # The line already did what it asked for: a second tick (or a `drop:` for a card
            # dropped from the phone) is success, not an error comment that sits there for five
            # ticks and then gets rejected. Every other verb is idempotent the same way.
            return f"already dropped {entry['board']}/{entry['task_id']}", None
        b.archive(entry["task_id"])
        events.log("card", "intake", f"dropped {entry['task_id']}", board=entry["board"],
                   task_id=entry["task_id"], payload={"event": "abandoned", "state": "closed"})
        b.append_ledger(entry["task_id"], ledger.make_entry(
            entry["board"], entry["task_id"], "abandoned", source="intake",
            summary="dropped from Boards/Intake.md", dedupe="drop",
            payload={"via": "intake"}))
        return f"dropped {entry['board']}/{entry['task_id']}", None
    return None, entry.get("why") or "unrecognised line"


def consume(*, vault=None, boards_dir=None, runner=None, sender=None, seen_path=None,
            state_dir=None, git_runner=None, now=None):
    """Read Boards/Intake.md once, run what it says, clear what ran. Runs at the TOP of the tick,
    before the other sync steps, so a phone approval is recorded in the same tick.
    Every step is guarded so a partial run is safe: the lock stops two ticks overlapping, a failed
    pull means no consume at all (a stale read would re-run instructions the owner already watched
    being processed), and the write-back is abandoned if the bytes moved under us; the side effects
    are all keyed, so the next tick converges and the receipts are one tick late."""
    import board_view
    # Same guard board_view.write/regenerate/migrate carry: under the suite's marker an implicit
    # default vault is always a bug. consume() writes Boards/Intake.md and pushes it, and the guard
    # goes BEFORE the lock so a test can never take a real lock either.
    board_view._refuse_default_vault_under_test(vault)
    res = {"consumed": [], "errors": {}, "receipts": [], "rejected": [], "written": False, "skipped": None}
    root = board_view.vault_root(vault)
    if root is None:
        res["skipped"] = "no-vault"
        return res
    b0 = board.open_board(INTAKE_LOCK_BOARD, runner=runner, boards_dir=boards_dir)
    try:
        with b0.lock(INTAKE_LOCK_TASK, state_dir=state_dir):
            return _consume_locked(root, res, boards_dir=boards_dir, runner=runner, sender=sender,
                                   seen_path=seen_path, git_runner=git_runner, now=now)
    except board.Locked:
        res["skipped"] = "locked"      # no event: the next tick gets it
        return res


def _consume_locked(root, res, *, boards_dir, runner, sender, seen_path, git_runner, now):
    import board_bridge as bb
    import board_view
    now = now or _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    path = root / board_view.INTAKE_REL
    if not vault_git.pull(root, runner=git_runner):
        res["skipped"] = "pull"
        return res
    if not path.exists():
        # Created here means it exists only on this host until pushed, and the phone is where the
        # file is meant to be written.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(board_view.INTAKE_TEMPLATE, encoding="utf-8")
        vault_git.push(root, [board_view.INTAKE_REL], "intake: create", runner=git_runner)
        return res
    text = path.read_text(encoding="utf-8")
    boards = tuple(board.board_names(boards_dir))
    seen = bb._load_seen(seen_path)
    seen_changed = False
    send = sender or bb._default_sender
    done_keys = set()

    for entry in parse(text, boards=boards):
        if entry["key"] in done_keys:
            # a duplicate line: one card, both lines cleared
            res["consumed"].append(entry["index"])
            continue
        if entry["verb"] == "error":
            reason = entry["why"]
        else:
            try:
                receipt, reason = _execute(entry, boards_dir=boards_dir, runner=runner, now=now)
            except Exception as exc:
                receipt, reason = None, f"{type(exc).__name__}: {exc}"
            if reason is None:
                res["receipts"].append(f"- {now} · {receipt}")
                res["consumed"].append(entry["index"])
                done_keys.add(entry["key"])
                continue
        res["errors"][entry["index"]] = reason
        events.log("card", "intake", f"{reason}: {entry['raw'].strip()[:120]}", severity="warn",
                   payload={"event": "intake_error", "state": "open"})
        n_key = f"intake:{entry['key']}:errors"
        n = int(seen.get(n_key) or 0) + 1
        seen[n_key] = n
        seen_changed = True
        if n >= ERROR_TICKS:
            res["rejected"].append(entry["index"])
            res["errors"].pop(entry["index"], None)
        msg_key = f"intake:{entry['key']}:error"
        if msg_key not in seen:
            if send(bb.fmt_msg("\U0001f4e5 Intake line not understood",
                               [entry["raw"].strip()[:120], reason,
                                "Fix the line in Boards/Intake.md; the next tick retries it."])):
                seen[msg_key] = _dt.date.today().isoformat()

    if seen_changed:
        bb._save_seen(seen, seen_path)
    if not (res["consumed"] or res["errors"] or res["receipts"] or res["rejected"]):
        return res

    # The pull -> parse -> execute window can race the owner saving a line. There is no lock to take
    # on a file a notes app on a phone also writes, so: hash on read, re-read before writing,
    # abandon the write if it moved. Universal idempotency makes the worst case
    # "a line runs twice", which is indistinguishable from running once.
    if path.read_text(encoding="utf-8") != text:
        events.log("card", "intake", "intake edited mid-tick: write-back abandoned, effects kept",
                   severity="warn", payload={"event": "intake_raced", "state": None})
        return res

    out = render(text, consumed=res["consumed"], errors=res["errors"],
                 receipts=res["receipts"], rejected=res["rejected"], now=now)
    _atomic_write(path, out)
    res["written"] = True
    # Intake only. The generated board is board_view.regenerate's to push, from its own caller
    # (not sprint-sync), and it short-circuits when the bytes did not move.
    vault_git.push(root, [board_view.INTAKE_REL],
                   f"intake: {len(res['consumed'])} lines", runner=git_runner)
    return res


_PLAN_NEXT_RE = re.compile(r"^-\s*(\S+)\s+(\S+)\s*(?:\|\s*(.*))?$")
# An exact `lines.index(PLAN_NEXT)` match went dead on a hand-typed `## Plan next ` (trailing
# space) or `##  Plan Next`, and the RUNBOOK tells the owner to type the heading by hand. So the
# match is case- and whitespace-insensitive; a typo in *shape* (missing `##`, no "plan next")
# still falls through to "no such heading".
_PLAN_NEXT_HEADING_RE = re.compile(r"^##\s+plan next\s*$", re.IGNORECASE)


def _plan_next_line(line):
    """One `## Plan next` line -> `(board, task_id, note)`, or None when the line is not `- <board>
    <task_id>` / `- <board> <task_id> | <note>` shaped. Pure and permissive about garbage: a
    malformed line is silently skipped, since this section is never consumed by the bridge and
    there is nowhere to attach an error comment."""
    text = line.strip()
    if not text.startswith("-"):
        return None
    m = _PLAN_NEXT_RE.match(text)
    if not m:
        return None
    b, tid, note = m.group(1), m.group(2), (m.group(3) or "").strip()
    if not board.BOARD_RE.fullmatch(b):
        return None
    if not TASK_ID_RE.fullmatch(tid):
        return None
    return (b, tid, note)


def _plan_next_bounds(lines):
    """`(start, end)` of the `## Plan next` section body, or None when the heading is absent."""
    start = None
    for i, line in enumerate(lines):
        if _PLAN_NEXT_HEADING_RE.match(line.rstrip("\r\n")):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].rstrip("\r\n").startswith("## "):
            end = j
            break
    return start, end


def plan_next(text):
    """Every well-formed `## Plan next` line, in file order, as `(board, task_id, note)` (`note` is
    `""` when absent). Empty when the heading is absent or carries nothing parseable. Read-only:
    the planner reads it with `python3 intake.py plan-next` and removes a line, once it has written
    that card's spec, with `remove_plan_next` (`plan-next-remove` on the CLI)."""
    lines = text.splitlines()
    bounds = _plan_next_bounds(lines)
    if bounds is None:
        return []
    start, end = bounds
    out = []
    for line in lines[start + 1:end]:
        parsed = _plan_next_line(line)
        if parsed is not None:
            out.append(parsed)
    return out


def remove_plan_next(text, board_name, task_id):
    """Return `(new_text, removed)`: every `## Plan next` line naming `(board_name, task_id)`
    dropped, everything else byte-identical, including the file's exact trailing newline(s). An
    earlier version normalized EOF through `"\\n".join(...).rstrip("\\n") + "\\n"`, which could eat
    a blank line at the end of the file. `splitlines(keepends=True)` keeps every original line
    terminator, so re-joining untouched lines reproduces them; only matched lines are dropped.
    `(text, False)` when no such line existed: the planner's removal call is safe to repeat."""
    lines = text.splitlines(keepends=True)
    bounds = _plan_next_bounds(lines)
    if bounds is None:
        return text, False
    start, end = bounds
    removed = False
    out = lines[:start + 1]
    for line in lines[start + 1:end]:
        parsed = _plan_next_line(line)
        if parsed is not None and parsed[0] == board_name and parsed[1] == str(task_id):
            removed = True
            continue
        out.append(line)
    out += lines[end:]
    if not removed:
        return text, False
    return "".join(out), True


def apply_remove_plan_next(board_name, task_id, *, vault=None):
    """The CLI's one write outside `add_finding`'s: read Boards/Intake.md, drop the `## Plan next`
    line naming `(board_name, task_id)`, write back if anything changed. Returns `(path, removed)`;
    raises ValueError when there is no vault or no Intake file, as `add_finding` does. No pull/push,
    on purpose, as in `add_finding`: this runs inside the planner's own Claude session (run.sh's
    generic `git add -A` / commit / push after the session exits carries it out)."""
    import board_view
    board_view._refuse_default_vault_under_test(vault)
    root = board_view.vault_root(vault)
    if root is None:
        raise ValueError(f"no vault at {vault or board_view.VAULT}")
    path = root / board_view.INTAKE_REL
    if not path.exists():
        raise ValueError(f"{path} does not exist (the sync tick creates it)")
    text = path.read_text(encoding="utf-8")
    out, removed = remove_plan_next(text, board_name, str(task_id))
    if removed:
        _atomic_write(path, out)
    return path, removed


def finding_line(board_name, risk, title, source):
    """The one line shape the two review jobs may add. It is identical to the `- repo: … | risk: … |
    <title>` grammar `parse_line` accepts, so the owner turns a finding into a card by moving the
    line."""
    return f"- repo: {board_name} | risk: {risk} | {title} ({source})"


def _check_text(what, value):
    if not value or not value.strip():
        raise ValueError(f"empty {what}")
    if "|" in value:
        raise ValueError(f"{what} must not contain '|'")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{what} must be one line")


def insert_finding(text, line):
    """Return `(new_text, appended)`: `line` added at the END of the `## Findings` section, the
    heading created immediately before `## Notes` when absent (or at the end of the file when there
    is no `## Notes`). Every other section is left byte-for-byte, and an exact duplicate of `line`
    appends nothing."""
    lines = text.splitlines()
    if any(l.rstrip() == line for l in lines):
        return text, False
    if FINDINGS in lines:
        start = lines.index(FINDINGS)
        end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        insert = start + 1
        for j in range(start + 1, end):
            if lines[j].strip():
                insert = j + 1          # after the last non-blank body line
        out = lines[:insert] + [line] + lines[insert:]
    elif NOTES in lines:
        i = lines.index(NOTES)
        out = lines[:i] + [FINDINGS, line] + lines[i:]
    else:
        out = lines + [FINDINGS, line]
    return "\n".join(out) + "\n", True


def add_finding(board_name, risk, title, source, *, vault=None, boards_dir=None):
    """Append ONE review finding under `## Findings` in Boards/Intake.md.

    The nightly `security` and monthly `agent-review` sessions read untrusted code and prose, so
    they must not hold Edit on Boards/Intake.md at all: a prompt-injected session with that Edit
    could write a `## New work` or `## Approvals` line and forge a card or a spec approval. This
    verb is the whole privilege they get: one line, validated, in one section.

    It does NOT pull or push: the sync tick's `consume` pulls Intake at the top of the tick and
    pushes it after writing, so a line added here rides out on the next tick (at most ten minutes).

    Returns `(path, appended)`. Raises ValueError on anything it refuses."""
    import board_view
    # Same guard consume/write/regenerate carry: under the suite's own marker
    # an implicit default vault is always a bug, and this function writes.
    board_view._refuse_default_vault_under_test(vault)
    # Capsule's boards, plus the old system's only when present: this verb only appends a text line
    # to Intake.md.
    names = tuple(board.board_names(boards_dir) if boards_dir else board.intake_board_names())
    if board_name not in names:
        raise ValueError(f"no board named {board_name!r} (have: {', '.join(names) or 'none'})")
    if risk not in RISKS:
        raise ValueError(f"risk must be one of {', '.join(RISKS)}, not {risk!r}")
    _check_text("title", title)
    _check_text("source", source)
    line = finding_line(board_name, risk, title.strip(), source.strip())
    # The line has to parse as the `new` verb it imitates, or moving it under
    # `## New work` would produce an inline error instead of a card.
    parsed = parse_line(line, boards=names)
    if parsed["verb"] != "new":
        raise ValueError(f"refusing a line the bridge would reject: {parsed.get('why')}")
    root = board_view.vault_root(vault)
    if root is None:
        raise ValueError(f"no vault at {vault or board_view.VAULT}")
    path = root / board_view.INTAKE_REL
    if not path.exists():
        raise ValueError(f"{path} does not exist (the sync tick creates it)")
    text = path.read_text(encoding="utf-8")
    out, appended = insert_finding(text, line)
    if appended:
        _atomic_write(path, out)
    return path, appended


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="intake.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add-finding",
                       help="append ONE review finding under ## Findings in Boards/Intake.md")
    p.add_argument("board", help="board name; must be one of board.intake_board_names()")
    p.add_argument("risk", help=f"one of {', '.join(RISKS)}")
    p.add_argument("title", nargs="+", help="imperative fix title (no '|')")
    p.add_argument("--source", required=True,
                   help="provenance label, e.g. 'security 2026-09-08' or 'agent review 2026-09'")
    p.add_argument("--vault", help="vault root (defaults to $OBSIDIAN_VAULT_PATH)")
    p.add_argument("--boards-dir",
                   help="boards directory (defaults to $AGENT_OPS_BOARDS_DIR, else "
                        "board.intake_board_names()); tests use this to avoid the live boards "
                        "directory")

    pn = sub.add_parser("plan-next",
                        help="print `## Plan next` entries in Boards/Intake.md, "
                             "one per line (read-only)")
    pn.add_argument("--vault", help="vault root; test-only (see AGENT_OPS_NO_SEND below), "
                                    "production always uses $OBSIDIAN_VAULT_PATH")

    pr = sub.add_parser("plan-next-remove",
                        help="remove one `## Plan next` line naming <board> <task_id> "
                             "- the sprint planner's own write, once it has specced that card")
    pr.add_argument("board")
    pr.add_argument("task_id")
    pr.add_argument("--vault", help="vault root; test-only (see AGENT_OPS_NO_SEND below), "
                                    "production always uses $OBSIDIAN_VAULT_PATH")

    ns = ap.parse_args(argv)

    if ns.cmd in ("plan-next", "plan-next-remove"):
        # run.sh's sprint-plan allowlist has to end `plan-next-remove *` with a wildcard (it takes
        # <board> <task_id>), which ALSO admits `--vault <anywhere>`: a capability the write-guard
        # hook cannot see (it polices Write/Edit, not Bash). This is the code-level fence: outside
        # a test run (AGENT_OPS_NO_SEND unset), --vault is refused outright, so the CLI pins both
        # verbs to the job's own $OBSIDIAN_VAULT_PATH. Tests set AGENT_OPS_NO_SEND (intake's
        # test-isolation marker) and keep passing --vault to point at a scratch vault.
        if ns.vault is not None and not os.environ.get("AGENT_OPS_NO_SEND"):
            sys.stderr.write(f"intake {ns.cmd}: --vault is test-only "
                             "(set AGENT_OPS_NO_SEND, or drop --vault and use "
                             "$OBSIDIAN_VAULT_PATH)\n")
            return 2

    if ns.cmd == "plan-next":
        import board_view
        root = board_view.vault_root(ns.vault)
        if root is None:
            sys.stderr.write(f"intake plan-next: no vault at {ns.vault or board_view.VAULT}\n")
            return 2
        path = root / board_view.INTAKE_REL
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        for b, tid, note in plan_next(text):
            print(f"{b} {tid} | {note}" if note else f"{b} {tid}")
        return 0

    if ns.cmd == "plan-next-remove":
        try:
            path, removed = apply_remove_plan_next(ns.board, ns.task_id, vault=ns.vault)
        except (ValueError, RuntimeError, OSError) as exc:
            sys.stderr.write(f"intake plan-next-remove: {exc}\n")
            return 2
        print(f"{'removed' if removed else 'not in ## Plan next'}: {ns.board} {ns.task_id} ({path})")
        return 0

    boards_dir = ns.boards_dir or os.environ.get("AGENT_OPS_BOARDS_DIR")
    try:
        path, appended = add_finding(ns.board, ns.risk, " ".join(ns.title), ns.source,
                                     vault=ns.vault, boards_dir=boards_dir)
    except (ValueError, RuntimeError, OSError) as exc:
        sys.stderr.write(f"intake add-finding: {exc}\n")
        return 2
    print(f"{'appended to' if appended else 'already in'} {path}")
    return 0


def _atomic_write(path, text):
    """mktemp + replace (the same dance bin/approve-release.sh does in shell), so the phone
    never syncs a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
