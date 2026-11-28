"""Prompt bundles: system prompt (the role's contract) + the run's card, per role (spec
§4, row 384)."""
import json
import re
import sys
import time

from capsule import contract_audit, contracts, validator_report
from capsule.blind import text as blind_text
# The shared section builders live in capsule.bundle_sections (this file's cap); the names
# outside modules and tests reach as `bundles.<name>` are re-exported here.
from capsule.bundle_sections import (  # noqa: F401
    ANSWER_PRECEDENCE_NOTE, ANSWER_SCOPE_NOTE, CARD_DOCUMENT_MISSING, FULL_SUITE_NOTE,
    REPORT_CHANNEL, ROW_TEXT_MISSING, _answers_for_review, _card_section, _envelope,
    _fields_section, _follow_ups_text, _header, _human_section, _invalid_document_section,
    _request_lines, _rows_section, _schema_fields, _schema_max_length, _spec_heading,
    _vault_context_section, card_hash)
# The guide, Korin and guide-ruling bundles live in capsule.bundles_guide (this file's cap);
# callers and tests reach them as `bundles.<name>`, so they are re-exported here.
from capsule.bundles_guide import (  # noqa: F401
    ASK_OWNER, ASKS, DECISION_ROW_PRECEDENCE, FIRST_SCREEN_NOTE, IMPL_REPORT_CAP,
    IMPL_REPORT_MISSING, LEGACY_ASK, NO_CITATIONS_NOTE, RED_EXEMPT_OPTIONS, RULING_ANSWER_NOTE,
    RULING_OPTIONS, RULING_SCHEMA, RUNAWAY_OPTIONS, VERDICT_NOTE, _claims_parts, _doc_length_note,
    _impl_report_section, _red_exempt_parts, _runaway_parts, accepted, guide, guide_ruling)

MAX_DIFF_BYTES = 400 * 1024  # 400 KiB (U1 card 23: an uncapped review packet)


def _checkpoint_section(checkpoint):
    if not checkpoint:
        return None
    parts = ["## Checkpoint from a previous run"]
    done_so_far = checkpoint.get("done_so_far")
    if done_so_far:
        parts.append(f"Done so far: {done_so_far}")
    next_steps = checkpoint.get("next_steps") or []
    if next_steps:
        parts.append("Next steps:\n" + "\n".join(f"- {s}" for s in next_steps))
    files_touched = checkpoint.get("files_touched") or []
    if files_touched:
        parts.append("Files touched:\n" + "\n".join(f"- {f}" for f in files_touched))
    open_questions = checkpoint.get("open_questions") or []
    if open_questions:
        parts.append("Open questions:\n" + "\n".join(f"- {q}" for q in open_questions))
    return "\n".join(parts)


