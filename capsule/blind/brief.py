"""The blind tester's brief (blind spec 3.1, 4.4 item 4), moved out of capsule/bundles.py (its
file cap): `bundles.render_blind_tester_brief` calls it. It reads the section helpers of `bundles`,
so `bundles` imports this module only at call time."""
import re
from pathlib import Path

from capsule import contracts
from capsule.blind import snapshot
from capsule.bundles import REPORT_CHANNEL, _rows_section, _schema_fields, _spec_heading


BLIND_HARNESS_NOTE = (
    "## Harness API and example rows\n"
    "Tests import `from harness import run, read, tmp` and touch the product only through them:"
    " `run(cmd, *, stdin=None, env=None, timeout=60)` runs a shell command in `/workspace` and"
    " returns (cmd, exit, stdout, stderr); `read(path)` reads a file under `/workspace` or a"
    " `tmp()` directory;"
    " `tmp()` gives a scratch directory. Import nothing else from the product.\n"
    "Every test needs one row in `cards/<id>/examples.jsonl`:"
    ' `{"id": "<card id>.<test name>", "given": "<input in behaviour words>",'
    ' "expect": "<outcome the acceptance text calls right>",'
    ' "source": "<card id> acceptance <N>"}`.\n'
    "List a test's full id in the `HEADER.json` `red_exempt` list only when its statement describes"
    " behaviour the product already has before the card, with that statement covered by the test;"
    " every other test must fail on the code from before the work.")


def _snapshot_listing(snapshot_dir):
    root = Path(snapshot_dir)
    files = root.rglob("*") if root.is_dir() else []
    names = sorted(p.relative_to(root).as_posix() for p in files if p.is_file())
    return "\n".join(f"- /interface/{n}" for n in names) or "(the snapshot is empty)"


def _blind_rerun(card):
    """The brief sections of a tester rerun: the red check's outcomes per test name (never
    product output) and the diff of the acceptance text since the tests landed."""
    red, parts = card.get("red_check"), []
    if red:
        lines = [f"- {name}: {outcome}" for name, outcome in sorted(red.get("tests", {}).items())]
        lines += [f"- lint: {p}" for p in red.get("problems", [])]
        parts.append(f"## Red check, round {red.get('round')}\n"
                     f"Your tests in `cards/{card.get('id')}/`"
                     " are in `/workspace`. Each must fail an assertion, every run, on the output"
                     " of a command it ran against the code from before the work (`ok`);"
                     " `passes-on-base` tests do not discriminate, `errors-before-first-call` tests"
                     " failed or broke before running any command, `inconclusive` ones gave mixed"
                     " results, broke after a command ran, or ran a command that timed out or was"
                     " not found (exit 124, 126 or 127)."
                     "\n" + "\n".join(lines))
    if card.get("acceptance_diff"):
        parts.append("## Acceptance text changed\nYour tests landed for the earlier text; update"
                     " them for this change only.\n```diff\n" + card["acceptance_diff"] + "\n```")
    return parts


def _blind_envelope(card_id):
    fields = ", ".join(f"`{f}`" for f in _schema_fields("blind_tests.v1"))
    return "\n".join([
        "## Envelope", "schema: blind_tests.v1", f"task_id: {card_id}",
        "run_id: <run_id, given at dispatch>", "claim_lock: <claim_lock, given at dispatch>",
        REPORT_CHANNEL, f"blind_tests.v1 fields: {fields}. No other field. Cover every numbered"
        " acceptance statement in `covered` (with its test ids) or `not_testable` (with the"
        " reason)."])


_PLAIN = re.compile(r"(?:[A-Z]?[a-z0-9]+|[A-Z0-9]+)\Z")   # `run`, `RUN`, `Run`: a bare word


def _leak_lint(text, identifiers):
    """The identifiers found in `text` as whole words (`run` does not hit `prune`; `_` is a word
    character), case-insensitively. A plain word (no underscore, dot or inner capital) in running
    prose cannot tell a leak from ordinary prose, so it counts only when marked as a name:
    anywhere inside a backtick span (`` `run` ``, `` `snapshot.run` ``, `` `run x` ``) or followed
    by `(` (`run()`). Bare prose use of a plain name stays allowed: an accepted residual."""
    spans = re.findall(r"`([^`\n]+)`", text)

    def found(name):
        word = rf"(?<!\w){re.escape(name)}"
        if not _PLAIN.match(name):
            return re.search(word + r"(?!\w)", text, re.IGNORECASE)
        return (re.search(word + r"\(", text, re.IGNORECASE)
                or any(re.search(word + r"(?!\w)", span, re.IGNORECASE) for span in spans))
    return sorted(i for i in set(identifiers) if i and found(i))


def render_blind_tester_brief(card, snapshot_dir, identifiers=()):
    """The blind tester's bundle (blind spec 3.1, 4.4 item 4): `system` is the role contract,
    `prompt` the card's title, numbered acceptance, `card_hash`, cited rows and spec text, what the
    interface snapshot holds, the harness API and the report envelope; never code. Raises
    ValueError for a card without acceptance and `snapshot.BriefLeak` when the title,
    acceptance, spec text or cited rows name one of `identifiers` (the diff's names; a plain
    word such as `run` counts only in backticks or before `(`, never in bare prose)."""
    acceptance = card.get("acceptance") or []
    acceptance = [acceptance] if isinstance(acceptance, str) else list(acceptance)
    if not any(str(s).strip() for s in acceptance):
        raise ValueError(f"card {card.get('id', '')} has no acceptance statements to test")
    numbered = "\n".join(f"{n}. {s}" for n, s in enumerate(acceptance, 1))
    cid = card.get("id", "")
    parts = [f"# Blind tests for card {cid}: {card.get('title', '')}", "## Acceptance\n" + numbered,
             f"## Card hash\ncard_hash: {card.get('card_hash', '')} (copy it into `HEADER.json`)"]
    if card.get("spec_section"):
        heading = _spec_heading(card.get("spec_path"), card.get("spec_sha"))
        parts.append(heading + "\n" + card["spec_section"])
    rows = _rows_section(card["rows"], card.get("row_texts")) if card.get("rows") else ""
    if rows:
        parts.append(rows)
    texts = [card.get("title", ""), numbered, card.get("spec_section") or "", rows]
    leaked = _leak_lint("\n".join(texts), identifiers)
    if leaked:
        raise snapshot.BriefLeak(leaked)
    parts += ["## What you may read\nRead only `/workspace` (the blind test repo; write tests under"
              f" `cards/{cid}/` only) and `/interface` (the interface snapshot, read-only; it is"
              " mounted in the exec container, read it there with `cat` and `ls`)."
              " The product's code is not mounted.",
              "## Interface snapshot\n" + _snapshot_listing(snapshot_dir), BLIND_HARNESS_NOTE,
              *_blind_rerun(card), _blind_envelope(cid)]
    return {"system": contracts.render("blind-tester"), "prompt": "\n\n".join(parts)}
