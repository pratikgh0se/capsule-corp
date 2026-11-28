"""Row 447's not-run rule, checked in code: with contract outputs in
the bundle, a code-blind validator document is never allowed to name anything
`not_run` - not in the top-level `not_run` array (the schema field the model actually
writes to), and not as an `assertions[].result`."""

import json
import re
from pathlib import Path

from capsule.db import event, txn

OK_RUN = "recorded run exited 0 with"
CLIPPED = "was clipped to the last"
_RAN = re.compile(r"^Ran (\d+) tests? in [0-9.]+s$")
_OK = re.compile(r"^OK(?: \(skipped=(\d+)\))?$")
_PYTEST_OK = re.compile(r"^=+ (\d+) passed(?:, \d+ (?:skipped|warnings?))* in [0-9.]+s =+$")

BLIND_FINDING = "a blind check failed; its text is withheld from you"
WITHHELD_CMD = "blind:withheld"       # what `withhold` leaves of a cmd that is no row of the job


def exit_failed(row):
    """True unless the row's `exit` is the int 0: a missing, null or non-int exit is not shown to
    have passed."""
    return type(row.get("exit")) is not int or row["exit"] != 0


def is_blind(cmd):
    """True for a `blind:<id>` cmd however the validator spelled it (case, spaces, backticks)."""
    return _norm(cmd).startswith("blind:")


def _norm(cmd):
    """The comparison key of a cited cmd: casefolded, surrounding whitespace, backticks and quotes
    off, inner whitespace runs collapsed to one space."""
    return " ".join(str(cmd).strip(" \t\n`'\"").casefold().split())


def _rows(outputs):
    """(cmds of the job's rows, True when one of them is blind)."""
    cmds = {str(o.get("cmd")) for o in outputs or [] if isinstance(o, dict)}
    return cmds, any(map(is_blind, cmds))


def _plain(cmd, outputs):
    """`cmd` for a message the implementer or Korin may read. A blind row is not named; nor, in a
    job with blind rows, is any text that is no plain row's own cmd (it may quote a blind row)."""
    cmds, blind_job = _rows(outputs)
    return str(cmd) if not is_blind(cmd) and (not blind_job or str(cmd) in cmds) else "a blind row"


def _quoted(excerpt, output):
    """True when every line of `excerpt` appears in the row's own recorded `output`."""
    return all(line.strip() in output for line in str(excerpt or "").splitlines() if line.strip())


def withhold(doc, outputs):
    """`doc` for the implementer's side. In a job whose `outputs` hold blind rows (D161), an
    assertion on a plain row keeps its excerpt only when every line of it is in that row's own
    recorded `output`; a blind row's cmd stays but loses its excerpt, any other cmd becomes
    `WITHHELD_CMD` (the validator's free text may quote a given or expect line). Any other job:
    only a blind-looking cmd is renamed (no row is blind there, so it is a plain fail)."""
    cmds, blind_job = _rows(outputs)
    if not blind_job:
        if not any(is_blind(a.get("cmd")) for a in doc.get("assertions", [])):
            return doc
        return {**doc, "assertions": [
            {**a, "cmd": WITHHELD_CMD, "excerpt": None} if is_blind(a.get("cmd")) else a
            for a in doc["assertions"]]}
    seen = {}
    for o in (o for o in outputs if isinstance(o, dict)):
        cmd = str(o.get("cmd"))
        seen[cmd] = seen.get(cmd, "") + "\n" + str(o.get("output") or "")
    kept = []
    for a in doc.get("assertions", []):
        if str(a.get("cmd")) in cmds and not is_blind(a.get("cmd")):
            ok = _quoted(a.get("excerpt"), seen[str(a["cmd"])])
            kept.append({**a, "excerpt": a.get("excerpt") if ok else None, "gap": None})
        else:
            cmd = a["cmd"] if str(a.get("cmd")) in cmds else WITHHELD_CMD
            kept.append({**a, "cmd": cmd, "excerpt": None, "gap": None})
    return {**doc, "assertions": kept}


