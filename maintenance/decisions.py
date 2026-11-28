#!/usr/bin/env python3
"""decisions.py - the decision gate (spec docs/superpowers/specs/2026-09-06-you-in-the-
loop-design.md)."""
import datetime as _dt
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board  # noqa: E402
import events  # noqa: E402
import vault_conf  # noqa: E402
import vault_git  # noqa: E402
import contracts  # noqa: E402
from contracts import DECISION_PREFIX, DECISION_OPEN, DECISION_ANSWER  # noqa: E402

OPEN_COMMENT = DECISION_OPEN
ANSWER_COMMENT = DECISION_ANSWER
SOURCE_NAME = "decisions"


def _record_intervention(board_name, task_id, via, text):
    """C3/M043: the host's own record of a human intervention, beside the board comment
    that mirrors it."""
    import verdicts
    if verdicts.mode(verdicts.ops_env()) == "off":
        return
    try:
        verdicts.append(board_name, task_id, {"kind": "intervention", "state": "human",
                                              "via": via, "text": text, "source": SOURCE_NAME,
                                              "mirrored": True})
    except Exception as exc:
        events.log("card", SOURCE_NAME, f"verdict record not written: {exc}", board=board_name,
                   task_id=task_id, severity="error",
                   payload={"event": "verdict_write_failed", "state": None})

# U1 incident 1 (2026-09-13): a model wrote the SOUL's `<option> - <trade-off>` with an
# em-dash and the gate rejected a well-formed DECISION ("got 0 options").
_OPT_RE = re.compile(r"^(\d)\.\s+(.+?)\s+[-\u2013\u2014]\s+(.+)$")
_REC_RE = re.compile(r"^Recommend:\s*(\d)\s*[-\u2013\u2014]\s*(.*)$")
_TICK_RE = re.compile(r"^- \[([ xX])\] (?:(\d)\. (.+)|Other: .*)$")
# `board` is interpolated straight into a vault path (note_path/find_note) and a
# kanban --board argument - belt-and-braces alongside the bash wrapper's regex
# (bin/answer-decision.sh), mirroring _specs_path's style.
BOARD_RE = board.BOARD_RE


def parse_decision(reason):
    text = str(reason or "")
    if not text.startswith(DECISION_PREFIX):
        return None
    lines = [l.rstrip() for l in text.splitlines()]
    d = {"question": lines[0][len(DECISION_PREFIX):].strip(), "why": "", "options": [], "recommend": None,
         "reasoning": "", "learn": "", "brainstorm": False, "class": "R3"}
    # state names the field a bare continuation line (matches no prefix) appends to:
    # "why" until the first option, "option" (the last option's trade-off) until
    # Recommend:, "reasoning" until Learn:, then "learn".
    state = None
    for l in lines[1:]:
        if l.startswith("Why:"):
            d["why"] = l[4:].strip()
            state = "why"
        elif l.startswith("Class:"):
            # Lenient by design: the field is a hint on an otherwise
            # well-formed block, so take the LEADING token and fall back to R3 - the
            # conservative default - when it is not an R-class.
            cls = (l[len("Class:"):].strip().split() or ["R3"])[0].strip("()-,.").upper()
            d["class"] = cls if cls in contracts.R_CLASSES else "R3"
            state = None
        elif _OPT_RE.match(l):
            n, opt, trade = _OPT_RE.match(l).groups()
            if int(n) != len(d["options"]) + 1:
                raise ValueError(f"option numbering: expected {len(d['options']) + 1}, got {n}")
            d["options"].append([opt.strip(), trade.strip()])
            state = "option"
        elif _REC_RE.match(l):
            n, r = _REC_RE.match(l).groups(); d["recommend"] = int(n); d["reasoning"] = r.strip()
            state = "reasoning"
        elif l.startswith("Learn:"):
            d["learn"] = l[6:].strip()
            state = "learn"
        elif l.startswith("Brainstorm:"):
            d["brainstorm"] = l[11:].strip().lower() in ("yes", "true", "y")
            state = None
        elif l.strip():
            if state == "option" and d["options"]:
                d["options"][-1][1] += " " + l.strip()
            elif state == "reasoning":
                d["reasoning"] += " " + l.strip()
            elif state == "learn":
                d["learn"] += " " + l.strip()
            elif state == "why":
                d["why"] += " " + l.strip()
    d["options"] = [tuple(o) for o in d["options"]]
    if not 2 <= len(d["options"]) <= 5:
        raise ValueError(f"a decision needs 2 to 5 options, got {len(d['options'])}")
    if d["recommend"] is None or not 1 <= d["recommend"] <= len(d["options"]):
        raise ValueError("Recommend: <n> - <reasoning> is required and n must name an option")
    if not d["question"]:
        raise ValueError("empty question")
    return d


