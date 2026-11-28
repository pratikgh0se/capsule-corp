#!/usr/bin/env python3
"""Lint the Order Book against its own written policy (mandate M243, M244).

Report-only: wired to no gate; run weekly by the order-book-lint job (M243). The policy lives in
maintenance/templates/order-book.md (`## Policy`, mirrored in the RUNBOOK
"The order book" block); this module checks only what that text lets a machine
check:

- shape: every section the template names exists, and its entries are `- `
  lines of the documented shape;
- the witness rule: each opportunity carries `witness: <id> · <YYYY-MM-DD>`,
  each id is indexed under `## Witnesses`, each index line has id, date, who
  and a link, and an on-deck or live pitch's `## Problem` cites an indexed id;
- the caps: at most 1 live bet, 1 on deck (with `Pitches/<slug>.md`), and 15
  opportunities (the policy's "~15" is read as a hard 15);
- the kill rule: a live bet whose `kill by` date has passed needs an outcome
  row (RUNBOOK "The outcome row") in the decision log.

NOT linted, because no honest mechanical test exists: "no solution words" in
an opportunity line (taste), whether a witness's raw text is really attached
or really a conversation, and whether a pitch is "fully pitched" beyond
existing. A pitch's missing Problem citation is checked only for a pitch file
that exists.

stdlib only. Never writes. Exit 0 clean, 1 violations, 2 usage or unreadable.
"""
import argparse
import datetime
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_log  # noqa: E402

MAX_LIVE, MAX_ON_DECK, MAX_OPPORTUNITIES = 1, 1, 15
SECTIONS = ("Live bet", "On deck", "Opportunities", "Killed", "Witnesses")

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_DATE = r"\d{4}-\d{2}-\d{2}"
_BET_RE = re.compile(rf"^(?P<slug>[a-z0-9][a-z0-9-]*) · appetite .+ · one number: .+ "
                     rf"· kill by (?P<kill>{_DATE})$")
_OPP_RE = re.compile(rf"^.+ · witness: (?P<ids>[^·]+?) · (?P<date>{_DATE})$")
_KILLED_RE = re.compile(rf"^[a-z0-9][a-z0-9-]* · {_DATE} · \S.*$")
_ID_SPLIT_RE = re.compile(r"[,\s]+")
_BET_SHAPE = "`<slug> · appetite … · one number: … · kill by <YYYY-MM-DD>`"


def _parse_sections(text):
    """{section: [(line_no, stripped_line)]} in file order. HTML comments are blanked
    with their newlines kept, so line numbers stay true."""
    blanked = _COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    out, current = {}, None
    for n, line in enumerate(blanked.splitlines(), 1):
        m = re.match(r"^## (.+?)\s*$", line)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
        elif current is not None and line.strip():
            out[current].append((n, line.strip()))
    return out


def _entries(sections, name, problems):
    """A section's `- ` entries; a non-bullet line is a violation, since it would
    escape every count."""
    entries = []
    for n, line in sections.get(name, []):
        if line.startswith("- "):
            entries.append((n, line[2:].strip()))
        else:
            problems.append(f"line {n}: {name}: not a `- ` entry, so it escapes the "
                            f"caps: {line[:60]!r}")
    return entries


def _witness_index(entries, problems):
    ids = {}
    for n, body in entries:
        cells = [c.strip() for c in body.split(" · ")]
        if len(cells) < 4 or not all(cells[:4]) or not re.fullmatch(_DATE, cells[1]):
            problems.append(f"line {n}: Witnesses: index line needs "
                            f"`<id> · <YYYY-MM-DD> · <who> · <link>`: {body[:60]!r}")
            continue
        if cells[0] in ids:
            problems.append(f"line {n}: Witnesses: id {cells[0]!r} already indexed "
                            f"on line {ids[cells[0]]}")
        ids.setdefault(cells[0], n)
    return set(ids)


def _check_opportunities(entries, witnesses, problems):
    if len(entries) > MAX_OPPORTUNITIES:
        problems.append(f"Opportunities: {len(entries)} lines, over the cap of "
                        f"{MAX_OPPORTUNITIES}")
    for n, body in entries:
        m = _OPP_RE.match(body)
        if not m:
            problems.append(f"line {n}: Opportunities: no `witness: <id> · <YYYY-MM-DD>` "
                            f"tail: {body[:60]!r}")
            continue
        for wid in (i for i in _ID_SPLIT_RE.split(m.group("ids")) if i):
            if wid not in witnesses:
                problems.append(f"line {n}: Opportunities: witness {wid!r} is not "
                                f"indexed under Witnesses")