def findings(doc, judged_only=False, outputs=None):
    """The validator's open assertions as lines for the implementer's next round. A `blind:` row
    is one fixed phrase, however many fail and whatever else failed beside them (D161): its given,
    expect and observed text never leaves the validator's report. `outputs`: the job's rows, when
    the doc has not been through `withhold` yet."""
    doc = withhold(doc, outputs)
    shown = [a for a in doc["assertions"]
             if a["result"] == "fail" or (a["result"] == "not_run" and not judged_only)]
    return list(dict.fromkeys(
        BLIND_FINDING if is_blind(a["cmd"]) else
        f"validator: `{a['cmd']}` exit {a['exit']} {a['result']}"
        + (f": {a['excerpt']}" if a.get("excerpt") else "")
        + (f" (not proved: {a['gap']})" if a.get("gap") else "") for a in shown))


def hold_reason(head, failures, tail="", cap=2000):
    """A hold reason of at most `cap` chars: `head`, "Also failed this round:" with one line per
    failure (a validator line keeps its excerpt: the cmd shrinks to 80 chars + "..."), then `tail`.
    The `head` shrinks first, so the failing excerpts are not clipped off the end."""
    also = "".join(
        "\n- " + re.sub(r"^(validator: `)([^`]{80})[^`]+(` exit )", r"\1\2...\3", str(f))
        for f in failures)
    also = ("\nAlso failed this round:" + also)[:cap // 2] if failures else ""
    tail = ("\n" + tail) if tail else ""
    return (head[:max(cap - len(also) - len(tail), 0)] + also + tail)[:cap]


def bare_fail_violation(doc):
    """The reason string for a `fail` verdict that names no failing item, or None.
    `tick.Tick._report` retries it as an invalid document."""
    if doc.get("verdict") != "fail":
        return None
    if any(a.get("result") == "fail" for a in doc.get("assertions", [])):
        return None
    return "validator verdict is fail but no assertion has result fail: name the failing item"


def not_run_violation(doc, outputs, repeat=False):
    """The reason string for `tick.Tick._report`, or None. `doc` is the validator's parsed
    `validator_result.v1`; `outputs` is the run's `job.json` `inputs.outputs` (falsy: no contract
    commands ran, so `not_run` may legitimately be non-empty). Also the report-mode blind rule
    (R28): a `fail` verdict whose failing assertions are all report-only `blind:*` rows is invalid,
    because blind results never fail a card until gate mode (R32). In every mode (the blind mode
    may be off, with no outputs at all) a `fail` citing a `blind:` row that did not fail is
    invalid: such a verdict would find nothing and pass the card."""
    unbacked = _blind_citation_violation(doc, outputs or [])
    if unbacked:
        return unbacked
    if not outputs:
        return None
    named = list(doc.get("not_run") or [])
    named += [a["cmd"] for a in doc.get("assertions", []) if a.get("result") == "not_run"]
    if not named:
        return ((None if repeat else _ok_run_violation(doc, outputs))
                or _clipped_gap_violation(doc, outputs)
                or _blind_report_violation(doc, outputs) or _blind_gate_violation(doc, outputs))
    return (f"validator marked {len(named)} item(s) not_run although the contract outputs are"
            f" in its bundle (row 447): {'; '.join(_plain(n, outputs) for n in named)}")[:2000]


def _ok_tests(row):
    """N when the row's recorded run exited 0 and ended with a passing summary of N >= 1 tests that
    did not all skip (unittest `Ran N tests` then `OK` or `OK (skipped=k)`, k < N; or a pytest
    `N passed` line); else 0. Any other summary line gives 0."""
    if exit_failed(row):
        return 0
    lines = [ln.strip() for ln in str(row.get("output") or "").splitlines() if ln.strip()]
    if not lines:
        return 0
    py = _PYTEST_OK.match(lines[-1])
    if py:
        return int(py.group(1))
    ok = _OK.match(lines[-1])
    ran = [m for m in map(_RAN.match, lines) if m]
    n = int(ran[-1].group(1)) if ran else 0
    return n if ok and n >= 1 and int(ok.group(1) or 0) < n else 0


def _gap(a):
    """The acceptance item the passing output does not prove (`gap`), or "": a fail on an exit-0 row
    that names one is a valid coverage finding, not a skip misread."""
    return str(a.get("gap") or "").strip()


def _ok_run_fails(doc, outputs):
    """{cmd, exit, summary} for each `fail` assertion on a plain row whose recorded run exited 0
    with a passing test summary and that names no `gap`."""
    rows = {str(o.get("cmd")): o for o in outputs or [] if isinstance(o, dict)}
    return [{"cmd": str(a.get("cmd")), "exit": rows[str(a.get("cmd"))]["exit"],
             "summary": str(rows[str(a.get("cmd"))]["output"]).strip().splitlines()[-1][:200]}
            for a in doc.get("assertions", [])
            if a.get("result") == "fail" and not is_blind(a.get("cmd"))
            and str(a.get("cmd")) in rows and not _gap(a) and _ok_tests(rows[str(a.get("cmd"))])]


def _ok_run_violation(doc, outputs):
    """The reason when a `fail` assertion cites a plain row whose recorded run exited 0 with a
    passing test summary and names no `gap`. A skip is not a failure: the validator declares the
    `gap` or withdraws the fail. Zero tests, or every test skipped, may still fail (row 459)."""
    for r in _ok_run_fails(doc, outputs)[:1]:
        return (f"validator failed `{_plain(r['cmd'], outputs)}`, but its {OK_RUN} {r['summary']}:"
                " a skip is not a failure; judge the recorded summary, and either set `gap` to the"
                " acceptance item the output does not prove or withdraw the fail")[:2000]
    return None


def clipped(row):
    """True when the row's recorded output is as long as `tick.OUTPUT_TAIL`: it was clipped."""
    from capsule.tick import OUTPUT_TAIL    # tick imports this module: resolve at call time
    return len(str(row.get("output") or "")) >= OUTPUT_TAIL


def _clipped_gap_violation(doc, outputs):
    """The reason when a `fail` assertion with a `gap` cites a plain row that exited 0 and whose
    recorded output is clipped (as long as `tick.OUTPUT_TAIL`): a line missing from a clipped tail
    proves nothing. It holds on the retry too, and its text has no `OK_RUN`, so `is_repeat` is
    false and two refusals reach the "no valid document twice" path."""
    from capsule.tick import OUTPUT_TAIL    # tick imports this module: resolve at call time
    rows = {str(o.get("cmd")): o for o in outputs or [] if isinstance(o, dict)}
    for a in doc.get("assertions", []):
        row = rows.get(str(a.get("cmd")))
        if (a.get("result") == "fail" and not is_blind(a.get("cmd")) and _gap(a) and row
                and not exit_failed(row) and clipped(row)):
            return (f"validator failed `{_plain(a.get('cmd'), outputs)}` with a gap, but its output"
                    f" {CLIPPED} {OUTPUT_TAIL} characters: a line missing from a"
                    " clipped tail is not a gap. Judge the exit code and the summary line, then"
                    " withdraw the fail")[:2000]
    return None


def _inputs(sp):
    """The `inputs` of the run's job.json ({} when unreadable)."""
    try:
        inputs = json.loads((Path(sp) / "job.json").read_text()).get("inputs")
    except (OSError, ValueError, AttributeError):
        return {}
    return inputs if isinstance(inputs, dict) else {}


def is_repeat(sp):
    """True when this run is the retry of a document `_ok_run_violation` rejected: the error is in
    its job inputs (`invalid_document_error`), where the retry's bundle reads it."""
    return OK_RUN in str(_inputs(sp).get("invalid_document_error"))


def reject(conn, run, err, now, outputs=()):
    """`err`, after an `invalid_document` event with it: the retry's bundle reads that event. A
    blind run's event carries the fixed withheld text, never `err` (it can quote tester output)."""
    from capsule.incident_inputs import BLIND_RUN_ERROR     # a top import is a cycle (host_wall)
    if conn is not None:
        reason = err[:500]
        if any(o.get("blind") for o in outputs):
            reason = BLIND_RUN_ERROR + (f" ({OK_RUN} a passing summary)" if OK_RUN in err else "") \
                + (" (a gap on a clipped output is not valid: withdraw the fail)"
                   if CLIPPED in err else "")
        with txn(conn):
            event(conn, run["task_id"], "invalid_document", {"reason": reason},
                  run_pk=run["id"], run_id=run["run_id"], now=now)
    return err


def note_error(conn, run, doc, sp, now):
    """A `validator_error` event for a repeat document that still fails an exit-0 row with no `gap`.
    That fail is not passed and not an infra block: it is an ordinary review fail for the
    implementer. Called once the document has passed every other check."""
    again = conn is not None and is_repeat(sp)
    rows = _ok_run_fails(doc, _inputs(sp).get("outputs")) if again else []
    if rows:
        with txn(conn):
            event(conn, run["task_id"], "validator_error", {"rows": rows},
                  run_pk=run["id"], run_id=run["run_id"], now=now)


def _blind_citation_violation(doc, outputs):
    """The reason when a `fail` assertion cites `blind:<x>` and no output row of that cmd failed
    (it exited 0, or `x` is no row, or the row is a hold the validator never sees). A row with no
    `exit` recorded is not shown to have passed."""
    failed = {_norm(o.get("cmd")) for o in outputs
              if o.get("blind") in ("gate", "report") and exit_failed(o)}
    bad = [a.get("cmd") for a in doc.get("assertions", []) if a.get("result") == "fail"
           and is_blind(a.get("cmd")) and _norm(a.get("cmd")) not in failed]
    if not bad:
        return None
    # report mode: a count, never a test id (this text reaches the implementer's notes)
    named = "" if any(o.get("blind") == "report" for o in outputs) else (
        f" {'; '.join(map(str, bad))[:500]}:")
    return (f"validator failed on {len(bad)} blind row(s) that did not fail:{named}"
            " cite only a `blind:<id>` row that exited non-zero")


def _blind_report_violation(doc, outputs):
    report = {_norm(o.get("cmd")) for o in outputs if o.get("blind") == "report"}
    failed = [_norm(a.get("cmd")) for a in doc.get("assertions", [])
              if a.get("result") == "fail"]
    if doc.get("verdict") != "fail" or not failed or not set(failed) <= report:
        return None
    # a count, never a test id: this text reaches Korin
    return ("validator failed the card only on report-only blind row(s), "
            f"{len(failed)} of them: blind results never fail a card in report mode;"
            " leave them out of your assertions and judge the other outputs")


def _blind_gate_violation(doc, outputs):
    """Gate mode (R32): every blind test that ran has an assertion citing `blind:<id>`, and a
    failing one (exit not 0) is a `fail` assertion under a verdict that is not `pass`."""
    ran = [o for o in outputs if o.get("blind") == "gate"]
    results = {}
    for a in doc.get("assertions", []):
        results.setdefault(_norm(a.get("cmd")), set()).add(a.get("result"))
    left_out = [o["cmd"] for o in ran if _norm(o["cmd"]) not in results]
    passed = [o["cmd"] for o in ran
              if exit_failed(o) and "fail" not in results.get(_norm(o["cmd"]), ())]
    if left_out:
        return (f"validator left out {len(left_out)} blind test(s) that ran: "
                f"{'; '.join(left_out)[:500]}: gate mode needs one assertion citing each")
    if passed or (doc.get("verdict") == "pass" and any(map(exit_failed, ran))):
        return ("validator did not fail failing blind test(s) "
                f"{'; '.join(passed)[:500]}: a failing blind test is a `fail` assertion citing"
                " `blind:<id>`, and the verdict cannot be pass")
    return None