# Cards migrated from the old agent runtime still say "Commit ..." and "call kanban_request_review";
# under Capsule the harness commits on the host (the container's git is read-only
# row 445) and neither tool exists (pilot prep).
HARNESS_NOTE = ("## Harness notes\n"
                "Capsule commits your work to the epic branch after the reviews pass: do not commit, and do"
                " not list a missing commit as left undone. The card's kanban tools"
                " (kanban_request_review and other kanban_* calls) do not exist here: your report"
                " document is the review request."
                " Put informational lines in the report's `notes` array, never in `left_undone`"
                " or `issues`:"
                " a protected-path stamp needed at landing, a test assertion you changed and"
                " explained, a gate"
                " that runs after the commit. The guide sees every note. Checks this sandbox cannot"
                " run"
                " (network, host paths, docker, `gh` and CI-log steps, a commit-needing gate that"
                " bin/precheck-gates.py, where the repo has it, does not run) go in `host_only`:"
                " it never"
                " blocks and is shown to the reviewer and guide. `left_undone` holds only"
                " work the card requires and you did not do."
                " A test or code change that the card requires and your file set allows is work to"
                " do, not a question: write it before you report, and put a line in"
                " `left_undone` only when you name what stops you."
                " A defect you find in a file of your file set is work to do in this card: fix it,"
                " add a test for it, and record it in `notes`."
                " `follow_ups` takes only files outside your file set: a follow-up that names one"
                " of your own files holds the card for a human."
                " A card criterion already met on your base goes in `notes` with its commit sha,"
                " never in `left_undone`."
                " A criterion you met by a method other than the one the card names goes in"
                " `notes` with the method and the test id, never in `left_undone`: the reviewer"
                " judges it."
                " A card item marked optional that you did not do goes in `notes`,"
                " never in `left_undone`."
                " When bin/precheck-gates.py exists in the repo, run"
                " `python3 bin/precheck-gates.py`"
                " before you report done (the base is your"
                " HEAD, the same base the review gate uses; `--base <rev>` changes it)."
                " It runs the Z8 simplicity check and the mutation gate on your working tree"
                " in a throwaway clone; it does not touch your branch. Fix every finding first."
                " If bin/precheck-gates.py or a final test run cannot run because the container"
                " cannot read a file, put the command and its exact error in `host_only`, never"
                " in `issues`: the lander runs the gates at landing."
                " A `targets-predate-range` marker takes the sha `git merge-base <start_sha>"
                " origin/main`, where start_sha is the card's review base (the HEAD your run"
                " started on, never a HEAD after a rebase): it stays valid when main moves."
                " Use the marker when the mutation gate reads `did not fail (BAD)` for a test"
                " whose subject is already on base. Keep the test. Put one marker at the top of"
                " the file: `# mutation-check: targets-predate-range <sha> -- <reason with"
                " file:line of the existing code> :: Class.test_name`."
                " Name only the ids whose subject is on base."
                " The card's recurrence test must still fail on base."
                " A subject changed in this range needs a test that fails on base, not a marker."
                " One exception: an old test that changes only its script or fixture, where no"
                " assertion changes and a new test in another file fails on base. The old test"
                " stays. Route: add the marker with the old test id, name the new test in the"
                " reason, and at frozen size offset the marker line in the same file."
                " Simplicity rule 2: a new def with one caller in the same file takes a leading"
                " underscore. A name in a card criterion is not a reason to hold, and the rename"
                " goes in `notes`."
                " Rule 2 reads the caller's file: a new def with one caller in another file is a"
                " hard finding, with or without a leading underscore."
                " Route: put that def in the caller's file with a leading underscore and offset"
                " its lines there; a file at the 800-line cap with no baseline key gets no"
                " exemption."
                " The container's git dir is read-only, so `git stash`, `git add`, `git commit`"
                " and `git checkout` fail: do not run them."
                " Rows (b) and (c) of bin/precheck-gates.py prove that a new test fails before"
                " the fix. With no precheck script, write the pre-fix file with"
                " `git show HEAD:<path>` into /tmp and run the test against that copy."
                " A (b) cell of `did not fail (BAD)` with"
                " `<file> kept at head for import of <name>` means a test reads a name"
                " this range adds. One such read restores that module for"
                " every selected test in the file. Route: rewrite the test to read behaviour"
                " through a name that exists on base, keep the product code the card requires, and"
                " record the change in `notes`. Do not hold the card for this finding."
                # inc_9d8b40ab: the CoS gave the size route by hand three times.
                "\n\nWhen tests/size_baseline.json exists in the repo, the size gate applies."
                " The baseline may only shrink. Nobody raises it, and the CoS does not override"
                " the gate. A file at its frozen size must not grow. Offset the added lines in"
                " the same file, or put the new code in a new file. Code in a new file still needs"
                " two production callers, or one caller in a frozen file. A new file has a limit of"
                " 400 lines. A new function has limits of 40 lines and complexity 10. Size"
                " work is in-scope work: do not hold the card to ask for a raise.")

# Row 465: an implementer once edited main's own
# checkout instead of its assigned workspace.
WORKTREE_NOTE = ("## Before you edit\n"
                 "Your assigned worktree is: <workspace, given at dispatch>\n"
                 "Use Edit/Write only on paths under that worktree; never write by absolute path"
                 " anywhere else. Your exec tool runs inside a container at `/workspace` - that is"
                 " this same worktree, mounted at a different path, not a different tree, so there"
                 " is nothing to compare it against. The harness's own preflight is what actually"
                 " refuses a mismatched run, before your edit ever runs.")

