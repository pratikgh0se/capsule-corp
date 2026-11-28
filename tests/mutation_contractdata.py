"""Card t_3ff01dd1: `contracts/*.md` (a role's rendered contract) and
`contracts/schemas/*.json` (a boundary schema) are read as DATA, never
`import`ed - the same shape `mutation_check.py`'s I1 fix (row 454) already
closed for `contracts/intent/<role>.jsonl`, but never extended to these two.
Before this module, neither dir ever entered the gate's revert set (the
`intent_data_files`/`revert_targets` lines in `mutation_check.py`'s
`_check_range_impl`): a test whose only real dependency is a contract's or a
schema's CONTENT had no way to be required to discriminate on it - not
`.py`, so never in `prod_files`, and `references_prod_file` is import-name
matching, which cannot see a data-file read at all. `is_contract_data_file`/
`sha256sums_for` close that the same way I1 did; `references_data_file`
supersedes `mutation_check`'s old `references_intent_data` (folded in here
rather than left behind, so intent-jsonl and the new contract/schema cases
share one check) - review round 1, C1: the first cut of this replaced the
old, broad "word 'intent' anywhere plus the role name anywhere, quoted"
check with a strictly narrower one (a real call-shaped read only), which
made the gate MORE LENIENT for `contracts/intent/*.jsonl` than main - a
path-built read (`.../ "usage_guard.jsonl"`) or a `for role in ROLES:
intent.load(role)` loop lost their judgment entirely (BAD -> n/a). The old
broad check is now kept as a FLOOR for intent-jsonl, OR'd with the new,
narrower call-shape and filename-literal checks - the gate is only ever
widened relative to main here, never narrowed. `references_data_file`
requires one of: the role/schema name as an argument to a real data-read
call (`intent.<method>("role", ...)`, `contracts.<method>("role", ...)`,
`schemas["name"]`/`schemas.get("name")`), the role/schema's full file name
as a quoted literal anywhere (`"usage_guard.jsonl"`, `"reviewer.md"`,
`"report.v1.json"` - the path-join/f-string shape), a subscript on anything
naming the bare stem (`[...]["decision.v1"]` - `load_all(...)["name"]`,
never requiring the container be named `schemas`), or, for intent-jsonl
only, the pre-existing floor (the word 'intent' anywhere in the file and
the role quoted anywhere, unscoped) - the false positive that forced
`test_the_reviewer_contract_pins_the_notes_and_zero_findings_sentence` out
of `capsule/tests/test_contracts.py` (commit d91a7fa) no longer recurs for
THAT case even with the floor kept: it was contracts/reviewer.md that was
never reverted, not the floor being wrong in isolation - `contracts/*.md`
is now genuinely in the revert set, so that pin test is required to (and
does) discriminate on its own real dependency regardless of what the floor
also flags it for.

Known gap (real, not "never worse" - review round 1, I2): a role or schema
name reached only through a loop variable with no quoted literal for it
ANYWHERE in the file (`for name, schema in schemas.items(): ...`,
`SCHEMAS.glob("*.json")`) is not recognized for `contracts/*.md` and
`contracts/schemas/*.json` - unlike intent-jsonl, these two have no floor
(main never reverted either, so there is no existing behavior to stay at
least as strict as). Such a file reads `no_prod_change` for that data file."""
import re
from pathlib import Path

CONTRACTS_DIR = "contracts/"
SCHEMAS_DIR = "contracts/schemas/"
INTENT_DIR = "contracts/intent/"
# Intake rq_9b17453e: Grafana's alert rules are data no test imports; a rule-shape test reads
# the file by its quoted name, so `references_data_file`'s filename-literal check judges it.
ALERT_DATA_FILES = ("observability/grafana/provisioning/alerting/rules.yaml",)
# W365: any other tracked data file at the repo root or one dir deep (`maintenance/prices.json`,
# a config toml/yaml, the root LICENSE) outside a `tests/` dir and outside `contracts/` (which has
# its own, narrower predicates above) is data no test imports; a data-only change plus a test
# pinned to the new value could never fail the revert (W359, W362). Judged by the same
# quoted-filename-literal reference check.
GENERIC_DATA_SUFFIXES = (".json", ".toml", ".yaml", ".yml")
GENERIC_DATA_ROOT_NAMES = ("LICENSE",)

