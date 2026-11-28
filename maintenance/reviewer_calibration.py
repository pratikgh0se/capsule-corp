#!/usr/bin/env python3
"""reviewer_calibration.py - how many planted bugs does Capsule's reviewer catch? (R15a)

A case is a directory under `calibration/reviewer/` (write-protected: `calibration/**` is in
`workspace.PROTECTED`): `case.json` (typed by
`calibration/reviewer/case.schema.json`) plus the unified diff it names. An optional `base/` holds
the pre-patch files the diff applies to, each named `<path>.base` so linters and test discovery
skip them; the diff's context and removed lines are checked against them, so a real run can build
the reviewer's workspace from the case alone. A `planted` case names the bug and where it sits
(`bug.locations`, file and new-file line); a `clean` case has no bug, so any finding on it is a
false flag.

Report only: the reviewer's contract is never changed from here (Z3, Z4). This module makes no
model call, no network call and no docker call.
  - Without `--verdicts`: a DRY RUN. Every case is validated, the reviewer bundle that would be
    sent is built by `capsule.bundles.reviewer` (the same builder the tick uses), and a dated JSON
    with `"dry_run": true` and each case's planned inputs is written. Rates are null.
  - With `--verdicts FILE` (a JSON object, case id -> `review_verdict.v1` document with role
    `reviewer` and the case's task id, written by a later real run): the same file is written with
    `"dry_run": false`, each verdict scored, and the catch rate and false-flag rate recorded. Every
    scored row lists the findings that matched a listed location (`matched`), a missed row too,
    so a person can audit each catch and each miss.

Scoring. A planted bug is caught only when the verdict is `fail` and a finding sits in a file named
in `bug.locations`, within LINE_TOLERANCE lines of that location's line (a leading `./`, `a/` or
`b/` on a finding's path is ignored). A `pass` verdict never catches, even with the right finding.
A finding at line 0 (file-level) never catches. A finding only counts for a file that is listed, so
a test file counts only when the case lists it (as the tax case does). Extra wrong findings on a
planted case are not measured yet.

R37 requirement: the real run builds the reviewer's workspace from the case alone (`base/` with
`.base` dropped, plus the patch), never from a capsule-corp clone: workers can read `calibration/`,
so a clone would hold the answers.
"""
import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # `capsule`

import events  # noqa: E402
from capsule import bundles, jsonschema, paths, safe_write  # noqa: E402

CASES = paths.REPO / "calibration" / "reviewer"
SCHEMA_FILE = "case.schema.json"
VERDICT_SCHEMA = paths.SCHEMAS / "review_verdict.v1.json"
RESULT_SCHEMA = "reviewer_calibration_result.v1"
LINE_TOLERANCE = 3
SUFFIX = ".base"   # base/pager.py.base: the suffix keeps linters and test discovery off base/
_STRIP_RE = re.compile(r"^(\./|a/|b/)")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _norm(path):
    """A path as the scorer compares it: no leading `./`, `a/` or `b/`; None for /dev/null."""
    path = path.split("\t")[0].strip()
    return None if path == "/dev/null" else _STRIP_RE.sub("", path)


def _body_lines(patch):
    """Yield (old file, new file, sign, old line no, new line no, text) for each hunk body line,
    and one line with sign None per file header pair."""
    old_file = new_file = None
    old_no = new_no = left_old = left_new = 0
    for text in patch.splitlines():
        sign = text[:1]
        if (left_old or left_new) and sign in " +-":
            yield old_file, new_file, sign, old_no, new_no, text[1:]
            old_no, left_old = old_no + (sign != "+"), left_old - (sign != "+")
            new_no, left_new = new_no + (sign != "-"), left_new - (sign != "-")
            continue
        hunk = _HUNK_RE.match(text)
        if hunk:
            old_no, new_no = int(hunk.group(1)), int(hunk.group(3))
            left_old, left_new = int(hunk.group(2) or 1), int(hunk.group(4) or 1)
        elif text.startswith("--- "):
            old_file = _norm(text[4:])
        elif text.startswith("+++ "):
            new_file = _norm(text[4:])
            yield old_file, new_file, None, 0, 0, ""   # a file header pair


