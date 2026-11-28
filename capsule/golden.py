"""Golden-set runner: replay recorded cases through an adapter and grade the report
against the expected dict (spec §9.2 - "graders are a bug source", so the grader
itself is dict-subset equality with a test of its own)."""
import json
from pathlib import Path


def _grade(expect, report):
    if not expect or not isinstance(report, dict):
        return False
    return all(key in report and report[key] == value for key, value in expect.items())


def run(role, cases_dir, adapter, spool_root=None):
    cases_dir = Path(cases_dir)
    passed = 0
    failed = 0
    cases = []
    for case_dir in sorted(p for p in cases_dir.iterdir() if p.is_dir()):
        case_file = case_dir / "case.json"
        if not case_file.exists():
            continue
        case = json.loads(case_file.read_text())
        env = {}
        if spool_root and case.get("run_id"):       # a builder replay rebuilds the recorded start
            env["start_state"] = _start_state(case["run_id"], spool_root)
        result = adapter.run(role, case["bundle"], str(case_dir), env)
        ok = _grade(case.get("expect", {}), result.report)
        cases.append({"case": case_dir.name, "passed": ok})
        if ok:
            passed += 1
        else:
            failed += 1
    return {"passed": passed, "failed": failed, "cases": cases}


def _start_state(run_id, spool_root):
    """The run's recorded start state (run_record.NAME), or None when absent or unreadable."""
    try:
        return json.loads((Path(spool_root) / run_id / "start_state.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
