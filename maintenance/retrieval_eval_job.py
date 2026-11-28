#!/usr/bin/env python3
"""Monthly retrieval-quality eval for the notes vault."""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events  # noqa: E402

import vault_conf  # noqa: E402

SKILL_ROOT = vault_conf.skill_root()
VAULT = vault_conf.vault()
STATE_DIR = events.state_root()
CASES_FILE = STATE_DIR / "retrieval_cases.jsonl"
BASELINE_COUNT_FILE = STATE_DIR / "retrieval_cases_note_count.txt"
EVAL_SCRIPT = SKILL_ROOT / "scripts" / "eval" / "retrieval_eval.py" if SKILL_ROOT else None

# Same exclusions report.sh uses for its 50-note milestone check, so this
# script's notion of "how big is the vault" matches the rest of the system.
EXCLUDE_PREFIXES = ("Templates/", "_trash/", "copilot/", "_export/")

# Exit 1 stays "the eval or case generation failed"; 3 means the recall floor
# regressed (maintenance/dark_test.py NEEDS_HUMAN_EXIT reads it as INFO).
FLOOR_REGRESSED_EXIT = 3
# Key the fork's retrieval_eval.py --json uses for per-case rows: {q, gold, rank, top_hit, ...}.
# rank is 1-based; 0 means the case never surfaced.
CASE_ROWS_KEY = "cases"


def failed_cases_block(data):
    """Failed-case list (rank 0 or beyond 5) for the note, or an explicit absent line."""
    rows = data.get(CASE_ROWS_KEY)
    if not rows:
        return "\nfailed cases: not reported by this engine\n"
    failed = [r for r in rows if not 0 < r["rank"] <= 5]
    lines = ["", f"failed cases ({len(failed)}):", ""]
    # The question text stays out: the vault is indexed, and a note that quotes the
    # eval query could outrank its gold note in the next run.
    for r in failed:
        lines.append(f"- wanted: {r['gold'][0]}; rank {r['rank']}")
    return "\n".join(lines) + "\n"


def note_count() -> int:
    n = 0
    for p in VAULT.rglob("*.md"):
        rel = p.relative_to(VAULT).as_posix()
        if rel.startswith(".") or "/." in rel:
            continue
        if any(rel.startswith(pfx) for pfx in EXCLUDE_PREFIXES):
            continue
        n += 1
    return n


def run_uv(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "--directory", str(SKILL_ROOT), "python", str(EVAL_SCRIPT), *args],
        capture_output=True, text=True, cwd=str(SKILL_ROOT),
    )


def floor_line(summary):
    """One sentence naming both numbers."""
    f = summary.get("floor") or {}
    if not f:
        return ""
    verb = "BELOW" if f.get("regressed") else "at or above"
    return (f"recall@5 {f.get('measured', 0) * 100:.1f}% is {verb} the "
            f"{f.get('recall_at_5', 0) * 100:.1f}% floor of {f.get('as_of', '?')}")


def floor_rc(summary):
    """1 when Task 2's historical (2026-09-02/75.9%) floor is breached."""
    return 1 if (summary.get("floor") or {}).get("regressed") else 0


def render_metrics(summary):
    """The §4.3 block appended to Knowledge/Retrieval Eval YYYY-MM.md."""
    r5 = summary.get("recall_at", {}).get("5", 0) * 100
    lines = ["| Metric | Value |", "|---|---|",
             f"| recall@5 | {r5:.1f}% |",
             f"| MRR | {summary.get('mrr', 0):.3f} |"]
    chunk = summary.get("chunk_hit_position")
    lines.append(f"| chunk position | p50 {chunk['p50']}, p90 {chunk['p90']}, "
                 f"max {chunk['max']} ({chunk['cases']} cases) |" if chunk
                 else "| chunk position | not reported by this engine |")
    stale = summary.get("stale_hit_rate")
    lines.append(f"| stale-hit rate | {stale * 100:.1f}% |" if stale is not None
                 else "| stale-hit rate | not probed |")
    lines.append(f"| cases file | `{summary.get('cases_file', '?')}` |")
    lines += ["", "### recall@5 by folder", "", "| Folder | Cases | recall@5 |", "|---|---|---|"]
    for folder, s in (summary.get("recall_at_5_by_folder") or {}).items():
        flag = " **under 50%**" if s["cases"] >= 3 and s["recall_at_5"] < 0.5 else ""
        lines.append(f"| {folder} | {s['cases']} | {s['recall_at_5'] * 100:.1f}%{flag} |")
    fl = floor_line(summary)
    if fl:
        # Row 132: the fixed 2026-09-02/75.9% floor is historical reference only in the
        # note - it never fails the job (see floor_rc's docstring and
        # check_stateful_floor).
        as_of = (summary.get("floor") or {}).get("as_of", "2026-09-02")
        lines += ["", f"**Floor (historical reference, {as_of}):** {fl}."]
    return "\n".join(lines) + "\n"


def floor_state_path(cases_label: str) -> Path:
    """Row 132: the real floor is stored per case-set VERSION, named from the logical
    cases filename Task 2 versions as `retrieval_cases-<N>notes.jsonl` (the
    `cases_file` summary key), not the physical `retrieval_cases.jsonl` every version
    shares on disk at CASES_FILE."""
    return STATE_DIR / f"retrieval_floor-{Path(cases_label).stem}.json"


