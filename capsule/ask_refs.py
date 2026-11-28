"""Source readers for the ask gate (split out of `capsule/ask_gate.py`, no change in behaviour):
the files a dossier quote may sit in (ledgers, the repo, the vault, project clones, incidents)
and the word-for-word quote check."""
import functools
import glob
import hashlib
import json
from pathlib import Path

from capsule import answer_base, host, paths, row_schemas, workspace

REQUIRED_SOURCES = ("ledger", "repo", "incidents")
MAX_READ = 2_000_000
MIN_QUOTE = 20                                   # a quote shorter than this proves nothing


def _flat(text):
    return " ".join(str(text or "").split())



def _read(path):
    try:
        with open(path, errors="replace") as fh:
            return fh.read(MAX_READ)
    except OSError:
        return None


def _ledger_files(home):
    """The resolved ledger files (host key `diligence.ledgers`; a relative entry sits under the
    vault)."""
    vault = host.vault_path(home)
    found = []
    for entry in host.get_or_default("diligence.ledgers", home):
        pattern = str(Path(entry).expanduser())
        if not Path(pattern).is_absolute():
            pattern = str(vault / entry) if vault else ""
        found += [Path(p).resolve() for p in sorted(glob.glob(pattern))] if pattern else []
    return found


def _roots(home, board):
    """The directories a ref may sit under: the Capsule product root, the vault and the board's
    own project clones (`<board>/work/<slug>/`)."""
    vault = host.vault_path(home)
    work = paths.board_dir(board, home) / "work"
    kids = work.iterdir() if work.is_dir() else []
    clones = sorted(p.resolve() for p in kids if p.is_dir() and not p.is_symlink())
    return [paths.REPO.resolve(), *([vault.resolve()] if vault else []), *clones]


def _open_ref(home, board, ref):
    """The text of the file `ref` names, only when it sits under a root of `_roots` or is a
    ledger file; else None. A relative ref is tried under each root. A path is resolved first, so
    `..` and a symlink cannot leave a root (the work folder is not a root, only each clone)."""
    roots = _roots(home, board)
    name = Path(ref)
    for cand in ([name] if name.is_absolute() else [r / name for r in roots]):
        cand = cand.resolve()
        if cand.is_file() and (cand in _ledger_files(home) or any(
                cand.is_relative_to(r) for r in roots)):
            return _read(cand)
    return None


def _source(conn, home, board, source, ref):
    """The text a quote must sit in. Source `ledger`: only the ledger files (`_ledger_texts`).
    Else the file `ref` names, else the incident row (source `incidents`); None when none."""
    if source == "ledger":
        return "\n".join(_ledger_texts(home, ref))
    text = _open_ref(home, board, ref)
    if text is not None:
        return text
    row = conn.execute("SELECT * FROM incidents WHERE id=?", (ref,)).fetchone()
    return json.dumps(dict(row)) if source == "incidents" and row else None


def _quote_problems(conn, home, doc):
    out = []
    for entry in doc["searched"]:
        for hit in entry["hits"]:
            text = _source(conn, home, doc["board"], entry["source"], hit["ref"])
            if text is None:
                out.append(f"hit ref {hit['ref']!r} is not a source the gate can open")
            elif len(_flat(hit["quote"])) < MIN_QUOTE:
                out.append(f"quote is shorter than {MIN_QUOTE} characters: {hit['quote'][:30]!r}")
            elif _flat(hit["quote"]) not in _flat(text):
                out.append(f"quote is not word for word in {hit['ref']!r}: {hit['quote'][:60]!r}")
    return out


def _ledger_quote_ok(home, ref, quote):
    """True when `quote` sits word for word in an owner ledger: the ledger file `ref` names, else
    any ledger file. Only the ledgers count (never the repo, never an earlier autonomous
    decision), and a short quote proves nothing."""
    want = _flat(quote)
    return len(want) >= MIN_QUOTE and any(want in _flat(t) for t in _ledger_texts(home, ref))


def _ledger_texts(home, ref):
    """The text of each ledger file `ref` names. A bare id (as `D335`) names none, so it reads
    every ledger file; a path that is no ledger file gives nothing. Nothing else is a ledger: not
    the repo, not the vault at large, not a project clone a worker can write."""
    files = _ledger_files(home)
    named = [p for p in files if ref and (str(p) == ref or str(p).endswith("/" + ref.lstrip("/")))]
    is_path = "/" in ref or bool(Path(ref).suffix)
    return [_read(p) or "" for p in (named if named or is_path else files)]


def dossier_id(board, subject, question):
    key = "\n".join((board, subject, _flat(question).lower()))
    return "dq_" + hashlib.sha256(key.encode()).hexdigest()[:8]


def fingerprint(conn, subject):
    """The incident fingerprint of a card (as the card or as the fix card), else the card id."""
    row = conn.execute("SELECT fingerprint FROM incidents WHERE task_id=? OR fix_card=?"
                       " ORDER BY opened_at DESC LIMIT 1", (subject, subject)).fetchone()
    return row[0] if row else subject


def _plain_problems(doc):
    from capsule import cos_ask
    problems = []
    for name, text in (doc.get("plain") or {}).items():
        try:
            cos_ask._lint(text)
        except ValueError as exc:
            problems.append(f"plain.{name}: {exc}")
    return problems


def _field_problems(doc, conn):
    ids = [o["id"] for o in doc["options"]]
    seen = {e["source"] for e in doc["searched"]}
    checks = (
        (conn.execute("SELECT 1 FROM tasks WHERE id=?", (doc["subject"],)).fetchone() is None,
         "subject is not a card on this board"),
        (len(set(ids)) != len(ids), "option ids repeat"),
        (doc["recommended"] not in ids, "recommended is not an option id"),
        (doc["reversible"] and not doc.get("undo"), "undo is required when reversible is true"),
        (doc["id"] != dossier_id(doc["board"], doc["subject"], doc["question"]),
         "id is not the id of board, subject and question"),
        (doc["fingerprint"] != fingerprint(conn, doc["subject"]),
         "fingerprint is not the incident root key (or the card id)"),
        (not set(REQUIRED_SOURCES) <= seen, f"searched must name {', '.join(REQUIRED_SOURCES)}"))
    return [why for bad, why in checks if bad]


def problems(conn, home, doc):
    """[] or one message per defect (rule R0). A bad shape stops the other checks."""
    found = row_schemas.problems("dossier.v1", doc)
    if found:
        return found
    return _field_problems(doc, conn) + _quote_problems(conn, home, doc) + _plain_problems(doc)


def panel_args(home, board, doc, cfg, now, runner):
    """The arguments of `ask_panel.run_panel`: the quote check holds only the owner ledgers, and
    the engineering seat looks at the repo of the card (its project clone, else Capsule)."""
    ctx = {"check_quote": functools.partial(_ledger_quote_ok, home), "runner": runner,
           "ledgers": _ledger_files(home), "repo": _repo_root(home, board, doc["subject"])}
    return home, board, doc, cfg, now, ctx


def _repo_root(home, board, subject):
    """The card's work clone `<board>/work/<slug>/` when it exists, else the Capsule root."""
    conn = answer_base.connect_ro_tolerant(paths.board_db(board, home))
    try:
        row = conn.execute("SELECT branch_name FROM tasks WHERE id=?", (subject,)).fetchone()
    finally:
        conn.close()
    work = paths.board_dir(board, home) / "work"
    clone = work / workspace._slug(row[0]) if row and row[0] else None
    return clone if clone and clone.is_dir() else paths.REPO