def slugify(text):
    return "-".join(re.findall(r"[a-z0-9]+", str(text).lower()))[:60].strip("-") or "untitled"


def note_path(vault, repo, date, slug, task_id):
    return Path(vault) / "Decisions" / repo / f"{date}-{slug}-{task_id}.md"


NOTES_HINT = "<!-- anything you write here is attached to the card verbatim for the worker -->"


def render_note(d, *, board, card, raised_by, repo, date, title):
    opts = "\n".join(f"{i}. {o} - {t}" for i, (o, t) in enumerate(d["options"], 1))
    ticks = "\n".join(f"- [ ] {i}. {o}" for i, (o, _) in enumerate(d["options"], 1))
    return (f"---\ndate: {date}\ntype: decision\nstatus: open\nboard: {board}\ncard: {card}\nraised_by: {raised_by}\n"
            f"class: {d.get('class', 'R3')}\ntags: [decision, {repo}]\nai-first: true\n---\n\n## For future agent\n"
            f"Decision raised by {raised_by} on card {card} ({title}). "
            f"The owner answers here (tick one option, add notes) "
            f"then replies \"done {card}\" or answers by number. Status flips to decided by "
            f"maintenance/decisions.py; never edit that by hand.\n\n## Question\n{d['question']}\nWhy: {d['why']}\n\n"
            f"## Options\n{opts}\n\n**Recommendation:** {d['recommend']} - {d['reasoning']}\n**What you learn:** {d['learn']}\n\n"
            f"## Your answer\n{ticks}\n- [ ] Other: (write it in the notes)\n\n### Notes\n{NOTES_HINT}\n")


def read_note(text):
    fm = {}
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        for l in text[4:end].splitlines():
            if ":" in l and not l.startswith((" ", "-")):
                k, _, v = l.partition(":"); fm[k.strip()] = v.strip()
    segment = text
    if "## Your answer" in segment:
        segment = segment.split("## Your answer", 1)[1]
    if "### Notes" in segment:
        segment = segment.split("### Notes", 1)[0]
    ticked, options = [], []
    for l in segment.splitlines():
        m = _TICK_RE.match(l)
        if not m:
            continue
        mark, n, opt = m.groups()
        if n:
            options.append(opt.strip())
        if mark in "xX":
            ticked.append(int(n) if n else "other")
    if len(ticked) > 1:
        raise ValueError(f"more than one option ticked: {ticked}")
    notes = ""
    if "### Notes" in text:
        notes = text.split("### Notes", 1)[1]
        notes = notes.split("\n## ", 1)[0].replace(NOTES_HINT, "").strip()
    return {"fm": fm, "ticked": ticked[0] if ticked else None, "notes": notes, "options": options}


def _bridge():
    import board_bridge as bb
    return bb


def _board(name, kanban=None, boards_dir=None):
    return board.open_board(name, runner=kanban, boards_dir=boards_dir)


def blocked_decisions(kanban=None, boards_dir=None):
    boards_dir = boards_dir or _bridge().KANBAN_BOARDS_DIR
    out = []
    for name in board.board_names(boards_dir):
        b = _board(name, kanban, boards_dir)
        try:
            # `triage` is where the old system's block-loop detector parks a card that kept re-
            # blocking (kanban_db.py block_task, ~6406) - a reviewer card raising a
            # second DECISION lands there instead of `blocked`.
            refs = b.list(statuses={"blocked", "triage"})
        except Exception as exc:
            events.log("card", "decisions", f"blocked_decisions: list failed for board {name}: {exc}",
                       board=name, severity="warn", payload={"event": "decision_list_failed"})
            continue
        for ref in refs:
            try:
                card = b.show(ref.id)
            except Exception as exc:
                events.log("card", "decisions", f"blocked_decisions: show failed for {name} {ref.id}: {exc}",
                           board=name, task_id=ref.id, severity="warn",
                           payload={"event": "decision_show_failed"})
                continue
            # Classify from the card's live block record (Card.current_block_reason -
            # board.block_reason, retired by a later `unblocked` task_event), never from
            # card.latest_summary - the old system's newest-run-summary alias outlives the block
            # it described, so a card that was once DECISION: keeps claiming to be one
            # long after it was answered and unblocked.
            reason = card.current_block_reason
            if not reason.startswith(DECISION_PREFIX):
                continue
            out.append({"board": name, "task_id": ref.id,
                        "title": card.title or ref.title or ref.id,
                        "assignee": card.assignee or "?", "reason": reason,
                        "workspace": card.workspace_path or "",
                        "comments": list(card.comments)})
    return out


