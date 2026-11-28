"""Git/baseline plumbing for tests/size_check.py, split out so size_check.py stays
small (Zenkai Z7, row 509 fix round 1). Entries are
`{"path:qualified_name:kind": measured_value}` in tests/size_baseline.json."""
import json
import re
import subprocess
from pathlib import Path

BASELINE_PATH = "tests/size_baseline.json"
MISSING = object()  # the file does not exist yet at this sha - never "the baseline grew"


def lines(argv, repo):
    r = subprocess.run(argv, cwd=repo, capture_output=True, text=True)
    return [p for p in r.stdout.splitlines() if p]


def read_at(repo, sha, path):
    r = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=repo, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def load_at(repo, sha):
    """The dict at `sha`; `MISSING`, or None for corrupt JSON (the caller hard-fails)."""
    text = read_at(repo, sha, BASELINE_PATH)
    if text is None:
        return MISSING
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def write(repo, data):
    (Path(repo) / BASELINE_PATH).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def pair(repo, base, head):
    """(ok, base_bl, head_bl, base_existed, err). Missing at both base and head falls
    back to the local `main` branch's baseline; missing there too is a hard failure."""
    base_raw, head_raw = load_at(repo, base), load_at(repo, head)
    if base_raw is None or head_raw is None:
        return False, None, None, None, f"{BASELINE_PATH} is corrupt JSON - fix or regenerate it"
    base_existed, head_existed = base_raw is not MISSING, head_raw is not MISSING
    if base_existed and not head_existed:
        return False, None, None, None, f"{BASELINE_PATH} was deleted (present at base, missing at head)"
    if not base_existed and not head_existed:
        main_raw = load_at(repo, "main")
        if main_raw is None:
            return False, None, None, None, f"{BASELINE_PATH} is corrupt JSON on main"
        if main_raw is MISSING:
            return False, None, None, None, f"no {BASELINE_PATH} on base, head or main; rebase or regenerate"
        return True, main_raw, main_raw, True, ""
    return True, (base_raw if base_existed else {}), head_raw, base_existed, ""


TEST_DIRS = ("capsule/tests/", "maintenance/tests/", "tests/")


def is_test_file(rel_path):
    rel_path = str(rel_path).replace("\\", "/")
    name = Path(rel_path).name
    return name.startswith("test_") and name.endswith(".py") and rel_path.startswith(TEST_DIRS)


def is_test_side(path):
    """Test files plus every .py under the two test-support trees (rule 2's test side)."""
    return is_test_file(path) or str(path).startswith(("capsule/tests/", "maintenance/tests/"))


def added_line_numbers(repo, base, head, path):
    """HEAD-side line numbers a -U0 diff adds. Rename-aware: a moved file's carried-over lines
    are not added (Ruling 1.1), so the old path joins the pathspec. Only a .py old path on the
    same side (test or production) counts as a move (Ruling 3.3); any other rename is new."""
    names = subprocess.run(["git", "diff", "-M", "--name-status", "--diff-filter=R", base, head],
                           cwd=repo, capture_output=True, text=True, timeout=300).stdout
    old = [f[1] for f in (ln.split("\t") for ln in names.splitlines()) if f[2:] == [path]
           and f[1].endswith(".py") and is_test_side(f[1]) == is_test_side(path)]
    r = subprocess.run(["git", "diff", "-M", "-U0", base, head, "--", path, *old],
                       cwd=repo, capture_output=True, text=True, timeout=300)
    out = set()
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
        if m:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out.update(range(start, start + count))
    return out
