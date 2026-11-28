"""One model run for one role (spec §6.2, §6.8, §8.4; rows 376, 417, 420)."""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from capsule import (bundles, clone_state, contracts, db, journal, paths, run_record, safe_write,
                     spool, workspace)
from capsule import epic_sync, risk, security_review
from capsule.jsonschema import load_all
from capsule.models import adapter_for
from capsule.runners import host_wall

CHECKPOINT_PROMPT = "Write your checkpoint report now"
CHECKPOINT_TURNS = 2
CHECKPOINT_RESERVE = 300   # inc_65656457: seconds the first call leaves for the checkpoint
CRASH_TAIL_LINES = 200      # row 482: worker.log's transcript tail on a fatal exit


def _fill(text, run, spool_dir, workspace_dir=None):
    """Put the run's envelope values where bundles left placeholders. `workspace_dir`
    (row 465) is the assigned workspace path itself, so the implementer's own pre-edit
    check has something concrete to compare `pwd -P` against, not just a description of the rule."""
    filled = (text.replace("<run_id, given at dispatch>", run["run_id"])
                  .replace("<claim_lock, given at dispatch>", run["claim_lock"])
                  .replace(" (<spool>, given at dispatch)", "")
                  .replace("<spool>", str(spool_dir)))
    if workspace_dir is not None:
        filled = filled.replace("<workspace, given at dispatch>", str(workspace_dir))
    return filled


def _behind_main(ws):
    """Commits on origin/main that the clone's HEAD lacks, as {sha, subject, paths}; never changes
    the clone (inc_f6e24ab9). Gives {total, commits} (commits capped at LANE_BEHIND_CAP, merges
    skipped); [] when nothing is missing or the remote has no main; None on any other error."""
    rng = f"HEAD..refs/remotes/origin/{epic_sync.MAIN}"
    cap, fmt = f"--max-count={bundles.LANE_BEHIND_CAP}", "--format=%x01%H%x09%s"

    def git(*args, timeout=workspace.GIT_FAST_TIMEOUT):
        return workspace._git(*args, ws=ws, timeout=timeout).stdout
    try:
        git("fetch", "--end-of-options", "origin", epic_sync.MAIN,
            timeout=workspace.GIT_SLOW_TIMEOUT)
        total = int(git("rev-list", "--no-merges", "--count", rng))
        out = git("log", "--no-merges", cap, fmt, "--name-only", "--end-of-options", rng)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return [] if "couldn't find remote ref" in str(exc) else None
    if not total:
        return []
    commits = []
    for chunk in out.split("\x01")[1:]:
        head, *paths = chunk.splitlines()
        sha, _, subject = head.partition("\t")
        commits.append({"sha": sha, "subject": subject, "paths": [p for p in paths if p]})
    return {"total": total, "commits": commits}


def _full_files(files, repo):
    """inc_9c7b6734: one line per existing .py path at or over its ceiling (the
    `<path>::file_length` entry of tests/size_baseline.json, else 800). A repo with no readable
    baseline, or a file that cannot be read, gives no line and never blocks a dispatch."""
    try:
        baseline = json.loads((Path(repo) / "tests" / "size_baseline.json").read_text())
    except (OSError, ValueError, TypeError):
        return []
    out = []
    for path in files if isinstance(baseline, dict) else []:
        try:
            count = len((Path(repo) / path).read_text().splitlines()) if path.endswith(".py") else 0
        except (OSError, ValueError):
            continue
        ceiling = baseline.get(f"{path}::file_length", 800)
        if count and isinstance(ceiling, int) and count >= ceiling:
            out.append(f"{path}: {count} lines, ceiling {ceiling}")
    return out


