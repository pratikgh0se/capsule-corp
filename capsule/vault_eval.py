"""MEM-13 unit V5: the vault retrieval eval set and gate (spec "Vault context for workers" 6, 7.4).
`bin/vault-eval build|score|nightly`. The counter below is capsule's own oracle (Z2): it shares no
code with the skill's retrieval_eval.py. Exit codes: 0 pass, 1 a threshold missed, 2 run invalid
(the two recall@5 counters disagree, or nightly has no skill figure), 3 setup problem or fewer
than --min-cases cases."""
import argparse
import functools
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

from capsule import citations, config, paths

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "maintenance" / "state" / "vault_eval"
STALE = {"superseded", "declined", "rejected", "archived", "obsolete", "parked", "on-hold"}
Q_BODY = 1200
MIN_NEGATIVES = 10
AGREE = 0.01


@functools.cache
def _vault_ops():
    root = os.environ.get("OBSIDIAN_SKILL_ROOT")
    if not root:
        raise OSError("OBSIDIAN_SKILL_ROOT is not set")
    path = Path(root) / "integrations" / "obsidian-mcp-server"
    sys.path.insert(0, str(path))   # vault_ops imports its sibling modules
    spec = importlib.util.spec_from_file_location("vault_ops", path / "vault_ops.py")
    vault_ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vault_ops)
    return vault_ops


def search(query, limit=10, semantic=None):
    """The one search call (swap point for bin/vault-search): the skill's vault_ops.search."""
    return _vault_ops().search(query, limit=limit, semantic=semantic)


def _status(vault, rel):
    try:
        head = (Path(vault) / rel).read_text(encoding="utf-8")[:2000]
    except OSError:
        return ""
    m = re.search(r"(?m)^status:\s*['\"]?([A-Za-z0-9_-]+)", head)
    return m.group(1).lower() if m else ""


def run_search(cases, vault, semantic=None):
    """{case id: [{"path", "stale"}]} from live search over the cases' questions."""
    os.environ["OBSIDIAN_VAULT_PATH"] = str(vault)
    return {c["id"]: [{"path": r["path"], "stale": _status(vault, r["path"]) in STALE}
                      for r in search(c["q"], 10, semantic)] for c in cases}


def _notes(vault):
    vault = Path(vault)
    found = {p.relative_to(vault).as_posix() for p in vault.rglob("*.md")
             if not any(part.startswith(".") for part in p.relative_to(vault).parts)}
    return found, {Path(p).stem: p for p in sorted(found)}


def _resolve(name, notes, stems):
    if not name.endswith(".md"):   # [[Alpha]] by stem, [[Specs/Alpha]] by folder path
        return name + ".md" if name + ".md" in notes else stems.get(name)
    return max((p for p in notes if name == p or name.endswith(" " + p)), key=len, default=None)


def _body_paths(body, notes, stems):
    """(existing vault paths the body names, count of named paths that do not exist)."""
    names = [n.strip() for n in re.findall(r"\[\[([^\]|#]+)", body)]
    names += re.findall(r"[\w&()-][\w .&()/-]*?\.md", body)
    found = [_resolve(n, notes, stems) for n in names]
    return sorted({p for p in found if p}), sum(1 for p in found if not p)


def _card_cases(db, vault):
    """One case per done or archived card that has gold; (cases, skipped, dropped paths)."""
    notes, stems = _notes(vault)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cards = conn.execute("SELECT id, title, body FROM tasks WHERE status IN ('done','archived')"
                         " ORDER BY id").fetchall()
    conn.close()
    cases, skipped, dropped = [], 0, 0
    for cid, title, body in cards:
        body = body or ""
        paths, bad = _body_paths(body, notes, stems)
        gold = [f"row {n}" for n in citations.cited_rows(body)] + paths
        dropped += bad
        if not gold:
            skipped += 1
            continue
        cases.append({"id": f"card:{cid}", "source": "card", "gold": gold,
                      "q": f"{title}\n{body[:Q_BODY]}"})
    return cases, skipped, dropped