def _write_json(path: Path, data) -> None:
    """Atomic write, the same shape as log_rotate._atomic_write and
    decision_log.write_sidecar: a kill between truncate and flush must never
    leave a half-written floor file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def check_stateful_floor(summary, today=None):
    """Row 132: the floor that actually gates this job."""
    today = today or datetime.date.today()
    measured = summary.get("recall_at", {}).get("5")
    if measured is None:
        return 0, ""
    cases_label = summary.get("cases_file") or CASES_FILE.name
    path = floor_state_path(cases_label)
    stored = None
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict):
                raise ValueError("floor file is not an object")
        except (OSError, ValueError) as exc:
            # Design §9: every new step degrades to ABSENT, never to a wedge.
            print(f"retrieval-eval: floor file {path.name} unreadable ({exc}) - "
                  f"treating it as absent and re-setting the baseline", file=sys.stderr)
            stored = None
    if stored is None:
        _write_json(path, {
            "recall_at_5": measured, "as_of": today.isoformat(),
            "cases_file": cases_label,
        })
        return 0, (f"baseline set: recall@5 {measured * 100:.1f}% is now the floor "
                    f"for `{cases_label}` (as of {today.isoformat()}).")
    stored_floor = stored.get("recall_at_5", 0)
    stored_as_of = stored.get("as_of", "?")
    if measured < stored_floor:
        # Record the numbers so report.sh's retrieval_lines()
        # can put them in the 08:30 brief instead of a bare failed-job name.
        stored["last_regression"] = {"measured": measured, "floor": stored_floor,
                                     "when": today.isoformat()}
        _write_json(path, stored)
        line = (f"regressed: recall@5 {measured * 100:.1f}% is below the "
                f"{stored_floor * 100:.1f}% floor set {stored_as_of} for `{cases_label}`.")
        return FLOOR_REGRESSED_EXIT, line
    if stored.pop("last_regression", None) is not None:
        # Recovered: the brief must stop repeating a regression that is over.
        _write_json(path, stored)
    return 0, (f"recall@5 {measured * 100:.1f}% is at or above the "
                f"{stored_floor * 100:.1f}% floor set {stored_as_of} for `{cases_label}`.")


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today()
    month_str = today.strftime("%Y-%m")

    if not CASES_FILE.exists():
        print(f"no case set yet - generating one at {CASES_FILE}")
        r = run_uv(["--generate", "30", "--style", "semantic", "--cases", str(CASES_FILE), "--force"])
        print(r.stdout)
        if r.stderr:
            print(r.stderr, file=sys.stderr)
        if r.returncode != 0 or not CASES_FILE.exists():
            print("case generation failed - aborting this run", file=sys.stderr)
            return 1
        BASELINE_COUNT_FILE.write_text(str(note_count()))

    r = run_uv(["--mode", "default", "--cases", str(CASES_FILE), "--json"])
    if r.returncode != 0:
        print("eval run failed:", r.stderr, file=sys.stderr)
        return 1
    data = json.loads(r.stdout)
    summary = data["summary"]

    baseline_n = None
    if BASELINE_COUNT_FILE.exists():
        try:
            baseline_n = int(BASELINE_COUNT_FILE.read_text().strip())
        except ValueError:
            pass
    current_n = note_count()
    regen_note = ""
    if baseline_n and current_n >= baseline_n * 2:
        regen_note = (
            f"\n\n> Vault has grown from {baseline_n} to {current_n} notes since the case set "
            f"was generated (~2x) - consider deleting `{CASES_FILE.name}` "
            f"(in `maintenance/state/`) so the next run regenerates it against current content."
        )

    knowledge_dir = VAULT / "Knowledge"
    prev_notes = sorted(p for p in knowledge_dir.glob("Retrieval Eval *.md") if not p.stem.endswith(month_str))
    trend = "First retrieval eval - no prior trend." if not prev_notes else (
        f"See [[Knowledge/{prev_notes[-1].stem}]] for the previous month's numbers."
    )

    recall_at = summary["recall_at"]
    r5 = recall_at.get("5", 0) * 100
    mrr = summary["mrr"]

    floor_rc_value, floor_status_line = check_stateful_floor(summary, today)

    out_path = knowledge_dir / f"Retrieval Eval {month_str}.md"
    body = f"""---
date: {today.isoformat()}
type: retrieval-eval
tags:
  - retrieval-eval
ai-first: true
cases: {summary['cases']}
recall-at-5: {recall_at.get('5')}
mrr: {mrr}
---

# Retrieval Eval {month_str}

## For future agent

Monthly measurement of how well `obsidian_search` (the hybrid keyword+semantic retrieval every MCP agent uses) finds the right note for a natural-language question. Cases are fixed once ({summary['cases']} semantic-paraphrase questions sampled from this vault, saved outside the vault) so recall numbers are comparable month over month. Mode: `default` - exactly what the MCP serves, not a lab-only mode. Pull this note to check whether retrieval quality has drifted before or after a search-stack change.

## Results

{render_metrics(summary)}
## Floor (row 132)

{floor_status_line}
{failed_cases_block(data)}
## Trend

{trend}{regen_note}
"""
    out_path.write_text(body, encoding="utf-8")
    print(f"wrote {out_path}")
    print(f"recall@5={r5:.1f}% mrr={mrr:.3f} cases={summary['cases']}")
    if floor_status_line:
        print(floor_status_line)
    return floor_rc_value


def run() -> int:
    """The entry point: no notes vault or no skill checkout configured means skip (exit 0)."""
    for missing, why in ((VAULT, vault_conf.NO_VAULT), (SKILL_ROOT, vault_conf.NO_SKILL_ROOT)):
        if missing is None:
            print(f"retrieval_eval_job: skipped - {why}")
            return 0
    return main()


if __name__ == "__main__":
    raise SystemExit(run())