# The Mac lane's exec tool runs on the host under seatbelt: no container, no /workspace.
MAC_WORKTREE_NOTE = WORKTREE_NOTE.split(" Your exec tool")[0] + (
    " Your exec tool runs on this Mac, inside the macOS sandbox, with this worktree"
    " (<workspace, given at dispatch>) as its working directory; there is no /workspace."
    " Pass paths relative to the worktree. The board's contract checks (lint, the full test"
    " suite, the web UI suite) run in the Linux container in the validator's contract job, not"
    " on this Mac; do not try to run them here and do not hold the card because they cannot"
    " run here; run the card's own check command and the tests the card names."
    " tests/ui runs only in the Linux container; the validator runs it there. Do not run"
    " tests/ui on this Mac. A tests/ui test that the card or the guide names, and an import"
    " error from tests/ui (such as a missing ui_guard), go in `host_only`, never in `issues`"
    " or `left_undone`; still make the code change the item asks for, as only the run moves."
    " The harness's own preflight is what actually"
    " refuses a mismatched run, before your edit ever runs.")


def _findings_section(findings):
    """The last review round's content fails, verbatim, each naming its role."""
    if not findings:
        return None
    return "\n".join(["## Review findings to fix",
                      "The last review of this card failed on these findings (role: file:line [class] text)."
                      " Fix each; a finding a cited row or ruling already settles, or one the reviewer"
                      " marked as not requesting a change, goes in `notes` with the citation - a finding"
                      " you judge wrong, with no such citation, goes in `issues` with the reason."]
                     + [f"- {f}" for f in findings])


CLONE_STATE_CAP = 20


def _clone_state_section(carried):
    """inc_1abc7a24: the uncommitted paths the clone holds at dispatch, and what to do with each."""
    if not carried:
        return None
    lines = ["## Clone state at dispatch",
             "These changes were already in the clone before this run started; they predate it."]
    for c in carried[:CLONE_STATE_CAP]:
        if c["protected"]:
            lines.append(f"- {c['path']} ({c['state']}, protected): do not edit it and do not hold"
                         " the card for it; the next verdict restores it.")
        else:
            lines.append(f"- {c['path']} ({c['state']}): treat it as earlier work on this card, and"
                         " keep it unless the card or review says otherwise.")
    if len(carried) > CLONE_STATE_CAP:
        lines.append(f"... and {len(carried) - CLONE_STATE_CAP} more")
    return "\n".join(lines)


def _amended_section(stamp):
    """inc_0ff71df2: the card body changed after the last review round; the card text wins."""
    when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(stamp))
    return ("## Card text amended\n"
            f"The card text above was amended at {when}. Where an earlier answer, ruling or"
            " finding in this prompt conflicts with the card text, the card text wins.")


def _prior_runs_section(prior_runs):
    """Row 520: the last runs' own raw evidence, not just this run's summary of them -
    docs/ZENKAI.md Z2, a second oracle beside the checkpoint/findings/rulings text above."""
    if not prior_runs:
        return None
    lines = ["## Prior runs on this card",
             "Read these before deciding what is already done (newest first) - each run's spool dir,"
             " report/done documents and worker log, read-only:"]
    for run in prior_runs:
        lines.append(f"- {run['run_id']} ({run['role']}): " + ", ".join(run["paths"]))
    return "\n".join(lines)


def _restart_section(notes):
    """M155 (D11): the fresh Opus attempt after the Sonnet attempt and its fix rounds failed."""
    return "\n".join(["## Fresh restart: earlier attempts failed review",
                      "The earlier attempt on this card (an attempt and its fix rounds) failed"
                      " review. You are a fresh attempt: do not assume its approach was right."
                      " The branch still holds its diff; you may discard that diff and start again"
                      " from the card's start point (the commit this card began from) if that is"
                      " the better way to a pass. The failure notes follow, oldest first, each with"
                      " its findings and test output (a long note is trimmed to 3000 characters)."]
                     + [f"\n{n}" for n in notes])


LANE_BEHIND_CAP = 20


def _lane_behind_section(commits):
    """`## Lane behind main` (inc_f6e24ab9): the commits main holds that this lane lacks."""
    if commits is None:
        return ("## Lane behind main\nThe lane status is unknown: the host could not compare"
                " this lane with origin/main.")
    total, commits = commits["total"], commits["commits"]
    lines = [f"## Lane behind main\nThis lane lacks {total} commit(s) on origin/main."
             " Compare a suite failure outside your diff with these paths and subjects. A match"
             " goes in `host_only` with the test id and the sha. Do not block on a match and do"
             " not copy main's file into the lane."]
    for c in commits[:LANE_BEHIND_CAP]:
        lines.append(f"- {c['sha'][:12]} {c['subject']} ({', '.join(c['paths'])})")
    if total > LANE_BEHIND_CAP:
        lines.append(f"- ... and {total - LANE_BEHIND_CAP} more")
    return "\n".join(lines)