def _verdict_cases(path, vault):
    """kept and missing become positives, dropped a negative; (cases, dropped targets, no-q)."""
    notes, _ = _notes(vault)
    cases, dropped, no_q = [], 0, 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        v = json.loads(line) if line.strip() else {}
        target = v.get("path") or (f"row {v['row']}" if v.get("row") else None)
        if v.get("verdict") not in ("kept", "missing", "dropped") or not target:
            continue
        if not v.get("q"):
            no_q += 1
            continue
        if not target.startswith("row ") and target not in notes:
            dropped += 1
            continue
        neg = v["verdict"] == "dropped"
        cid = "verdict:" + hashlib.sha1((v["q"] + target).encode()).hexdigest()[:10]
        cases.append({"id": cid, "source": "verdict", "q": v["q"],
                      "gold": [] if neg else [target], "negative": [target] if neg else []})
    return cases, dropped, no_q


def save_cases(out, new, source):
    """Write `new` and keep the other source's cases already in `out`."""
    out = Path(out)
    old = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if out.exists() else []
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [c for c in old if c["source"] != source] + new
    out.write_text("".join(json.dumps(c) + "\n" for c in rows), encoding="utf-8")


def _path(item):
    return item.get("path") if isinstance(item, dict) else item


def counter(cases, results):
    """recall@5, MRR, stale hits in the top 3 and bad-hit@3 (None below 10 negatives)."""
    pos = [c for c in cases if c["gold"]]
    neg = [c for c in cases if c.get("negative")]
    ranks = []
    for c in pos:
        paths = [_path(i) for i in results.get(c["id"], [])]
        ranks.append(next((n for n, p in enumerate(paths, 1) if p in c["gold"]), 0))
    top3 = {c["id"]: results.get(c["id"], [])[:3] for c in cases}
    stale = sum(any(isinstance(i, dict) and i.get("stale") for i in t) for t in top3.values())
    bad = sum(any(_path(i) in c["negative"] for i in top3[c["id"]]) for c in neg)
    return {"cases": len(pos), "negatives": len(neg),
            "recall_at_5": sum(0 < r <= 5 for r in ranks) / max(len(pos), 1),
            "mrr": sum(1 / r for r in ranks if r) / max(len(pos), 1),
            "stale_hits": stale, "stale_hit_at_3": stale / len(cases) if cases else 0.0,
            "bad_hit_at_3": bad / len(neg) if len(neg) >= MIN_NEGATIVES else None}


def gate(m, lexical, external, need_external=False):
    """(failed metric names, run valid): thresholds of spec section 6 plus the two-counter check."""
    failed = [k for k, bad in (
        ("recall_at_5", m["recall_at_5"] < 0.70), ("mrr", m["mrr"] < 0.50),
        ("stale_hits", m["stale_hits"] > 0),
        ("bad_hit_at_3", m["bad_hit_at_3"] is not None and m["bad_hit_at_3"] > 0.10),
        ("lexical_recall_at_5", lexical is not None and lexical["recall_at_5"] < 0.50)) if bad]
    disagree = external is not None and abs(external - m["recall_at_5"]) > AGREE
    return failed, not (disagree or (need_external and external is None))