def _bundle(conn, role, task, run, inputs, workspace_dir=None):
    if role == "implementer":
        lane_behind = _behind_main(workspace_dir) if workspace_dir else []
        checkpoint = None
        if run["resumed_from"]:
            prev = conn.execute("SELECT handoff FROM task_runs WHERE run_id=?", (run["resumed_from"],)).fetchone()
            if prev is not None and prev["handoff"]:
                checkpoint = json.loads(prev["handoff"]).get("checkpoint")
        carried = clone_state.carried(workspace_dir) if workspace_dir is not None else None
        return bundles.implementer(task, checkpoint=checkpoint, interventions=inputs.get("interventions"),
                                   clone_state=carried,
                                   card_amended_at=inputs.get("card_amended_at"),
                                   review_findings=inputs.get("review_findings"),
                                   guide_rulings=inputs.get("guide_rulings"), prior_runs=inputs.get("prior_runs"),
                                   retry_error=inputs.get("invalid_document_error"),
                                   restart_notes=inputs.get("restart_notes"),
                                   vault_context=inputs.get("vault_context"),
                                   lane_behind=lane_behind,
                                   size_room=_full_files(
                                       risk.file_set(task.get("body")), workspace_dir))
    if role == "reviewer":
        return bundles.reviewer(task, inputs.get("diff", ""), card_hash=inputs.get("card_hash"),
                                retry_error=inputs.get("invalid_document_error"),
                                host_only=inputs.get("host_only"), workspace_dir=workspace_dir,
                                round=inputs.get("round"), follow_ups=inputs.get("follow_ups"),
                                interventions=inputs.get("interventions"),
                                blind=inputs.get("blind"),
                                vault_context=inputs.get("vault_context"),
                                guide_ruling=inputs.get("guide_ruling"),
                                last_findings=inputs.get("last_findings"))
    if role == "validator":
        return bundles.validator(task, inputs.get("outputs", []), card_hash=inputs.get("card_hash"),
                                 retry_error=inputs.get("invalid_document_error"))
    if role == security_review.ROLE:
        return security_review.bundle(task, inputs, inputs.get("invalid_document_error"))
    if role == "guide" and inputs.get("ruling"):
        return bundles.guide_ruling(task, inputs["ruling"], inputs.get("spec_section", ""), inputs.get("rows", []),
                                    row_texts=inputs.get("row_texts"), spec_sha=inputs.get("spec_sha"),
                                    spec_path=inputs.get("spec_path"),
                                    interventions=inputs.get("interventions"))
    if role == "guide":
        return bundles.guide(task, inputs.get("spec_section", ""), inputs.get("rows", []),
                             first_screen=bool(inputs.get("first_screen")), row_texts=inputs.get("row_texts"),
                             card_hash=inputs.get("card_hash"), spec_sha=inputs.get("spec_sha"),
                             spec_path=inputs.get("spec_path"), impl_report=inputs.get("impl_report"),
                             retry_error=inputs.get("invalid_document_error"),
                             out_of_set=inputs.get("out_of_set"),
                             interventions=inputs.get("interventions"), blind=inputs.get("blind"),
                             vault_context=inputs.get("vault_context"))
    if role == "blind-tester":          # R30: dispatch checked the card; it rides in the inputs
        return bundles.render_blind_tester_brief(inputs["card"], inputs["interface"])
    if role == "korin":                 # row 456: the post-mortem role
        return bundles.korin(task, inputs.get("incident"), inputs.get("evidence"), inputs.get("logs"))
    raise ValueError(f"worker: unknown role {role!r}")


def _write_json(path, doc):
    # Runs on the HOST (the tick spawns `capsule run-one` as a host subprocess) into the run's
    # spool dir. The container writes only requests/ (and the prior/ mirror); defence in depth:
    # no predictable temp, no symlink followed.
    _write_text(path, json.dumps(doc, ensure_ascii=False))


def _start(spool_dir, bundle, job, role, run):
    """What the run began with: its bundle, and its code start state (EVAL-03, never fatal)."""
    _write_json(spool_dir / "bundle.json", bundle)
    run_record.record(spool_dir, job["workspace"], role, job["route"], run, job.get("runner_image"))
    _step(spool_dir, "start")