# `\w+\([^)]*'role'` - the role/name quoted inside SOME call's own parens (never a bare
# tuple/list literal like `ROLES = ("a", "b")`, which has no identifier directly before its
# `(`) - a real data read, not just co-occurring text.
_CALL_WITH_ARG_RE = r"""\w+\([^)]*(['"]){name}\1"""
_INTENT_IMPORTED_RE = r"""\bimport\s+intent\b|\bfrom\s+intent\s+import\b|\bintent\.\w+\("""
_CONTRACT_CALL_RE = r"""contracts\.\w+\([^)]*(['"]){role}\1|contracts/{role}\.md"""
_SCHEMA_CALL_RE = r"""schemas(?:\[|\.get\()(['"]){name}\1|contracts/schemas/{name}\.json"""
# the full file name, quoted, anywhere - the path-join/f-string shape (`... / "role.jsonl"`,
# `paths.SCHEMAS / "report.v1.json"`) that no call-shape pattern above can see.
_FILENAME_LITERAL_RE = r"""(['"]){name}\1"""
# a subscript on ANYTHING naming the bare stem - `load_all(paths.SCHEMAS)["decision.v1"]` -
# never requiring the container be named `schemas` the way `_SCHEMA_CALL_RE` does.
_SUBSCRIPT_ANY_RE = r"""\[(['"]){name}\1\]"""


def _is_generic_data_file(rel_path):
    parts = rel_path.split("/")
    if rel_path in GENERIC_DATA_ROOT_NAMES:
        return True
    return (rel_path.endswith(GENERIC_DATA_SUFFIXES) and len(parts) <= 2 and parts[0] != "contracts"
            and "tests" not in parts[:-1] and not parts[-1].startswith("test_"))


def is_contract_data_file(rel_path):
    """A role's contract (`contracts/<role>.md`, direct child only - never
    `contracts/intent/*`) or a boundary schema (`contracts/schemas/<name>.json`,
    direct child only), or a path in `ALERT_DATA_FILES`."""
    rel_path = str(rel_path).replace("\\", "/")
    if rel_path in ALERT_DATA_FILES:
        return True
    if _is_generic_data_file(rel_path):
        return True
    if rel_path.startswith(SCHEMAS_DIR) and rel_path.endswith(".json"):
        return rel_path == SCHEMAS_DIR + Path(rel_path).name
    if rel_path.startswith(CONTRACTS_DIR) and rel_path.endswith(".md") and not rel_path.startswith(INTENT_DIR):
        return rel_path == CONTRACTS_DIR + Path(rel_path).name
    return False


def sha256sums_for(data_files):
    """`contracts/schemas/SHA256SUMS` and/or `contracts/intent/SHA256SUMS`, reverted
    alongside any changed schema json / direct-child intent jsonl in `data_files` - left
    at HEAD's hash while the file it hashes sits at base, a pin test's own checksum read
    would mismatch for a reason that has nothing to do with what the test is actually
    judging (review round 1, N3: pre-existing on main for intent, fixed here too)."""
    out = []
    if any(f.startswith(SCHEMAS_DIR) and f.endswith(".json") for f in data_files):
        out.append(SCHEMAS_DIR + "SHA256SUMS")
    if any(f.startswith(INTENT_DIR) and f.endswith(".jsonl") for f in data_files):
        out.append(INTENT_DIR + "SHA256SUMS")
    return out


def references_data_file(test_source, data_file):
    """True when `test_source` makes a real (or, for intent-jsonl, at-least-as-lenient-
    as-main) data read of `data_file`. See the module docstring for the exact shapes."""
    test_source = test_source or ""
    data_file = str(data_file).replace("\\", "/")
    stem = re.escape(Path(data_file).stem)
    literal = re.search(_FILENAME_LITERAL_RE.format(name=re.escape(Path(data_file).name)), test_source)
    if data_file.startswith(INTENT_DIR) and data_file.endswith(".jsonl"):
        floor = "intent" in test_source and re.search(rf"""(['"]){stem}\1""", test_source)
        call = re.search(_INTENT_IMPORTED_RE, test_source) and re.search(
            _CALL_WITH_ARG_RE.format(name=stem), test_source)
        return bool(floor or call or literal)
    if data_file.startswith(SCHEMAS_DIR) and data_file.endswith(".json"):
        call = re.search(_SCHEMA_CALL_RE.format(name=stem), test_source)
        sub = re.search(_SUBSCRIPT_ANY_RE.format(name=stem), test_source)
        return bool(call or sub or literal)
    if data_file.startswith(CONTRACTS_DIR) and data_file.endswith(".md"):
        call = re.search(_CONTRACT_CALL_RE.format(role=stem), test_source)
        return bool(call or literal)
    if data_file in ALERT_DATA_FILES or _is_generic_data_file(data_file):
        return bool(literal)
    return False
