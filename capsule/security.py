"""`python3 -m capsule security findings|check` (M173, SEC-10): every nightly scan finding gets
a recorded verdict in `docs/security-verdicts.jsonl`; an accepted risk names a decision.

`findings` normalises gitleaks and semgrep JSON into one list of findings (never a secret
value). `check` reads that list and the ledger, prints `untriaged <id>` per finding without a
verdict and a totals line, and writes nothing. The decision pattern is `threat_model.DECISION`.
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

from capsule.threat_model import DECISION

LEDGER = Path("docs") / "security-verdicts.jsonl"
VERDICTS = ("fix", "accept", "ignore")


class _BadFile(Exception):
    pass


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):      # ValueError covers bad JSON and bad UTF-8
        raise _BadFile(f"{path}: not readable JSON") from None


def _finding(tool, rule, path, line, summary):
    return {"id": f"{tool}:{rule}:{path}:{line}", "tool": tool, "rule": rule, "path": path,
            "line": line, "summary": summary}


def _normalise(gitleaks, semgrep):
    out = []
    if gitleaks:
        for r in _load(gitleaks) or []:
            out.append(_finding("gitleaks", r.get("RuleID"), r.get("File"), r.get("StartLine"),
                                r.get("Description")))
    if semgrep:
        for r in (_load(semgrep) or {}).get("results", []):
            out.append(_finding("semgrep", r.get("check_id"), r.get("path"),
                                (r.get("start") or {}).get("line"),
                                (r.get("extra") or {}).get("message")))
    return out


def _problem(row):
    """The reason a decoded ledger row is not a valid verdict, or None."""
    if not isinstance(row, dict):
        return "not a JSON object"
    if row.get("verdict") not in VERDICTS:
        return "unknown verdict"
    if not isinstance(row.get("id"), str) or not row["id"]:
        return "missing id"
    if not isinstance(row.get("reason"), str) or not row["reason"].strip():
        return "empty reason"
    stamp = row.get("date")
    try:
        if not (isinstance(stamp, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp)):
            raise ValueError
        date.fromisoformat(stamp)
    except ValueError:
        return "bad date"
    decision = row.get("decision")
    named = isinstance(decision, str) and DECISION.search(decision)
    if row["verdict"] == "accept" and not named:
        return "accept names no decision (lowercase `row N` or `DN`)"
    return None


def _ledger(repo):
    """({id: verdict}, [problem line]) for the repo's ledger; a missing file is empty."""
    try:
        text = (Path(repo or ".").expanduser() / LEDGER).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}, []
    verdicts, problems = {}, []
    for no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            problems.append(f"problem {no}: not JSON")
            continue
        why = _problem(row)
        if why:
            problems.append(f"problem {no}: {why}")
        else:
            verdicts[row["id"]] = row["verdict"]
    return verdicts, problems


def add_parser(sub):
    p = sub.add_parser("security", help="scan findings and their verdict ledger (M173)")
    ssub = p.add_subparsers(dest="sec_cmd", required=True)
    f = ssub.add_parser("findings", help="print gitleaks and semgrep reports as findings JSON")
    f.add_argument("--gitleaks", default=None, help="a gitleaks JSON report")
    f.add_argument("--semgrep", default=None, help="a semgrep JSON report")
    c = ssub.add_parser("check", help="list findings with no verdict, then the totals")
    c.add_argument("--repo", default=".", help="the repo holding the ledger (default: here)")
    c.add_argument("--findings", required=True, help="the findings JSON from `security findings`")
    c.add_argument("--strict", action="store_true", help="exit 1 on untriaged or ledger problems")


def _findings(path):
    found = _load(path)
    if isinstance(found, list) and all(isinstance(f, dict) and "id" in f for f in found):
        return found
    raise _BadFile(f"{path}: not a JSON list of findings with ids")


def handle(args, parser):
    try:
        if args.sec_cmd == "findings":
            print(json.dumps(_normalise(args.gitleaks, args.semgrep), indent=2))
            return 0
        found = _findings(args.findings)
    except AttributeError:      # a report of the wrong shape
        print("security: a report is not a gitleaks or semgrep JSON report", file=sys.stderr)
        return 2
    except _BadFile as e:
        print(str(e), file=sys.stderr)
        return 2
    verdicts, problems = _ledger(args.repo)
    ids = [f["id"] for f in found]
    open_ids = [i for i in ids if i not in verdicts]
    accepted = sum(1 for i in ids if verdicts.get(i) == "accept")
    print("\n".join([f"untriaged {i}" for i in open_ids] + problems
                    + [f"findings={len(ids)} triaged={len(ids) - len(open_ids)} "
                       f"untriaged={len(open_ids)} accepted={accepted}"]))
    return 1 if args.strict and (open_ids or problems) else 0