def score(cases, results, out_dir, lexical_results=None, external=None, min_cases=40,
          need_external=False):
    """Exit 0 pass, 1 threshold missed, 2 invalid (counters disagree or external missing when
    needed), 3 too few cases. The dated JSON is written in every case."""
    m = counter(cases, results)
    lex = counter(cases, lexical_results) if lexical_results is not None else None
    failed, valid = gate(m, lex, external, need_external)
    small = m["cases"] < min_cases
    doc = {"date": date.today().isoformat(), "metrics": m, "lexical": lex, "failed": failed,
           "external_recall_at_5": external, "two_oracles": external is not None,
           "valid": valid, "too_few_cases": small}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / f"result-{doc['date']}.json").write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc))
    return 3 if small else (2 if not valid else int(bool(failed)))


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _external(text):
    """The skill's `retrieval_eval.py --json` recall@5, or None when it is missing or garbled."""
    try:
        doc = json.loads(text)
        if "recall_at_5" in doc:
            return float(doc["recall_at_5"])
        return float(doc.get("summary", doc)["recall_at"]["5"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _skill_external(cases, vault):
    """Second oracle: the skill's retrieval_eval.py scoring this same engine on the same cases."""
    pos = STATE / "cases-pos.jsonl"
    pos.write_text("".join(json.dumps({"q": c["q"], "gold": c["gold"]}) + "\n"
                           for c in cases if c["gold"]), encoding="utf-8")
    me = [sys.executable, str(ROOT / "bin" / "vault-eval"), "query"]
    env = {**os.environ, "OBSIDIAN_VAULT_PATH": str(vault),
           "RETRIEVAL_EVAL_EXTERNAL_CMD": json.dumps(me)}
    script = Path(os.environ["OBSIDIAN_SKILL_ROOT"]) / "scripts" / "eval" / "retrieval_eval.py"
    run = subprocess.run([sys.executable, str(script), "--mode", "external", "--cases", str(pos),
                          "--json"], env=env, capture_output=True, text=True, timeout=280)
    return _external(run.stdout)


def mode(home):
    """`redesign.vault_context` from the first board's config; anything unreadable or not an
    object reads as `off`."""
    names = sorted({p.parent.name for f in ("config.json", "kanban.db")
                    for p in (Path(home) / "boards").glob(f"*/{f}")})
    if not names:
        return "off"
    try:
        cfg = _load(paths.resolve_config(home, names[0]))   # falls back to the legacy file
    except (OSError, ValueError):
        return "off"
    if not isinstance(cfg, dict) or not isinstance(cfg.get("redesign", {}), dict):
        return "off"
    return config.redesign(cfg).get("vault_context", "off")


def _nightly():
    """No-op unless redesign.vault_context is report or live (default off). Needs the skill's
    figure too: without it the run is invalid (exit 2)."""
    home = paths.home()
    dbs = sorted((home / "boards").glob("*/kanban.db"))
    if mode(home) not in ("report", "live"):
        return 0
    vault = os.environ.get("OBSIDIAN_VAULT_PATH", "")
    out = STATE / "cases.jsonl"
    main(["build", "--vault", vault, "--out", str(out)]
         + [x for db in dbs for x in ("--board-db", str(db))])
    if (STATE / "verdicts.jsonl").exists():
        main(["build", "--vault", vault, "--out", str(out), "--from-verdicts",
              str(STATE / "verdicts.jsonl")])
    cases = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]
    return score(cases, run_search(cases, vault), STATE, run_search(cases, vault, semantic=False),
                 _skill_external(cases, vault), need_external=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vault-eval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--board-db", action="append", default=[])
    b.add_argument("--vault", required=True)
    b.add_argument("--out", default=str(STATE / "cases.jsonl"))
    b.add_argument("--from-verdicts")
    s = sub.add_parser("score")
    s.add_argument("--cases", required=True)
    s.add_argument("--results", help="search JSON {case id: [paths]}; omitted = search now")
    s.add_argument("--lexical-results")
    s.add_argument("--external")
    s.add_argument("--vault", default=os.environ.get("OBSIDIAN_VAULT_PATH", ""))
    s.add_argument("--out-dir", default=str(STATE))
    s.add_argument("--min-cases", type=int, default=40)
    sub.add_parser("nightly")
    sub.add_parser("query").add_argument("text")   # the engine retrieval_eval.py scores
    a = ap.parse_args(argv)
    try:
        return _run(a)
    except (OSError, ValueError, sqlite3.Error) as err:
        print(f"vault-eval: {err}", file=sys.stderr)
        return 3


def _run(a):
    if a.cmd == "nightly":
        return _nightly()
    if a.cmd == "query":
        print(json.dumps([r["path"] for r in search(a.text, 10)]))
        return 0
    if a.cmd == "build":
        if a.from_verdicts:
            new, dropped, no_q = _verdict_cases(a.from_verdicts, a.vault)
            save_cases(a.out, new, "verdict")
            print(json.dumps({"cases": len(new), "dropped_paths": dropped, "skipped_no_q": no_q}))
            return 0
        res = [_card_cases(db, a.vault) for db in a.board_db]
        save_cases(a.out, [c for r in res for c in r[0]], "card")
        print(json.dumps({"cases": sum(len(r[0]) for r in res), "skipped_no_gold":
                          sum(r[1] for r in res), "dropped_paths": sum(r[2] for r in res)}))
        return 0
    cases = [json.loads(x) for x in Path(a.cases).read_text(encoding="utf-8").splitlines() if x]
    results = _load(a.results) if a.results else run_search(cases, a.vault)
    lexical = _load(a.lexical_results) if a.lexical_results else None
    ext = _external(Path(a.external).read_text(encoding="utf-8")) if a.external else None
    return score(cases, results, a.out_dir, lexical, ext, a.min_cases,
                 need_external=bool(a.external))