def _step(spool_dir, name):
    """Journal a completed step; like run_record, never fatal to the run."""
    try:
        journal.append(spool_dir, name)
    except Exception as exc:
        print(f"journal: skipped {name}: {exc}", file=sys.stderr)


def _envelope_result(raw):
    """An error envelope's non-empty `result` text (its message), cut at 300; else the raw text."""
    try:
        env = json.loads(raw)
        error = env.get("is_error") is True
        result = env.get("result") if error else None
        errors = env.get("errors") if error else None
        code = env.get("api_error_code") if error else None
    except (TypeError, ValueError, AttributeError):
        return str(raw or "")
    if not (isinstance(result, str) and result) and isinstance(errors, list) and errors:
        result = "; ".join(str(e) for e in errors)
    if not (isinstance(result, str) and result) and isinstance(code, str):
        result = code
    return result[:300] if isinstance(result, str) and result else str(raw or "")


def _error_line(raw):
    """The last `...Error: ...`/`...Exception...` line of the adapter's raw output, else its last line."""
    lines = [ln.strip() for ln in _envelope_result(raw).splitlines() if ln.strip()]
    hits = [ln for ln in lines if re.search(r"\w+(Error|Exception)\b", ln)]
    return ((hits or lines or [""])[-1])[:300]


def _tool_denials(raw, blind=False):
    """The CLI's own `permission_denials` entries (each
    `tool_name`/`tool_use_id`/`tool_input`), oldest first, plus its own denial/error
    reason text - `result` on a success envelope, `errors` (joined) on the CLI's own
    `error_during_execution` envelope, which never carries `result` (confirmed
    against the installed CLI 2.1.283's result schema). With `blind` (the run carries blind
    tests) the reason is None: it may quote them."""
    try:
        obj = json.loads(raw) if raw else None
    except ValueError:
        obj = None
    if not isinstance(obj, dict):
        return [], None
    denials = [d for d in (obj.get("permission_denials") or []) if isinstance(d, dict)]
    result, errors = obj.get("result"), obj.get("errors")
    if isinstance(result, str):
        reason = result
    elif isinstance(errors, list) and errors:
        reason = "; ".join(str(e) for e in errors)
    else:
        reason = None
    return denials, None if blind else reason


def _write_text(path, text):
    safe_write.atomic_write(path.parent, path, text)


SESSION_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")   # a safe glob segment


def _session_transcript_tail(session_id):
    """The last CRASH_TAIL_LINES lines of the `claude` CLI's own session transcript - a
    real, multi-line transcript, unlike `raw`, which is a single JSON line and gives
    no more than `cli_result.txt` already holds."""
    if not session_id or not SESSION_ID_RE.fullmatch(session_id):
        return None
    # `sorted()` picks one match deterministically if more than one ever exists - never expected,
    # since session ids are unique, but a fixed pick beats depending on the OS's glob order.
    matches = sorted(paths.claude_projects_dir().glob(f"*/{session_id}.jsonl"))
    if not matches:
        return None
    try:
        text = matches[0].read_text(encoding="utf-8")
    except OSError:
        return None
    return "\n".join(text.splitlines()[-CRASH_TAIL_LINES:])


def _persist_crash_output(spool_dir, result, denials, denial_reason):
    """On a fatal exit, keep the adapter's whole stdout (`cli_result.txt`) and stderr
    (`cli_stderr.txt`) intact - never truncated, and never merged, since `raw` drops
    stderr whenever stdout is non-empty."""
    _write_text(spool_dir / "cli_result.txt", result.raw or "")
    _write_text(spool_dir / "cli_stderr.txt", result.stderr or "")
    lines = []
    tail = _session_transcript_tail(result.session_id)
    if tail:
        _write_text(spool_dir / "cli_transcript_tail.jsonl", tail)
        lines.append("transcript tail: cli_transcript_tail.jsonl")
    if denials:
        last = denials[-1]
        lines.append(f"denied tool={last.get('tool_name')} tool_use_id={last.get('tool_use_id')}"
                     f" reason={(denial_reason or '')[:500]}")
    if not lines:
        return
    with safe_write.open_append(spool_dir, spool_dir / "worker.log") as log:
        log.write(("\n".join(lines) + "\n").encode("utf-8"))