SIZE_ROUTE_SCOPE_NOTE = (
    "## Size route and the file set (inc_1517d48d)\n"
    "A new file that takes code from a file at its frozen size is in the card's file set,"
    " and so is its new test file. Implementer: name each such file in `notes` and do not hold"
    " for a file-set ruling. Reviewer: do not raise such a file as outside the file set; other"
    " findings on that file stay ordinary. An edit to an existing file outside the set stays a"
    " hold.")


def _size_room_section(lines):
    """inc_9c7b6734: the file-set files at their size ceiling, with the two legal routes."""
    return "\n".join(
        ["## Files with no size room",
         "These file-set files are at their ceiling:"] + [f"- {x}" for x in lines]
        + ["The baseline only shrinks: do not hold the card for a raise. Two routes: the"
           " same-file offset (remove as many lines as you add), or a new module (limit 400"
           " lines). Name such a new module in `notes`: it counts as in scope for this card."])


# 2026-11-25: cards G6 and Q3 each lost a review round: the check output showed no acceptance item.
PROOF_NOTE = (
    "## Make the check show the proof\n"
    "The validator has no tools. It judges each acceptance item only from the recorded"
    " output of the contract commands; `check_cmd` is the check command of this"
    " card. You cannot change `check_cmd`. Run it as written before you report. Make the"
    " tests that it runs print one line per acceptance item, `PASS <item name>`, so the"
    " output names each item also with -q. Output that shows only \"OK\" proves no item."
    " Only the last 8000 characters are kept. If the command does not run the test that"
    " proves an item, or the card has no check_cmd, put one line in `notes` with the item"
    " and the command that shows it.")


def implementer(task, *, checkpoint=None, interventions=None, review_findings=None, guide_rulings=None,
                prior_runs=None, retry_error=None, restart_notes=None, vault_context=None,
                lane_behind=(), size_room=None, clone_state=None, card_amended_at=None):
    parts = [_header(task), task.get("body", ""), HARNESS_NOTE, FULL_SUITE_NOTE,
             MAC_WORKTREE_NOTE if task.get("tier") == "mac" else WORKTREE_NOTE,
             SIZE_ROUTE_SCOPE_NOTE, PROOF_NOTE]
    if lane_behind is None or lane_behind:
        parts.append(_lane_behind_section(lane_behind))
    if size_room:
        parts.append(_size_room_section(size_room))
    fields = _fields_section(task)
    if fields:
        parts.insert(2, fields)
    invalid_doc_section = _invalid_document_section(retry_error)
    if invalid_doc_section:
        parts.append(invalid_doc_section)
    findings_section = _findings_section(review_findings)
    if findings_section:
        parts.append(findings_section)
    if restart_notes:
        parts.append(_restart_section(restart_notes))
    # row 454: the guide ruled a contradicted document wrong; its reason, verbatim
    if guide_rulings:
        parts.append("\n".join(["## Guide rulings",
                                 "The guide ruled an earlier document on this card wrong against an intent example."
                                 " Follow the ruling's reason."] + [f"- {r}" for r in guide_rulings]))
    if card_amended_at:
        parts.append(_amended_section(card_amended_at))
    clone_section = _clone_state_section(clone_state)
    if clone_section:
        parts.append(clone_section)
    prior_runs_section = _prior_runs_section(prior_runs)
    if prior_runs_section:
        parts.append(prior_runs_section)
    checkpoint_section = _checkpoint_section(checkpoint)
    if checkpoint_section:
        parts.append(checkpoint_section)
    human_section = _human_section(interventions)
    if human_section:
        parts.append(human_section)
    parts += _vault_context_section(vault_context)
    parts.append(_envelope("implementer", task))
    return {"system": contracts.render("implementer"), "prompt": "\n\n".join(parts)}


_QUOTED_ESCAPES = {"n": 10, "t": 9, "\\": 92, '"': 34}
_DIFF_PATHS = re.compile(r'^diff --git (?:"a/(?:[^"\\]|\\.)*"|a/.*?) '
                         r'(?:"b/((?:[^"\\]|\\.)*)"|b/(.*))$')