def _bets(entries, name, cap, problems):
    if len(entries) > cap:
        problems.append(f"{name}: {len(entries)} bets, over the cap of {cap}")
    bets = []
    for n, body in entries:
        m = _BET_RE.match(body)
        if m:
            bets.append((n, m.group("slug"), m.group("kill")))
        else:
            problems.append(f"line {n}: {name}: not {_BET_SHAPE}: {body[:60]!r}")
    return bets


def _check_pitch(vault, slug, n, name, witnesses, problems):
    pitch = Path(vault) / "Pitches" / f"{slug}.md"
    if not pitch.is_file():
        if name == "On deck":
            problems.append(f"line {n}: On deck: {slug} has no Pitches/{slug}.md")
        return
    m = re.search(r"(?ms)^## Problem\s*\n(.*?)(?=^## |\Z)", pitch.read_text(encoding="utf-8"))
    cited = m and any(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", m.group(1))
                      for w in witnesses)
    if not cited:
        problems.append(f"line {n}: {name}: Pitches/{slug}.md ## Problem cites no "
                        f"witness id indexed under Witnesses")


def _has_outcome(rows, slug):
    named = re.compile(rf"(?<![\w-]){re.escape(slug)}(?![\w-])")
    return any(named.search(r["topic"])
               and (r["status"] == "outcome" or r["topic"].lower().startswith("outcome:"))
               for r in rows)


def _check_kill(live, rows, today, problems):
    for n, slug, kill in live:
        if kill < today and not _has_outcome(rows or [], slug):
            where = "" if rows is not None else " (no decision log found to look in)"
            problems.append(f"line {n}: Live bet: {slug} kill date {kill} passed with no "
                            f"Outcome row in the decision log{where}; the verdict blocks "
                            f"the next bet")


def lint(text, vault, rows, today):
    """One plain line per violation, empty when the book is clean. `rows` is the parsed
    decision log, or None when there is none."""
    problems = []
    sections = _parse_sections(text)
    for name in SECTIONS:
        if name not in sections:
            problems.append(f"missing section `## {name}`")
    witnesses = _witness_index(_entries(sections, "Witnesses", problems), problems)
    _check_opportunities(_entries(sections, "Opportunities", problems), witnesses, problems)
    live = _bets(_entries(sections, "Live bet", problems), "Live bet", MAX_LIVE, problems)
    deck = _bets(_entries(sections, "On deck", problems), "On deck", MAX_ON_DECK, problems)
    for n, body in _entries(sections, "Killed", problems):
        if not _KILLED_RE.match(body):
            problems.append(f"line {n}: Killed: not `<slug> · <YYYY-MM-DD> · <reason>`: "
                            f"{body[:60]!r}")
    for name, bets in (("Live bet", live), ("On deck", deck)):
        for n, slug, _kill in bets:
            _check_pitch(vault, slug, n, name, witnesses, problems)
    _check_kill(live, rows, today, problems)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order Book policy lint (report-only, "
                                 "wired to no gate; run weekly by the order-book-lint job)")
    ap.add_argument("book", nargs="?",
                    help="Order Book note (default: $OBSIDIAN_VAULT_PATH/Boards/Order Book.md)")
    ap.add_argument("--vault", help="vault root for Pitches/ and Decisions/ (default: "
                    "$OBSIDIAN_VAULT_PATH, else the book's grandparent)")
    ap.add_argument("--decisions", help="decision-log note (default: newest under "
                    "<vault>/Decisions/)")
    ap.add_argument("--today", help="YYYY-MM-DD, for tests (default: today)")
    a = ap.parse_args(argv)
    env_vault = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not a.book and not (a.vault or env_vault):
        print("usage: order_book_lint.py [book] [--vault DIR]  (or set OBSIDIAN_VAULT_PATH)",
              file=sys.stderr)
        return 2
    book = Path(a.book) if a.book else Path(a.vault or env_vault) / "Boards" / "Order Book.md"
    vault = Path(a.vault or (None if a.book else env_vault) or book.resolve().parent.parent)
    if not book.is_file():
        print(f"no order book at {book}", file=sys.stderr)
        return 2
    log = Path(a.decisions) if a.decisions else decision_log.find_log(vault)
    rows = decision_log.parse(log.read_text(encoding="utf-8")) if log and log.is_file() else None
    today = a.today or datetime.date.today().isoformat()
    problems = lint(book.read_text(encoding="utf-8"), vault, rows, today)
    for p in problems:
        print(p)
    print(f"order-book lint: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
