"""Deterministic spec checks run before the Spec Ready phone message (spec
output-quality section 5). Never blocks; the planner fixes what is listed."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contracts import DECISION_PREFIX  # noqa: E402

OBSERVABLE = ("shows", "returns", "exits", "contains", "renders", "responds", "displays", "prints", "writes",
              "creates", "sends", "rejects", "refuses", "logs", "redirects", "lists", "counts", "fails", "passes",
              "starts", "stops", "appends", "adds", "removes", "deletes", "updates", "saves", "stores", "loads",
              "reads", "parses", "opens", "moves", "marks", "sets", "posts", "replies", "notifies", "records",
              "mirrors", "blocks", "unblocks", "completes", "emits", "raises", "skips", "exists", "matches",
              "equals", "includes", "produces", "generates", "runs", "installs", "serves")
# Row 128, M076. The AI-first rule's preamble is Contextual Retrieval done by hand: it
# is what makes a spec answer "what is this for" to a role that retrieves it cold, six
# weeks later, out of a search.
REQUIRED = ("For future agent", "Goal", "Acceptance criteria", "Test plan")
RISKS = ("low", "med", "high")
_SUFFIXES = ("ing", "ed", "es", "s")
_WORD_RE = re.compile(r"[a-z']+")


def _stem(word):
    """Cheap suffix-stripping stem so a criterion's past-participle / 3rd-person
    form ("created", "matching", "started") still matches an OBSERVABLE entry
    ("creates", "matches", "starts") without a full stemmer."""
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)]
    return word


_OBSERVABLE_STEMS = {_stem(w) for w in OBSERVABLE}


def _is_testable(line):
    words = _WORD_RE.findall(line.lower())
    return any(_stem(w) in _OBSERVABLE_STEMS for w in words)


def _sections(body):
    out = {}; cur = None
    for line in (body or "").splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1); out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


_PLACEHOLDER_LINE_RE = re.compile(r"^\s*(?:[-*]\s*(?:\[[ xX]\]\s*)?)?<[^<>]*>\s*$")


def _is_placeholder(text):
    """A REQUIRED section still carrying only template text (L2)."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return bool(lines) and all(_PLACEHOLDER_LINE_RE.match(l) for l in lines)


_ACCEPTANCE_RE = re.compile(r"^\s*acceptance:\s*(\S.*?)\s*$", re.M)
# A command starts with a word that looks like one: a binary, a path, or an env
# assignment.
_COMMAND_RE = re.compile(r"^[A-Za-z0-9_./-]+(=\S+)?(\s|$)")
# ...and a command carries at least one character prose does not: a digit, a flag dash,
# a path slash, a dotted module or an `=`.
_NOT_PROSE_RE = re.compile(r"[0-9./=_-]")


def acceptance_commands(body):
    """Every `acceptance: <command>` line in the spec body, in order (M022)."""
    return [m.group(1) for m in _ACCEPTANCE_RE.finditer(body or "")]


def _is_command(cmd):
    return bool(_COMMAND_RE.match(cmd) and _NOT_PROSE_RE.search(cmd))


def bet_slug(fm):
    """The spec's `bet:` frontmatter value, stripped - `""` when absent."""
    value = fm.get("bet")
    return "" if value is None else str(value).strip()


def _epic_plan_problems(fm, bb):
    """M119: a `lane: epic` spec needs a plan in the repo that parses into
    tasks. Listed, never raised - this lint's whole contract is that the
    planner fixes what it lists (module docstring)."""
    import epic_lane
    rel = (fm.get("plan", "") or "").strip()
    if not rel:
        return ["lane: epic needs a plan: path"]
    root = bb.REPO_PATHS.get(fm.get("repo"))
    if not root:
        return []          # the unknown-repo problem is already listed above
    root = Path(root).resolve()
    path = (root / rel).resolve()
    if root not in path.parents and path != root:
        return [f"plan path escapes the repo: {rel}"]
    if not path.is_file():
        return [f"plan not found: {rel}"]
    try:
        epic_lane.parse_plan(path.read_text(encoding="utf-8"))
    except epic_lane.PlanError as exc:
        return [str(exc)]
    except OSError as exc:
        return [f"plan unreadable: {exc}"]
    return []


def lint(body, fm):
    probs = []
    import board_bridge as bb
    import board
    if fm.get("repo") not in bb.REPO_PATHS:
        probs.append(f"unknown repo: {fm.get('repo')}")
    if fm.get("risk") not in RISKS:
        probs.append("risk must be low/med/high")
    # Row 139 / M249: `bet: <slug>` is the card's reason.
    if "bet" in fm:
        slug = bet_slug(fm)
        if not board.BET_SLUG_RE.fullmatch(slug):
            probs.append(f'bet: "{slug}" is not a slug')
    lane = (fm.get("lane", "") or "card").strip().lower()
    if lane not in ("card", "epic"):
        probs.append("lane must be one of ['card', 'epic']")
    elif lane == "epic":
        probs += _epic_plan_problems(fm, bb)
    secs = _sections(body)
    for name in REQUIRED:
        if not secs.get(name):
            probs.append(f"missing section: {name}")
        elif _is_placeholder(secs[name]):
            probs.append(f"placeholder section: {name}")
    if fm.get("kind") != "research":
        for line in secs.get("Acceptance criteria", "").splitlines():
            m = re.match(r"^\s*- \[[ xX]\]\s+(.+?)\s*$", line)
            if m and not _is_testable(m.group(1)):
                probs.append(f'not testable: "{m.group(1)}"')
        # M022: when the spec names the command that proves it done, that command must
        # be a command.
        for cmd in acceptance_commands(secs.get("Acceptance criteria", "")):
            if not _is_command(cmd):
                probs.append(f'acceptance: "{cmd}" is not a command')
    tp = secs.get("Test plan", "")
    if tp and "`" not in tp:
        probs.append("Test plan has no command")
    dn = secs.get("Decisions needed", "")
    for block in re.split(rf"\n(?={re.escape(DECISION_PREFIX)})", dn):
        m = re.match(rf"{re.escape(DECISION_PREFIX)}\s*(.+)", block)
        if m and not re.search(r"^Decided \(", block, re.M):
            probs.append(f"unanswered DECISION: {m.group(1).strip()}")
    return probs


def summary(problems):
    if not problems:
        return "ok"
    return f"{len(problems)} problems: " + "; ".join(problems[:2])