_MAX_ONELINE = 300


def _oneline(text):
    """Collapse to one line and cap it - protects the card comment and the spec
    line from a multi-line `other:` reply that could inject a markdown heading
    or otherwise break the single-line shapes those two callers promise."""
    return " ".join(str(text).split())[:_MAX_ONELINE]


def find_note(vault, board, task_id):
    if not BOARD_RE.fullmatch(str(board)):
        raise ValueError("bad board")
    cands = []
    unparsable = []  # (path, error) - notes that carry this task_id in their
    # filename but failed to parse (e.g. two ticks); never candidates to act on, but if
    # there is no good open note either, answer() must say why instead of claiming there
    # is no note at all.
    for p in sorted(Path(vault, "Decisions", board).glob("*.md")):
        try:
            fm = read_note(p.read_text(encoding="utf-8"))["fm"]
        except (ValueError, OSError) as exc:
            if task_id in p.name:
                unparsable.append((p, exc))
            continue  # unparsable note (e.g. two ticks) - never the one to act on
        if fm.get("card") == task_id and fm.get("status") == "open":
            cands.append(p)
    if len(cands) > 1:
        raise ValueError(f"two open decisions for {task_id}: {', '.join(p.name for p in cands)} - "
                          "answer the older one first by editing its note status to decided or ask Fable")
    if not cands and unparsable:
        p, exc = unparsable[0]
        raise ValueError(f"cannot read the note {p.name}: {exc}")
    return cands[0] if cands else None


def resolve(note, choice, text=""):
    ticked, options = note["ticked"], note["options"]
    notes = note["notes"] + (("\n" if note["notes"] else "") + text if text else "")
    choice = str(choice).strip().lower()
    if choice == "done":
        if ticked is None:
            raise ValueError("nothing ticked in the note")
        chosen = ticked
    elif choice == "other":
        chosen = "other"
    elif choice.isdigit() and 1 <= int(choice) <= len(options):
        if ticked is not None and ticked != int(choice):
            raise ValueError(f"note says {ticked}, reply says {choice} - which?")
        chosen = int(choice)
    else:
        raise ValueError(f"reply must be 1-{len(options)}, other: <text>, or done")
    option = (text or notes.split("\n")[0] if chosen == "other" else options[chosen - 1])
    return chosen, option, notes


def _spec_for(board_name, task_id, kanban):
    try:
        card = _board(board_name, kanban).show(task_id)
    except (ValueError, subprocess.CalledProcessError, OSError, board.BoardError) as exc:
        events.log("card", "decisions", f"spec lookup failed for {board_name} {task_id}: {exc}",
                   board=board_name, task_id=task_id, severity="warn",
                   payload={"event": "spec_for_failed"})
        return None, ""
    m = re.search(r"spec:\s*(Specs/[A-Za-z0-9_.\-/]+\.md)", card.body)
    # current_block_reason (the card's live block record, gated on status in {blocked,
    # triage} and retired by a later `unblocked` task_event), never latest_summary -
    # the old system's newest-run-summary alias outlives the block it described, so
    # answer() must not keep treating a long-unblocked card as still on a
    # DECISION block.
    return (m.group(1) if m else None), card.current_block_reason


def _specs_path(vault, spec_rel):
    """Resolve spec_rel (as read out of a card body, untrusted) against vault/Specs.
    Returns the resolved Path or None if it escapes Specs/ - never raises."""
    if not spec_rel:
        return None
    specs_root = (Path(vault) / "Specs").resolve()
    candidate = (Path(vault) / spec_rel).resolve()
    try:
        candidate.relative_to(specs_root)
    except ValueError:
        return None
    return candidate