def _unquote_git(quoted):
    r"""A git C-quoted path body (`\303\251`, `\"`, `\n`) back to text."""
    raw, i = bytearray(), 0
    while i < len(quoted):
        c, nxt = quoted[i], quoted[i + 1:i + 2]
        if c == "\\" and nxt in tuple("01234567"):
            raw.append(int(quoted[i + 1:i + 4], 8) & 255)
            i += 4
        elif c == "\\" and nxt:
            raw.append(_QUOTED_ESCAPES.get(nxt, ord(nxt) & 255))
            i += 2
        else:
            raw += c.encode("utf-8")
            i += 1
    return raw.decode("utf-8", "replace")


def _changed_files(diff):
    """File paths touched by a unified `diff --git a/... b/...` text, in order of first appearance.
    A path git C-quotes (control characters, `"`, `\\` and non-ASCII bytes, under core.quotePath)
    is decoded."""
    files = []
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        m = _DIFF_PATHS.match(line)
        if m:
            files.append(_unquote_git(m.group(1)) if m.group(1) is not None else m.group(2))
            continue
        bits = line.split(" ")
        if len(bits) >= 4:
            path = bits[3]
            files.append(path[2:] if path.startswith("b/") else path)
    return files


def _hollow_lines(diff, ws):
    from capsule import hollow_tests, workspace      # lazy: the check is optional context
    return hollow_tests.hollow_test_lines(
        _changed_files(diff), lambda p: workspace.read_at(ws, "HEAD", p))


def _hollow_section(diff, ws):
    """D85: the advisory hollow-test section, or "" when there is no clone, nothing hollow, or
    any error: an advisory check never stops the bundle being built."""
    if not ws:
        return ""
    try:
        lines = _hollow_lines(diff, ws)
    except Exception as exc:
        print(f"bundles: hollow-test check skipped ({exc})", file=sys.stderr)
        return ""
    if not (lines and lines[0].startswith("hollow-tests: advisory")):
        return ""
    return ("## Hollow tests (advisory)\n" + lines[0] + "\n"
            "Context, not a finding: these tests may not be able to fail. Check each"
            " one before you trust it.")


# inc_03ac215a: each review round found another word form, because the card gave no finish line.
MATCHER_SCOPE_NOTE = (
    "## Phrase matchers (D170)\n"
    "When the card asks for a matcher of phrases, the forms the card names are the acceptance list."
    " Fail a named form that the matcher misses. Put an unnamed form in `notes`. A finding with"
    " `safety: true` or class `security` still fails.")


ONE_FIX_NOTE = (
    "## One fix per finding\n"
    "One fix per finding: each finding names exactly one fix. A finding that offers a second"
    " option (\"The other option is\", \"Alternatively,\") is refused as an invalid document.")

# t_75159d13: three rounds each raised one new finding from the same kit file.
WHOLE_SPEC_NOTE = (
    "## Check the whole spec in one pass\n"
    "When you cite a spec, kit or design file, check every requirement that it states in this"
    " round. List every failure now. A requirement that you check in a later round costs a full"
    " round. If you raise a new finding in a later round on code that had the defect before,"
    " raise it and say that you missed it.")


def _guide_ruling_section(reason):
    """inc_c4473fc2: the guide ruled the reviewer's last verdict wrong; this run gives a new one."""
    if not reason:
        return None
    return ("## The guide ruled your last verdict wrong\n"
            "The guide ruled your previous verdict on this diff `document-wrong`. Its reason:\n"
            f"> {reason}\n"
            "Review the diff again and write a new verdict. Do not repeat the ruled verdict.")


