"""Intent examples: the second oracle beside every contract (Z1, Z2).

`contracts/intent/<role>.jsonl` holds one example per line:

    {"id": "<slug>", "given": {"document": <pattern>, "inputs": <pattern>?},
     "expect": <pattern> (+ optional "$outcome"), "source": "row N" | "recorded run <id>" | "owner"}

`given` is the shape of a document (and of the run's job inputs) a human has judged; `expect`
is what that human calls right for it. `check` returns one contradiction per example whose
`given` matches a schema-valid document while its `expect` does not. `$outcome` in `expect`
judges the tick's own routing of the document (`hold`, `review`, `checkpoint`: the value of
`Tick._route`, the one function the tick also branches on), so an example can catch a reliably
wrong script as well as a wrong document. Lint (`lint_all`) holds every field an
example names to its role's schema and rejects a `given` that would match any document.

Patterns: a dict matches a dict key by key (extra keys allowed); a list or scalar matches by
type-strict equality (lint rejects an operator inside a list); the strings `<empty>` (missing, null, "", [] or {}), `<non-empty>` and `<any>`
(present); the one-key dicts `{"<not>": p}`, `{"<each>": p}` (a non-empty list, every item)
and `{"<some>": p}` (at least one item).

The files sit under `contracts/`, so they are protected like the contracts
(`workspace.PROTECTED`), pinned in `contracts/intent/SHA256SUMS`, and never written here.
"""
import datetime
import json
import re
from pathlib import Path

from capsule import contracts, intent_example_schema, paths

