"""The epic lane's vocabulary (C6, decision row 133)."""
import bisect
import os
import re
from dataclasses import dataclass, field

LANES = ("card", "epic")
FLAG = "OPS_EPIC_LANE"
MAX_TASKS = 20          # a plan with more tasks than this is a roadmap, not an epic
MIN_TASKS = 2           # one task is a card; the card lane already handles it
FIX_WAVES = 1           # row 133: one fix wave, then the epic stops quietly

# Row 135's design-and-judgement seat: Fable 5.1 on the whole-branch review, Opus 5 as
# the fallback.
EPIC_REVIEW_MODEL = "claude-fable-5-1"
EPIC_REVIEW_FALLBACK = "claude-opus-5"
REVIEW_MODELS = (EPIC_REVIEW_MODEL, EPIC_REVIEW_FALLBACK)
REVIEW_MODEL_ENV = "OPS_EPIC_REVIEW_MODEL"

EPIC_META_OPEN = "<!-- epic-meta"
# The marker must start a line inside the first 400 characters of the body - the
# board.HELPER_MARKER rule (board.py:147-199): a spec that quotes the block in prose
# must not turn a card into an epic card.
_META_RE = re.compile(r"^<!--\s*epic-meta\s*$(.*?)^-->\s*$", re.M | re.S)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
# `### Task 12 (FORK): the title` -> (12, "the title"). The parenthetical is a
# marker this plan format uses for a repo switch; it is not part of the title.
_TASK_RE = re.compile(r"^###\s+Task\s+(\d+)\s*(?:\([^)]*\))?\s*[:.\-–]\s*(.+?)\s*$", re.M)
_STEP_RE = re.compile(r"^(\s*)- \[ \](\s)", re.M)


class PlanError(ValueError):
    """The plan file is not a plan this lane can dispatch."""


def _fenced_line_mask(text):
    """Per-line fence membership for `text`: True at index i means line i sits inside a
    fenced code block opened by a ```` ``` ```` or `~~~` line and not yet closed."""
    mask = []
    open_fence = False
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            mask.append(open_fence)
            open_fence = not open_fence
        else:
            mask.append(open_fence)
    return mask


def _line_offsets(text):
    offsets = []
    pos = 0
    for line in text.split("\n"):
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def _task_marks(text):
    """`_TASK_RE` matches whose heading line is not inside a fenced code
    block - a heading printed inside a fence in an example is body text, not
    a real task boundary."""
    offsets = _line_offsets(text)
    mask = _fenced_line_mask(text)
    marks = []
    for m in _TASK_RE.finditer(text):
        idx = bisect.bisect_right(offsets, m.start()) - 1
        if idx < 0 or idx >= len(mask) or not mask[idx]:
            marks.append(m)
    return marks


@dataclass(frozen=True)
class Task:
    n: int
    title: str
    body: str
    steps: list = field(default_factory=list)


def _env(env=None):
    return os.environ if env is None else env


def enabled(env=None):
    """True only for an explicit `1` - the budgets.enabled rule (budgets.py:80)."""
    if env is None:
        import verdicts
        env = verdicts.ops_env()
    return env.get(FLAG) == "1"


def check_slug(slug):
    if not _SLUG_RE.fullmatch(str(slug or "")):
        raise ValueError(f"epic_lane: bad slug {slug!r}")
    return str(slug)


def _check_n(n, *, hi=MAX_TASKS):
    n = int(n)
    if not 1 <= n <= hi:
        raise ValueError(f"epic_lane: number {n} outside 1..{hi}")
    return n


def branch(slug):
    return f"epic/{check_slug(slug)}"


def worktree_arg(repo, slug):
    """`worktree:<root>/.worktrees/epic-<slug>` - _worktree_arg's shape
    (board_bridge.py:109-113) with `sprint-` swapped for `epic-`."""
    import board_bridge as bb
    root = bb.REPO_PATHS.get(repo)
    if not root:
        raise SystemExit(f"no REPO_PATHS entry for repo {repo!r}")
    return f"worktree:{root}/.worktrees/epic-{check_slug(slug)}"


def task_key(slug, n):
    return f"sprint:{check_slug(slug)}:task{_check_n(n)}"


def review_key(slug, r):
    return f"sprint:{check_slug(slug)}:review{_check_n(r, hi=FIX_WAVES + 1)}"


def fix_key(slug, r):
    return f"sprint:{check_slug(slug)}:fix{_check_n(r, hi=FIX_WAVES)}"


def epic_meta(body):
    """The `<!-- epic-meta ... -->` block as a dict, or {} when absent."""
    text = str(body or "")
    m = _META_RE.search(text[:2000])
    if not m or not text.lstrip().startswith(EPIC_META_OPEN):
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def is_epic_card(body):
    return epic_meta(body).get("lane") == "epic"


def render_meta(slug, n, total, design, plan, *, area=None, risk=None):
    """`n` is the task number, or 0 for the review/fix cards."""
    extra = ""
    if area:
        extra += f"area: {area}\n"
    if risk:
        extra += f"risk: {risk}\n"
    return (f"{EPIC_META_OPEN}\n"
            f"lane: epic\n"
            f"slug: {check_slug(slug)}\n"
            f"task: {int(n)}\n"
            f"tasks: {int(total)}\n"
            f"{extra}"
            f"design: {design}\n"
            f"plan: {plan}\n"
            "-->\n\n")


def parse_plan(text):
    """The plan's `### Task <n>: <title>` sections, in order."""
    text = str(text or "")
    marks = _task_marks(text)
    if not marks:
        raise PlanError("plan has no `### Task <n>: <title>` headings")
    tasks = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        body_mask = _fenced_line_mask(body)
        steps = [l.strip() for l, fenced in zip(body.splitlines(), body_mask)
                 if not fenced and l.strip().startswith("- [ ]")]
        tasks.append(Task(n=int(m.group(1)), title=m.group(2).strip(), body=body, steps=steps))
    numbers = [t.n for t in tasks]
    if numbers != list(range(1, len(numbers) + 1)):
        raise PlanError(f"plan task numbers are not 1..n: {numbers}")
    if not MIN_TASKS <= len(tasks) <= MAX_TASKS:
        raise PlanError(f"plan has {len(tasks)} tasks, want {MIN_TASKS}..{MAX_TASKS}")
    return tasks


def tick(text, n):
    """Rewrite `- [ ]` to `- [x]` under task `n`'s heading only."""
    text = str(text or "")
    marks = _task_marks(text)
    for i, m in enumerate(marks):
        if int(m.group(1)) != int(n):
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        segment = text[m.end():end]
        mask = _fenced_line_mask(segment)
        lines = segment.split("\n")
        ticked = [line if fenced else _STEP_RE.sub(r"\1- [x]\2", line)
                  for line, fenced in zip(lines, mask)]
        section = "\n".join(ticked)
        return text[:m.end()] + section + text[end:]
    return text


def review_model(env=None):
    """The whole-branch review's model (row 135)."""
    want = str(_env(env).get(REVIEW_MODEL_ENV) or "").strip()
    return want if want in REVIEW_MODELS else EPIC_REVIEW_MODEL