def reviewer(task, diff, card_hash=None, retry_error=None, host_only=None, workspace_dir=None,
             round=None, follow_ups=None, interventions=None, blind=None, vault_context=None,
             last_findings=None,
             guide_ruling=None):
    diff = diff or ""
    if len(diff.encode("utf-8")) <= MAX_DIFF_BYTES:
        diff_section = "## Diff\n```diff\n" + diff + "\n```"
    else:
        files = _changed_files(diff)
        listing = "\n".join(f"- {f}" for f in files) if files else "(no files parsed from the diff)"
        diff_section = (
            "## Diff (over the 400 KiB cap, not inlined; U1 card 23)\n"
            f"Changed files:\n{listing}\n"
            "Read the rest with Read."
        )
    parts = [_header(task), _card_section(task, card_hash)]
    fields = _fields_section(task)
    if fields:
        parts.append(fields)
    if round is not None:   # inc_7879cecc: intent.check judges on this number
        text = f"This is review round {round}."
        if round >= 4:
            text += (" The rule of row 540 applies: move every non-safety finding to"
                     " `notes`, or file a decision.v1. Only a finding with `safety: true` or class"
                     " `security` stays in `findings`.")
        parts.append("## Review round\n" + text)
    if last_findings:   # inc_7aca2dfa: a reviewer must not contradict its own earlier fix unseen
        parts.append("## Your findings last round\n" + "\n".join(f"- {x}" for x in last_findings)
                     + "\nRe-check each finding above against the diff.\n"
                     "If the diff applies a fix you ordered and you now reject it, name that round"
                     " in the finding or note and say why the fix fails.")
    invalid_doc_section = _invalid_document_section(retry_error)
    if invalid_doc_section:
        parts.append(invalid_doc_section)
    if ruled := _guide_ruling_section(guide_ruling):
        parts.append(ruled)
    parts += _answers_for_review(interventions) + [MATCHER_SCOPE_NOTE, SIZE_ROUTE_SCOPE_NOTE,
                                                          ONE_FIX_NOTE, WHOLE_SPEC_NOTE]
    if host_only:   # checks the sandbox could not run; context, not a finding
        lines = "\n".join(f"- {x}" for x in host_only)
        parts.append("## Host-only checks (not run in the sandbox)\n" + lines)
    if follow_ups:   # the guide sees the same items in the implementer's report
        parts.append("## Follow-ups the implementer reported\n" + _follow_ups_text(follow_ups))
    hollow = _hollow_section(diff, workspace_dir)    # "" when there is nothing to say
    if audit := contract_audit.section(task.get("body")):
        parts.append(audit)
    parts += _vault_context_section(vault_context)
    parts += ([hollow] if hollow else []) + blind_text.counts_parts(blind)
    parts += [diff_section, _envelope("reviewer", task)]
    return {"system": contracts.render("reviewer"), "prompt": "\n\n".join(parts)}


VALIDATOR_OUTPUTS_NOTE = (
    "The tick ran each contract command below in the card's runner; this is the whole evidence (R1, row 447)."
    " You are code-blind and have no tools to run anything: judge only from these outputs. Write one assertion"
    " per command: `cmd` copied verbatim, `exit` as given, `result` pass or fail (a non-zero exit, or output"
    " showing a failure, is fail), `excerpt` the lines that decide it. Never `not_run`: every command here has"
    " run, so `not_run` stays []. The card's other checks (files existing, a given test passing) are judged"
    " from these outputs, never asserted as commands of your own. Exit 124 with `timed out` is a"
    " timeout, not a test failure: say `timed out` in the excerpt; the result stays fail."
    " A command ending `# part K/N` is one share of a split suite: every part must pass, and a"
    " failed, timed-out or missing part fails the result (name it, e.g. part 3/6)."
    " A skipped test whose skip reason names another task, plan step or row (for example `not live"
    " yet - task 11 turns this row live`) belongs to that other work: it is not this card's"
    " failure. Judge only the card's own acceptance items. If you believe this card should have"
    " made such a row live, say so in a note on a passing assertion, not as a fail."
    " A skipped test is not a failure. Exit 0 with OK (skipped=N) passes, unless the card's"
    " acceptance names a skipped test as its proof. If a command passed (exit 0) but its output"
    " shows no check"
    " for an acceptance item of THIS card, mark it fail and set `gap` to that item; a skip of a"
    " test outside this card is never a gap. When an output is marked CLIPPED, a check"
    " missing from it is not a gap. Without a `gap`, a fail on a passing run is rejected.")


BLIND_REPORT_NOTE = (
    "Entries marked `mode: report only` (cmd `blind:<id>`) are blind acceptance results, shown for"
    " the record while the blind tests are in report mode: they never change your verdict. Write no"
    " assertion for them and never mark them `not_run`.")


