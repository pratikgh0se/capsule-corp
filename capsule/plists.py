"""Fill the placeholders of a launchd plist template (public portable edition, P08).

This is the Python twin of deploy.sh's `render()` (lines 166-180: the awk + sed pipe that fills
`__PREFIX__`, `__HOME__`, `__REPO__`, `__CAPSULE_HOME__`, plus `__VAULT__`, `__WORKER_IMAGE__` and
the repo-mount lines). Two renderers exist only until the flip (spec 2026-10-24, section 4): merge
them then. `capsule/tests/test_plists.py` renders a real template through both and fails on any
difference, so drift shows up there.

Unlike the sed pipe, a placeholder that is not one of the four (or `__BOARD__`, filled only for a
per-board template, with a name that passes `paths.board_name_problem`, or `__PYTHON__`, likewise
only when `values` carries it) is an error here, never
left in the output: a template that still needs the vault or the worker image is not a
stranger-safe template.
Values are filled in one pass, so a value that contains a placeholder is never expanded again.
Each value is XML-escaped (`&`, `<`, `>`): a path with an `&` must still be a valid plist. The sed
twin does not escape, so the parity test uses plain values.
"""
import re
from xml.sax.saxutils import escape

from capsule import paths

PLACEHOLDERS = ("__PREFIX__", "__HOME__", "__REPO__", "__CAPSULE_HOME__")
BOARD = "__BOARD__"      # only a per-board template uses it; `values` then carries a valid name
PYTHON = "__PYTHON__"    # likewise: the interpreter the job runs (a stock python3 is 3.9)
CONFIG = "__CONFIG__"    # likewise: the jobs config dir (CAPSULE_JOBS_DIR), as deploy.sh fills it
_ANY = re.compile(r"__[A-Z][A-Z_]*__")


def render(text, values):
    """`text` with each of the four placeholders replaced by `values[<placeholder>]`.

    `values` maps every name in PLACEHOLDERS to a string, and `__BOARD__` to a valid board name
    when the template uses it. Raises ValueError for a missing value, a bad board name, or any
    other `__NAME__` left in the text (it names the first one)."""
    missing = [p for p in PLACEHOLDERS if not isinstance(values.get(p), str)]
    if missing:
        raise ValueError(f"no value for {missing[0]}")
    board = values.get(BOARD)

    def fill(match):
        word = match.group(0)
        if word == BOARD and isinstance(board, str):
            problem = paths.board_name_problem(board)
            if problem:
                raise ValueError(problem)
            return board
        if word == CONFIG and isinstance(values.get(CONFIG), str):
            return escape(values[CONFIG])
        if word == PYTHON and isinstance(values.get(PYTHON), str):
            return escape(values[PYTHON])
        if word not in PLACEHOLDERS:
            raise ValueError(f"template needs {word}, which the installer does not fill")
        return escape(values[word])

    return _ANY.sub(fill, text)