def _add_tokens(a, b):
    a, b = a or {}, b or {}
    return {k: a.get(k, 0) + b.get(k, 0) for k in sorted(set(a) | set(b))}


BLIND_ERROR = "the run carries blind tests, so its error text is withheld"
WALL_CLASSES = (host_wall.ERROR_CLASS, host_wall.TIMEOUT_CLASS, *host_wall.HOLD_CLASSES)


def _last_text(raw):
    """The model's last words (the envelope's `result` text, its last 600 characters), or None."""
    try:
        text = json.loads(raw).get("result")
    except (TypeError, ValueError, AttributeError):
        return None
    return text[-600:] if isinstance(text, str) and text else None


def _finish(spool_dir, result, tokens, attempts, tool, denied_tools, denial_reason, role=None):
    error = _error_line(result.raw) if result.error_class else None
    if error and result.error_class not in WALL_CLASSES and host_wall.is_tester_spool(spool_dir):
        error = f"{result.error_class}: {BLIND_ERROR}"      # a fixed phrase, no run text
    _write_json(spool_dir / "run.json", {
        "exit": result.exit, "tokens": tokens, "attempts": attempts,
        "error_class": result.error_class, "retry_after": result.retry_after,
        "session_id": result.session_id,
        "last_text": _last_text(result.raw) if role == "implementer" and not result.error_class
        else None,    # only a clean implementer run: a blind row's text never lands here
        "tool": tool, "denied_tools": denied_tools, "denial_reason": denial_reason,
        # row 456: a failed run's own error line becomes its crash incident's root-cause key
        "error": error})
    _step(spool_dir, "run")