def validator(task, outputs, card_hash=None, retry_error=None):
    lines = ["## Contract outputs"]
    if outputs:
        lines.append(VALIDATOR_OUTPUTS_NOTE)
    else:
        lines.append("No contract commands ran for this card: report each acceptance check as `not_run`, saying why.")
    if any(o.get("blind") == "report" for o in outputs or []):
        lines.append(BLIND_REPORT_NOTE)
    if any(o.get("blind") == "gate" for o in outputs or []):
        lines.append(blind_text.GATE_NOTE)
    for output in outputs or []:
        if output.get("blind") == "hold":       # the tick holds the card; nothing to judge
            continue
        lines.append(f"- cmd: {output.get('cmd', '')}")
        lines.append(f"  exit: {output.get('exit', '')}")
        if output.get("blind") == "report":
            lines.append("  mode: report only")
        elif output.get("blind") == "gate":
            lines.append("  mode: gate")
        if output.get("output"):
            label = ("output (CLIPPED: only the last 8000 characters; earlier lines are missing)"
                     if not output.get("blind") and validator_report.clipped(output)
                     else "output (last lines)")
            lines.append(f"  {label}: {output['output']}")
    parts = [_header(task), _card_section(task, card_hash)]
    invalid_doc_section = _invalid_document_section(retry_error)
    if invalid_doc_section:
        parts.append(invalid_doc_section)
    parts += ["\n".join(lines), _envelope("validator", task)]
    return {"system": contracts.render("validator"), "prompt": "\n\n".join(parts)}


KORIN_NOTE = (
    "## What to produce (row 456; Zenkai rows 454, 455)\n"
    "Find the ROOT cause from the evidence, not the symptom: read the logs and the repo (read-only) until you"
    " can name the line or rule that let this happen. Then write one `postmortem.v1` document:\n"
    "- `root_cause`, `lesson`, `mitigation`: plain sentences; the mitigation is a change to code, a contract or"
    " a check, never 'be careful'.\n"
    "- `enforcer`: the file, check or hook that will refuse the failure next time (Zenkai: obligation + named"
    " enforcer). Not yet mechanisable: write 'enforced by review until mechanised: <where>'.\n"
    "- `recurrence_test`: a dotted unittest id (e.g. `capsule.tests.test_tick.Timeouts.test_x`) the fix card"
    " writes first; it fails before the fix and passes after."
    " The incident closes only when it passes on main."
    " Name a new test module when the target module has more than 650 lines.\n"
    "- `fix_card`: `title` and `body` of the card that builds the fix (acceptance criteria, file set). It goes"
    " through the normal build chain; you never write code. A card that asks for a phrase matcher"
    " names a closed list of literal lines, never \"at least\" or \"such as\".\n"
    "  A guard against a code behaviour never bans literal tokens alone; a rename evades it."
    " Name an ast check over a closed list of call shapes, or a test that runs the behaviour.\n"
    "  When the fix depends on a file outside the repo (a vault note, a host file, an API), quote"
    " its shape in the body from evidence. When you cannot read the file, add one line"
    " `Needs fact: <path>: <what the builder needs>`. Each such line reaches the card's hold"
    " reason. A recurrence test reads behaviour only through names that exist on base, and never"
    " reads a name the fix adds.\n"
    "- A new function with fewer than two production callers takes a leading underscore when its"
    " one caller is in the same file. A fix card never orders a new def with one production"
    " caller in another file; place the def in the caller's file, or name two production"
    " callers. Each named caller exists on base or is ordered by the card. A card that changes"
    " behaviour lists each existing test that drives that behaviour, with its route.\n"
    "- Spec check (inc_22b42704): read the spec that governs the changed behaviour, cite its lines"
    " on the fix card, and never ask for what the spec forbids; if the spec must change, ask for"
    " a decision.\n"
    "- `decision_row_text`: a draft decision-log row (`| <n> | title | context | decision |`) that names the"
    " incident id; the owner or Fable records it.\n"
    "- `confidence`: low, medium or high. A repeat incident means the last fix did not hold: look for why.")


def korin(task, incident, evidence, logs):
    """Korin's packet (row 456): the incident record, its evidence paths and the logs' tails, inlined."""
    record = {k: v for k, v in (incident or {}).items() if v is not None}
    parts = [_header(task), task.get("body", ""),
             "## Incident\n```json\n" + json.dumps(record, ensure_ascii=False, indent=1) + "\n```",
             "## Evidence (paths and event ids; read them)\n" + "\n".join(f"- {e}" for e in evidence or [])]
    for log in logs or []:
        parts.append(f"## Log tail: {log.get('path', '')}\n```\n{log.get('tail', '')}\n```")
    parts += [KORIN_NOTE, _envelope("korin", task)]
    return {"system": contracts.render("korin"), "prompt": "\n\n".join(parts)}


def render_blind_tester_brief(card, snapshot_dir, identifiers=()):
    """The blind tester's bundle: `capsule.blind.brief` (it reads this module's helpers)."""
    from capsule.blind import brief
    return brief.render_blind_tester_brief(card, snapshot_dir, identifiers)