PRECEDENCE = "intent rows > spec > contract > script > code"   # Z3
OUTCOMES = ("hold", "review", "checkpoint")
KEYS = {"id", "given", "expect", "source"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# "row N" alone, or "row N, YYYY-MM-DD" for a row cited by an amend timestamp: the same
# trailing-ISO-date form `tick.cited_rows` already strips off a card's own row citation so it
# isn't misread as another cited row. Matched with
# `fullmatch`, never `match`: with `match`, a bare `$` still allows one trailing
# newline through even though the pattern also has `^`.
SOURCE_RE = re.compile(r"^(row \d{1,4}(, (?P<date>\d{4}-\d{2}-\d{2}))?"
                       r"|recorded run [0-9a-z_-]{4,64}|owner|pratik)$")


def _valid_source(source):
    """`SOURCE_RE` plus a real calendar date for the amend-date suffix - the regex
    alone accepts an impossible date like `0000-99-99`."""
    if not isinstance(source, str):
        return False
    m = SOURCE_RE.fullmatch(source)
    if not m:
        return False
    date = m.group("date")
    if date is None:
        return True
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        return False
    return True


MAX_LINE = 2000
WORDS = ("<empty>", "<non-empty>", "<any>")
OPS = ("<not>", "<each>", "<some>")


class _Missing:
    def __repr__(self):
        return "<missing>"


MISSING = _Missing()


def role_path(role, directory=None):
    return Path(directory or paths.INTENT) / f"{contracts.stem(role)}.jsonl"


def load(role, directory=None):
    """The role's examples, in file order; a role without a file has none."""
    path = role_path(role, directory)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _op(pattern):
    if isinstance(pattern, dict) and len(pattern) == 1:
        (key,) = pattern
        if key in OPS:
            return key, pattern[key]
    return None, None


def matches(pattern, value):
    if isinstance(pattern, str) and pattern in WORDS:
        if pattern == "<any>":
            return value is not MISSING
        empty = value is MISSING or value is None or value in ("", [], {})
        return empty if pattern == "<empty>" else not empty
    op, inner = _op(pattern)
    if op == "<not>":
        return not matches(inner, value)
    if op in ("<each>", "<some>"):
        if not isinstance(value, list):
            return False
        hits = [matches(inner, item) for item in value]
        return bool(hits) and all(hits) if op == "<each>" else any(hits)
    if isinstance(pattern, dict):
        return isinstance(value, dict) and all(matches(p, value.get(k, MISSING)) for k, p in pattern.items())
    return value is not MISSING and _same(pattern, value)


def _same(a, b):
    """Equality without Python's bool/int/float coercion (`1` never matches `true` or `1.0`)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return a == b


def _show(value):
    return "<missing>" if value is MISSING else json.dumps(value, ensure_ascii=False)[:200]


def _differs(expect, document, outcome):
    """One `field: expected X, got Y` line per top-level `expect` key the document fails."""
    out = []
    for key, pattern in expect.items():
        if key == "$outcome":
            if outcome is not None and outcome != pattern:
                out.append(f"$outcome: expected {pattern!r}, got {outcome!r}")
            continue
        value = document.get(key, MISSING)
        if not matches(pattern, value):
            want = pattern if isinstance(pattern, str) else json.dumps(pattern, ensure_ascii=False)
            out.append(f"{key}: expected {want}, got {_show(value)}")
    return out


def check(role, document, *, inputs=None, outcome=None, directory=None):
    """[{id, source, differs}] for every example of `role` whose `given` matches `document`
    (and `inputs`, the run's job inputs) while its `expect` fails. `outcome` is the tick's
    routing of the document; None leaves `$outcome` unjudged."""
    found = []
    for ex in load(role, directory):
        given = ex["given"]
        if not matches(given["document"], document):
            continue
        if "inputs" in given and not matches(given["inputs"], inputs if inputs is not None else {}):
            continue
        differs = _differs(ex["expect"], document, outcome)
        if differs:
            found.append({"id": ex["id"], "source": ex["source"], "differs": differs})
    return found


# -- lint (the contracts' mechanism: checkable, gated) ---------------------------

def _is_word(value):
    return isinstance(value, str) and value.startswith("<") and value.endswith(">")


def _lint_pattern(pattern, where, node):
    """Operators known, operators never inside a literal list (a list matches by equality),
    and every field named by the pattern is one the schema `node` declares (a misspelled field
    reads as missing, which `<empty>`/`<not>` then match on every document). `node` None: unchecked."""
    errors = []
    if _is_word(pattern) and pattern not in WORDS:
        errors.append(f"{where}: unknown operator {pattern!r}")
    elif isinstance(pattern, dict):
        op, inner = _op(pattern)
        if op:
            child = node.get("items") if (node is not None and op != "<not>") else node
            errors += _lint_pattern(inner, f"{where}.{op}", child)
        else:
            props = (node or {}).get("properties")
            for key, value in pattern.items():
                if key.startswith("<") or key.startswith("$"):
                    errors.append(f"{where}: unknown operator key {key!r}")
                elif node is not None and props is not None and key not in props:
                    errors.append(f"{where}: unknown field {key!r} (not in the schema)")
                else:
                    errors += _lint_pattern(value, f"{where}.{key}", (props or {}).get(key) if node is not None else None)
    elif isinstance(pattern, list):
        if any(_is_word(v) or isinstance(v, (dict, list)) and _has_op(v) for v in pattern):
            errors.append(f"{where}: an operator inside a list is matched literally; use <each>/<some>")
    return errors


def _has_op(pattern):
    if _is_word(pattern):
        return True
    if isinstance(pattern, dict):
        return any(k.startswith("<") or _has_op(v) for k, v in pattern.items())
    if isinstance(pattern, list):
        return any(_has_op(v) for v in pattern)
    return False


# What each role's job inputs carry (tick._start_reviews / tick._launch), for `given.inputs`.
# `invalid_document_error`: the validator's exact error text plus
# the offending JSON path, present only on a role's ONE retry after an invalid_document (never on
# a fresh run) - every role that can write a spool document can be retried this way.
INPUT_KEYS = {
    "implementer": {"interventions", "review_findings", "guide_rulings", "invalid_document_error"},
    # round: from round 4 on the reviewer passes or escalates (contracts/reviewer.md, Z3)
    "reviewer": {"diff", "card_hash", "round", "interventions", "invalid_document_error",
                 "last_findings"},
    "validator": {"outputs", "card_hash", "invalid_document_error"},
    "guide": {"spec_section", "rows", "uncited", "first_screen", "row_texts", "card_hash", "spec_sha", "spec_path",
             "impl_report", "interventions", "invalid_document_error"},
}


def _schema(schema_name):
    path = paths.SCHEMAS / f"{schema_name}.json"
    return json.loads(path.read_text()) if path.is_file() else None


def lint(path, schema_name, role=None):
    """[] or one message per problem in one role file; `schema_name` is the role contract's schema,
    `role` (default: the file's stem) names the job inputs `given.inputs` may test."""
    path = Path(path)
    schema = _schema(schema_name)
    input_keys = INPUT_KEYS.get(role or path.stem)
    errors, seen = [], set()
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        at = f"{path.name}:{n}"
        if len(line.encode()) > MAX_LINE:
            errors.append(f"{at}: over the {MAX_LINE} byte line cap")
        try:
            ex = json.loads(line)
        except ValueError as exc:
            errors.append(f"{at}: not JSON ({exc})")
            continue
        if not isinstance(ex, dict) or set(ex) != KEYS:
            errors.append(f"{at}: keys must be exactly {sorted(KEYS)}")
            continue
        errors += intent_example_schema.problems(ex, at)   # the typed line, beside the checks below
        if not isinstance(ex["id"], str) or not ID_RE.fullmatch(ex["id"]):
            errors.append(f"{at}: bad id {ex['id']!r}")
        elif ex["id"] in seen:
            errors.append(f"{at}: duplicate id {ex['id']!r}")
        seen.add(ex["id"])
        if not _valid_source(ex["source"]):
            errors.append(f"{at}: source must be 'row N', 'row N, YYYY-MM-DD', 'recorded run <id>',"
                          " or 'owner'")
        given, expect = ex["given"], ex["expect"]
        if not isinstance(given, dict) or not isinstance(given.get("document"), dict) \
                or not set(given) <= {"document", "inputs"}:
            errors.append(f"{at}: given must be {{document: {{...}}, inputs?: {{...}}}}")
        else:
            doc_schema = given["document"].get("schema")
            if doc_schema is not None and doc_schema != schema_name:
                errors.append(f"{at}: given.document.schema {doc_schema!r} is not the contract's {schema_name!r}")
            if not set(given["document"]) - {"schema"}:
                errors.append(f"{at}: given.document names no field besides schema (it would match every document)")
            elif matches({k: v for k, v in given["document"].items() if k != "schema"}, {}):
                errors.append(f"{at}: given.document matches an empty document (vacuous: only <empty>/<not> patterns)")
            errors += _lint_pattern(given["document"], f"{at} given.document", schema)
            if "inputs" in given:
                if not isinstance(given["inputs"], dict):
                    errors.append(f"{at}: given.inputs must be an object")
                else:
                    unknown = sorted(set(given["inputs"]) - input_keys) if input_keys is not None else []
                    if unknown:
                        errors.append(f"{at}: given.inputs names {unknown}, not in the {role or path.stem} job inputs")
                    errors += _lint_pattern(given["inputs"], f"{at} given.inputs", None)
        if not isinstance(expect, dict) or not expect:
            errors.append(f"{at}: expect must be a non-empty object")
        else:
            rest = {k: v for k, v in expect.items() if k != "$outcome"}
            if "$outcome" in expect and expect["$outcome"] not in OUTCOMES:
                errors.append(f"{at}: $outcome must be one of {OUTCOMES}")
            errors += _lint_pattern(rest, f"{at} expect", schema)
    return errors


STANDALONE = (
    "after_landing",
    "ask_gate",
    "auto_amend",
    "blind_gate",
    "blind_harness",
    "blind_mutation",
    "blind_run",
    "blind_runner",
    "blind_tester",
    "blind_wall",
    "board_fence",
    "board_query",
    "board_tick_setup",
    "bootstrap",
    "brainstorm_handoff",
    "branch_sweep",
    "capsule_home",
    "capsule_secret",
    "capsule_state_migrate",
    "capsule_suite",
    "capsule_tick_sh",
    "capsule_unblocked_hook",
    "capsule_unblocked_hook_cwd",
    "card_fields",
    "check_gate",
    "claims_precheck",
    "compose_live_guard",
    "cost_line",
    "dark_test",
    "deploy",
    "deps_ready",
    "deps_sync",
    "diff_ceiling",
    "dispatch_caps",
    "docker_base_image",
    "doctor",
    "epic_sync",
    "eval_check",
    "event_payloads",
    "flake_reruns",
    "git_lock",
    "golden_cli",
    "grafana_prune_folders",
    "heal",
    "hollow_tests",
    "host_config",
    "idempotent_landing",
    "image_build",
    "image_lock",
    "image_render",
    "image_requests",
    "incident_history",
    "install",
    "intake_size_ceiling",
    "intent_source",
    "interrupts",
    "intervention_schema",
    "interventions",
    "invariants",
    "job_lockdown",
    "keeper_watch",
    "land_reliability",
    "lander",
    "lander_replay",
    "lander_revert",
    "lander_sandbox",
    "lander_select",
    "lander_trust",
    "landing_prep",
    "lib_jobs",
    "light_runner",
    "live_snapshot",
    "lookout_spool_schema",
    "mac_runner",
    "meters",
    "mount_audit",
    "mutation_alias",
    "mutation_check",
    "notice_gen",
    "obs_probe",
    "ops_preview",
    "order_book_lint",
    "outbox_row",
    "outcomes",
    "personal_paths_check",
    "plan_cut",
    "preflight",
    "prior_verdicts",
    "protected_paths",
    "public_export",
    "public_lint",
    "publish_ready",
    "quiet_hours",
    "redesign_switches",
    "replay",
    "report",
    "restart_ladder",
    "review_gate",
    "reviewer_calibration",
    "rhythm",
    "risk",
    "run_shell_suites",
    "runbook_hook_snippet",
    "secret_read_guard",
    "select_tests",
    "shown_sets",
    "simplicity_check",
    "size_check",
    "state_root",
    "step_journal",
    "storage_gc",
    "style_lint",
    "tick_guard",
    "token_accountant",
    "touch",
    "ui_check",
    "unblocked",
    "unblocked_dependency",
    "unblocked_item_schema",
    "unblocked_ownership",
    "unblocked_park",
    "unblocked_stop",
    "untrack",
    "usage_guard",
    "usage_meter",
    "vault_context",
    "vault_read_routes",
    "web_stack",
    "weekly_capture",
    "weekly_pause",
    "worktree_only_edits",
)
# Z1: a plain script (not a tick-dispatched role judged against a
# report/decision/... schema with the run envelope) still owes 1-3 intent examples beside it.
# `STANDALONE` names those - `lint_all` checks their file the same way (id/source/pattern
# shape, 1-3 examples via the shipped-file test) without requiring a `contracts/<name>.md`
# page or a schema: there is no tick document for a standalone script's own oracle to judge.


def lint_all(directory=None):
    """Every `<role>.jsonl` in the directory: a `STANDALONE` script's file lints clean on its
    own (no schema, no contract); every other role must have a contract, and its file lints
    clean against that contract's schema."""
    directory = Path(directory or paths.INTENT)
    errors = []
    for path in sorted(directory.glob("*.jsonl")):
        role = path.stem
        if role in STANDALONE:
            if contracts.contract_path(role).is_file():
                errors.append(f"{path.name}: {role!r} is in STANDALONE but also ships"
                               f" contracts/{role}.md - pick one (a role contract would silently"
                               f" never be schema-checked while STANDALONE skips the schema lint)")
                continue
            errors += lint(path, None, role)
            continue
        if not contracts.contract_path(role).is_file():
            errors.append(f"{path.name}: no contract contracts/{role}.md for role {role!r}")
            continue
        errors += lint(path, contracts.schema_for(role), role)
    return errors
