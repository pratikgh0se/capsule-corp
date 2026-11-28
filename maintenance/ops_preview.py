#!/usr/bin/env python3
"""ops_preview.py - the every-other-day preview of branch-sweep and storage-gc (row 643).

Runs maintenance/branch_sweep.py and maintenance/storage_gc.py with NO arguments, so neither can
apply: the schedule previews, and only the CoS applies, by hand, after reading a preview. Each
script's full output goes to <state root>/ops-preview/<script>-<date>.txt; stdout gets one line
per script with counts only (run.sh sends those to the quiet project channel). Exit 0 when both
scripts exit 0 and print their counts, else 1 (a failed line names the file)."""
import datetime
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import events  # noqa: E402

MAINT = Path(__file__).resolve().parent
# (label, script, regex for "<would go> ... <kept>" over the script's own summary lines)
PREVIEWS = (
    ("branch sweep", "branch_sweep.py", r"(\d+) to remove, (\d+) kept"),
    ("storage gc", "storage_gc.py", r"(\d+) chosen \(\d+ bytes\), (\d+) kept"),
)
IMAGE_BYTES = r"images: (\d+) bytes"      # storage_gc's image line; branch_sweep never prints one


def _counts(pattern, text):
    """(would go, kept) summed over every matching summary line, or None when none matched."""
    found = [(int(a), int(b)) for a, b in re.findall(pattern, text)]
    return (sum(a for a, _ in found), sum(b for _, b in found)) if found else None


def _preview(label, script, pattern, outdir, day):
    """(one-line summary, ok). The script's argv is only its path: preview is its default."""
    run = subprocess.run([sys.executable, str(MAINT / script)], capture_output=True, text=True)
    out = outdir / f"{Path(script).stem}-{day}.txt"
    out.write_text(run.stdout + run.stderr)
    counts = _counts(pattern, run.stdout)
    if run.returncode or counts is None:
        return f"{label} preview: FAILED (exit {run.returncode}), see {out}", False
    image = re.search(IMAGE_BYTES, run.stdout)
    extra = f", {image.group(1)} image bytes" if image else ""
    return f"{label} preview: {counts[0]} would go, {counts[1]} kept{extra}", True


def main():
    day = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S%f")    # a rerun never overwrites
    outdir = events.state_root() / "ops-preview"
    outdir.mkdir(parents=True, exist_ok=True)
    results = [_preview(*p, outdir, day) for p in PREVIEWS]
    print("\n".join(line for line, _ in results))
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