def _added_lines(patch):
    """{file: set of new-file line numbers the patch adds}."""
    added = {}
    for _, new_file, sign, _, new_no, _ in _body_lines(patch):
        if sign == "+":
            added.setdefault(new_file, set()).add(new_no)
    return added


def _snapshot(root, name):
    """The base/ copy of `name` when it is a real file under `root` (symlinks and `..` resolved)."""
    target = (root / (name + SUFFIX)).resolve()
    return target if root in target.parents and target.is_file() else None


def _apply_problems(base, patch):
    """One message per file the patch names that does not match its copy under `base`: an old file
    needs a copy whose context and removed lines match, a new file must have none."""
    root, texts, bad = Path(base).resolve(), {}, []
    for old_file, new_file, sign, old_no, _, text in _body_lines(patch):
        name = old_file or new_file
        if sign is None:
            if name is None:   # /dev/null on both sides names no file at all
                bad.append("/dev/null")
            elif bool(_snapshot(root, name)) == (old_file is None):
                bad.append(name)
        elif sign != "+" and old_file and old_file not in bad:
            if old_file not in texts:
                snap = _snapshot(root, old_file).read_text(encoding="utf-8", errors="replace")
                texts[old_file] = snap.splitlines()
            lines = texts[old_file]
            if not 1 <= old_no <= len(lines) or lines[old_no - 1] != text:
                bad.append(old_file)
    return [f"{n}: the patch does not apply to base/{n}{SUFFIX}" for n in dict.fromkeys(bad)]


def _problems(case_dir, schema):
    """One message per problem with the case in `case_dir`; [] when it is sound."""
    try:
        doc = json.loads((case_dir / "case.json").read_text())
    except (OSError, ValueError) as err:
        return [f"{case_dir.name}: case.json unreadable: {err}"]
    found = [f"{case_dir.name}: {p}" for p in jsonschema.validate(doc, schema)]
    if found:
        return found
    if doc["id"] != case_dir.name:
        found.append(f"{case_dir.name}: id {doc['id']!r} must equal the directory name")
    if (doc["kind"] == "planted") != ("bug" in doc):
        found.append(f"{case_dir.name}: a planted case names its bug, a clean case has none")
    try:
        patch = (case_dir / doc["patch"]).read_text()
    except OSError as err:
        return found + [f"{case_dir.name}: patch unreadable: {err}"]
    added = _added_lines(patch)
    for loc in doc.get("bug", {}).get("locations", []):
        if loc["line"] not in added.get(_norm(loc["file"]), ()):
            where = f"{loc['file']}:{loc['line']}"
            found.append(f"{case_dir.name}: {where} is not a line the patch adds")
    if (case_dir / "base").is_dir():
        found += [f"{case_dir.name}: {p}" for p in _apply_problems(case_dir / "base", patch)]
    return found


def _load_cases(cases_dir):
    """(cases, problems): each case is its document plus `patch_text`, in id order."""
    cases_dir = Path(cases_dir)
    try:
        schema = jsonschema.load_schema(cases_dir / SCHEMA_FILE)
    except (OSError, ValueError) as err:
        return [], [f"{SCHEMA_FILE} unusable: {err}"]
    cases, problems = [], []
    for case_json in sorted(cases_dir.glob("*/case.json")):
        found = _problems(case_json.parent, schema)
        problems += found
        if not found:
            doc = json.loads(case_json.read_text())
            doc["patch_text"] = (case_json.parent / doc["patch"]).read_text()
            cases.append(doc)
    return cases, problems


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _planned(case):
    """What a real run would send: the reviewer bundle, as sizes and hashes."""
    bundle = bundles.reviewer(case["task"], case["patch_text"],
                              card_hash=bundles.card_hash(case["task"]["body"]))
    sizes = {"system": bundle["system"], "prompt": bundle["prompt"], "diff": case["patch_text"]}
    out = {f"{k}_bytes": len(v.encode("utf-8")) for k, v in sizes.items()}
    out.update({f"{k}_sha256": _sha256(sizes[k]) for k in ("system", "prompt")})
    return out


def _brief(finding):
    return {k: finding[k] for k in ("file", "line", "class", "text")}