def answer(board, task_id, choice, text="", *, vault, kanban=None, now=None, git_runner=None):
    vault = Path(vault); now = now or _dt.datetime.now()
    if not BOARD_RE.fullmatch(str(board)):
        raise ValueError("bad board")
    # 0. pull: the tick may have been made on the phone; read that copy, not
    # the stale laptop one. A failed pull is a warn event, not a refusal.
    vault_git.pull(vault, runner=git_runner)
    # 1. validate: note exists and parses, the reply resolves against it, and the card
    # is actually blocked on a DECISION - all before any kanban call or file write.
    path = find_note(vault, board, task_id)
    if path is None:
        raise ValueError(f"no open decision note for {board} {task_id}")
    txt = path.read_text(encoding="utf-8")
    note = read_note(txt)
    chosen, option, notes = resolve(note, choice, text)
    spec_rel, reason = _spec_for(board, task_id, kanban)
    if not reason.startswith(DECISION_PREFIX):
        raise ValueError(f"{task_id} is not blocked on a DECISION (reason starts {reason[:20]!r}); refusing to unblock")
    comment_option = _oneline(option)
    # 2. comment, then unblock - the two calls that touch the card.
    b = _board(board, kanban)
    try:
        comment_text = f"{ANSWER_COMMENT}{chosen} - {comment_option} / notes: {notes}"
        b.comment(task_id, comment_text)
        _record_intervention(board, task_id, "decisions.answer", comment_text)
        b.unblock(task_id)
    except Exception as exc:
        # Board wraps a CLI failure in board.LegacyCliError; unwrap back to the original
        # subprocess/OSError so this still raises the same shape _kanban used to, per
        # the comment above.
        cause = getattr(exc, "__cause__", None)
        if isinstance(cause, (subprocess.CalledProcessError, OSError)):
            raise cause from exc
        raise
    # 3. only now touch files: flip the note (frontmatter block only, so a stray
    # "status: open" elsewhere in the body is never touched), tick the answer, append
    # the full (uncollapsed) Decision section.
    fm_end = txt.find("\n---", 4)
    fm, rest = txt[:fm_end], txt[fm_end:]
    fm = fm.replace("status: open\n", f"status: decided\ndecided: {chosen}\ndecided_at: {now.strftime('%Y-%m-%dT%H:%M')}\n", 1)
    txt = fm + rest
    if chosen != "other":
        txt = re.sub(rf"^- \[ \] {chosen}\. ", f"- [x] {chosen}. ", txt, count=1, flags=re.M)
    else:
        txt = txt.replace("- [ ] Other:", "- [x] Other:", 1)
    txt = txt.rstrip("\n") + f"\n\n## Decision\n{option}\n\n{notes}\n"
    path.write_text(txt, encoding="utf-8")
    # 4. spec line, best-effort: only inside vault/Specs, only if it exists, collapsed
    # and capped like the comment so an `other:` reply can't inject a heading (or
    # several kilobytes) into the spec file.
    sp = _specs_path(vault, spec_rel)
    if sp is not None and sp.exists():
        notes_first = _oneline(notes.split("\n", 1)[0])
        sp.write_text(sp.read_text(encoding="utf-8").rstrip("\n") + f"\nDecided ({now.date().isoformat()}): {comment_option} - {notes_first}\n", encoding="utf-8")
    events.log("card", "decisions", f"decided {chosen}: {option[:120]}", board=board, task_id=task_id,
               payload={"event": "decision_answered", "state": "closed", "choice": str(chosen)})
    pushed = [path.relative_to(vault).as_posix()] + ([spec_rel] if sp is not None and sp.exists() else [])
    vault_git.push(vault, pushed, f"decisions: {task_id} decided {chosen}", runner=git_runner)
    return f"{board} {task_id}: decided {chosen} - {option}"


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="decision gate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("answer"); a.add_argument("board"); a.add_argument("task_id"); a.add_argument("choice"); a.add_argument("text", nargs="*")
    ns = ap.parse_args(argv)
    vault = vault_conf.vault()
    if vault is None:
        print(f"{ns.board} {ns.task_id}: not applied - {vault_conf.NO_VAULT}")
        sys.exit(2)
    try:
        print(answer(ns.board, ns.task_id, ns.choice, " ".join(ns.text), vault=str(vault)))
    except (ValueError, subprocess.CalledProcessError, OSError, board.BoardError) as exc:
        # board.BoardError: Board.unblock's TransitionRefused (a triage-parked card
        # - see its docstring) is a BoardError, not a plain CLI failure - without
        # this in the tuple it fell through to an unhandled traceback instead of the
        # same "not applied - ..." exit 2 every other refusal gets (Opus review
        # blocker 2).
        print(f"{ns.board} {ns.task_id}: not applied - {exc}")
        sys.exit(2)


if __name__ == "__main__":
    main()