def _model_call(adapter, route, role, bundle, workdir, limits, fresh=None):
    """The model call, plus the checkpoint call after `max_turns` or a wall-clock timeout.
    The checkpoint call gets only the timeout the first call left, so the two calls end before
    the tick's cap (inc_35cbd720). A failed resume (`fresh`: the same job as a new session) runs
    once as `fresh`: the runner names no missing-session error, so any failure of a resume
    counts."""
    if fresh is not None and limits.get("resume"):
        got = _model_call(adapter, route, role, bundle, workdir, limits)
        if not got[0].error_class:
            return got
        bundle, limits = fresh, {k: v for k, v in limits.items() if k != "resume"}
    first = limits
    if role == "implementer":
        first = dict(limits)
        if "timeout" in limits:
            reserve = min(CHECKPOINT_RESERVE, limits["timeout"] // 2)
            first["timeout"] = max(60, limits["timeout"] - reserve)
        if not limits.get("resume"):
            first["new_session"] = True     # the adapter names a fresh session per attempt
    began = time.monotonic()
    result = (adapter or adapter_for(route)).run(role, bundle, workdir, first)
    tokens, attempts = _add_tokens(result.tokens, None), result.attempts
    if (role == "implementer" and result.session_id
            and (result.error_class == "max_turns" or result.exit == 124)):
        again = adapter or adapter_for(dict(route, turn_budget=CHECKPOINT_TURNS))
        second = again.run(role, dict(bundle, prompt=CHECKPOINT_PROMPT), workdir,
                           dict(limits, resume=result.session_id,
                                timeout=max(60, limits["timeout"] - (time.monotonic() - began))))
        tokens, attempts = _add_tokens(tokens, second.tokens), attempts + second.attempts
        if result.exit == 124 and second.error_class:      # keep the cap's own error line
            return result, tokens, attempts
        result = second._replace(session_id=second.session_id or result.session_id)
    return result, tokens, attempts


def _resume_bundle(bundle, inputs):
    """A resumed session never saw the human answers: they follow the continue text."""
    answers = bundles._human_section(inputs.get("interventions"))
    return dict(bundle, prompt="\n\n".join(filter(None, [inputs["continue_prompt"], answers])))


def _stub_ids(report, adapter, route, run):
    """A canned stub report cannot know this run's ids: fill any it leaves out."""
    if (adapter is None and (route.get("adapter") or route.get("provider")) == "stub"
            and isinstance(report, dict) and "schema" in report):
        return {"run_id": run["run_id"], "task_id": run["task_id"],
                "claim_lock": run["claim_lock"], **report}
    return report


def run_one(board_db, spool_dir, run_id, role, *, adapter=None):
    spool_dir = Path(spool_dir)
    job = json.loads((spool_dir / "job.json").read_text())
    # Row 465: before an implementer touches
    # anything, its assigned workspace must itself be identifiably this epic's clone
    # (`workspace.why_not_worktree`: under the epic's own work root, its git dir
    # genuinely present there, that git dir's own origin hashing to the epic's
    # repo_url_sha256) - the mechanised twin of the brief's own pre-edit check.
    if role == "implementer":
        reason = workspace.why_not_worktree(job["workspace"], job.get("work_root"), job.get("repo_url_sha256"))
        if reason is not None:
            msg = f"assigned workspace {job['workspace']} is not a worktree of the repo ({reason}): refusing to run"
            _write_json(spool_dir / "run.json", {
                "exit": 1, "tokens": {}, "attempts": 0, "error_class": "worktree_mismatch", "retry_after": None,
                "session_id": None, "error": msg[:500]})
            return 1
    conn = db.connect_ro(board_db)
    try:
        run = conn.execute("SELECT * FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise LookupError(f"worker: no run {run_id!r} on {board_db}")
        task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone())
        bundle = _bundle(conn, role, task, run, job.get("inputs") or {},
                         workspace_dir=job.get("workspace"))
    finally:
        conn.close()
    bundle = {k: _fill(v, run, spool_dir, workspace_dir=job["workspace"]) for k, v in bundle.items()}
    resume = (job.get("inputs") or {}).get("resume_session")      # a quiet stop's resume
    fresh = dict(bundle, prompt=f"{bundle.get('prompt')}\n\n{job['inputs']['continue_prompt']}") \
        if resume else None       # the same text as a fresh run, when the session is missing
    bundle = _resume_bundle(bundle, job["inputs"]) if resume else bundle
    _start(spool_dir, bundle, job, role, run)

    route, workdir = job["route"], job["workspace"]
    # a guide ruling on an intent contradiction answers with decision.v1 (row 454 item 3)
    schema = bundles.RULING_SCHEMA if (job.get("inputs") or {}).get("ruling") else contracts.schema_for(role)
    limits = dict(job.get("limits") or {}, schema=load_all(paths.SCHEMAS)[schema],
                  wall=host_wall.plan(role, spool_dir, job),    # D161
                  **({"resume": resume} if resume else {}))
    _step(spool_dir, "model_start")         # a hang's last journal step says the model call began
    result, tokens, attempts = _model_call(adapter, route, role, bundle, workdir, limits, fresh)
    _step(spool_dir, "model")

    tool, denied_tools, denial_reason = None, [], None
    if result.error_class == "fatal":       # row 482: never lose a crash's own evidence
        denials, denial_reason = _tool_denials(result.raw, host_wall.is_tester_spool(spool_dir))
        denied_tools = [d.get("tool_name") for d in denials]
        # last, not "the cause"
        tool = denied_tools[-1] if denied_tools else None
        if denial_reason:
            denial_reason = denial_reason[:500]
        _persist_crash_output(spool_dir, result, denials, denial_reason)

    if result.report is not None:
        report = _stub_ids(result.report, adapter, route, run)
        spool.write_doc(spool_dir, "report", report)
        _step(spool_dir, "report")
    _finish(spool_dir, result, tokens, attempts, tool, denied_tools, denial_reason, role)
    return 0