def _outcome(case, verdict):
    """(outcome, findings that decided it): caught/missed for a planted case, false_flag/ok for a
    clean one. A planted row lists the findings matching a listed location, caught or not; a
    clean row lists every finding raised."""
    found = verdict["findings"]
    if case["kind"] == "clean":
        flagged = verdict["verdict"] == "fail" or bool(found)
        return ("false_flag" if flagged else "ok"), [_brief(f) for f in found]
    spots = [(_norm(loc["file"]), loc["line"]) for loc in case["bug"]["locations"]]
    matched = [_brief(f) for f in found if f["line"] >= 1 and any(
        _norm(f["file"]) == name and abs(f["line"] - line) <= LINE_TOLERANCE
        for name, line in spots)]
    caught = verdict["verdict"] == "fail" and bool(matched)
    return ("caught" if caught else "missed"), matched


def _scored(rows, kind):
    return [r for r in rows if r["kind"] == kind and r["outcome"]]


def _rate(rows, kind, hit):
    """Share of the scored `kind` rows whose outcome is `hit`; None when none were scored."""
    scored = _scored(rows, kind)
    return round(sum(r["outcome"] == hit for r in scored) / len(scored), 4) if scored else None


def _build_result(cases, verdicts, date):
    """The dated report: one row per case, then the summary. No `verdicts` means a dry run."""
    rows = []
    for case in cases:
        verdict = verdicts.get(case["id"])
        outcome, matched = _outcome(case, verdict) if verdict else (None, None)
        rows.append({"id": case["id"], "kind": case["kind"], "origin": case["origin"],
                     "bug": case.get("bug"), "planned": _planned(case),
                     "verdict": verdict["verdict"] if verdict else None,
                     "outcome": outcome, "matched": matched})
    return {"schema": RESULT_SCHEMA, "date": date, "dry_run": not verdicts, "cases": rows,
            "summary": {"cases": len(rows), "planted_scored": len(_scored(rows, "planted")),
                        "clean_scored": len(_scored(rows, "clean")),
                        "catch_rate": _rate(rows, "planted", "caught"),
                        "false_flag_rate": _rate(rows, "clean", "false_flag")}}


def _load_verdicts(path, cases):
    """{case id: verdict document}: a JSON object whose every key is a case id and whose every
    value is a `review_verdict.v1` reviewer verdict for that case's task; raises ValueError."""
    verdicts = json.loads(Path(path).read_text())
    if not isinstance(verdicts, dict):
        raise ValueError("the file must be a JSON object, case id -> verdict")
    task_ids = {c["id"]: c["task"]["id"] for c in cases}
    schema = jsonschema.load_schema(VERDICT_SCHEMA)
    bad = []
    for cid, doc in verdicts.items():
        if cid not in task_ids:
            bad.append(f"{cid}: no such case")
            continue
        errs = [f"{cid}: {p}" for p in jsonschema.validate(doc, schema)]
        if not errs and (doc["role"] != "reviewer" or doc["task_id"] != task_ids[cid]):
            errs.append(f"{cid}: must be a reviewer verdict for task {task_ids[cid]}")
        bad += errs
    if bad:
        raise ValueError("; ".join(bad))
    return verdicts


def _write_result(out_dir, result):
    """Write the dated JSON under `out_dir`; return its path."""
    out_dir = Path(out_dir)
    suffix = "-dry-run" if result["dry_run"] else ""
    path = out_dir / f"reviewer-calibration-{result['date']}{suffix}.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_write.atomic_write(out_dir, path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cases", default=str(CASES), help="the cases directory")
    ap.add_argument("--out", default=str(events.state_root() / "reviewer-calibration"))
    ap.add_argument("--verdicts", help="score recorded verdicts instead of a dry run")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    args = ap.parse_args(argv)
    try:
        date = datetime.date.fromisoformat(args.date).isoformat()
    except ValueError:
        print(f"--date: {args.date!r} is not a YYYY-MM-DD date", file=sys.stderr)
        return 1
    cases, problems = _load_cases(args.cases)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    try:
        verdicts = _load_verdicts(args.verdicts, cases) if args.verdicts else {}
    except (OSError, ValueError) as err:
        print(f"verdicts: {err}", file=sys.stderr)
        return 1
    print(_write_result(args.out, _build_result(cases, verdicts, date)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
