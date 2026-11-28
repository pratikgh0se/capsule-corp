#!/usr/bin/env python3
"""Motion judge (F4 a): fail when the screen does not move in a beat that the design calls moving.

Usage: python3 ui_motion.py judge <trace dir> --budget <budget file> [--reference <dir>]

The judge reads motion.json plus one raw RGBA file for each frame, compares numbers,
and prints PASS or FAIL. It writes no file. Standard library only.
Exit codes: 0 pass, 1 fail, 2 bad input.
"""
import hashlib
import json
import math
import os
import sys

SCHEMA = "ui_motion.v1"
EPS = 1e-9


class BadInput(Exception):
    pass


def _json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise BadInput(f"cannot read JSON {path}: {exc}") from None


def _num(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def load_trace(directory):
    """Return the motion record with a bytes object in each frame."""
    meta = _json(os.path.join(directory, "motion.json"))
    if not isinstance(meta, dict) or meta.get("schema") != SCHEMA:
        raise BadInput(f"unknown schema in {directory}/motion.json")
    for key in ("fps", "stage_width"):
        if not _num(meta.get(key)) or meta[key] <= 0:
            raise BadInput(f"bad {key} in {directory}/motion.json")
    for key in ("width", "height"):
        if not isinstance(meta.get(key), int) or meta[key] <= 0:
            raise BadInput(f"bad {key} in {directory}/motion.json")
    frames = meta.get("frames")
    if not isinstance(frames, list):
        raise BadInput(f"no frames list in {directory}/motion.json")
    size = meta["width"] * meta["height"] * 4
    for frame in frames:
        _load_frame(directory, frame, size)
    return meta


def _load_frame(directory, frame, size):
    if not isinstance(frame, dict) or not _num(frame.get("t")):
        raise BadInput("a frame has no numeric t")
    name = frame.get("file")
    if not isinstance(name, str) or name != os.path.basename(name) or not name:
        raise BadInput(f"bad frame file name {name!r}")
    try:
        with open(os.path.join(directory, name), "rb") as fh:
            data = fh.read()
    except OSError:
        raise BadInput(f"frame file missing: {name}") from None
    if len(data) != size:
        raise BadInput(f"frame file {name} has {len(data)} bytes, expected {size}")
    bodies = frame.get("bodies", {})
    if not isinstance(bodies, dict):
        raise BadInput(f"bad bodies in frame {name}")
    frame["data"] = data


def _bodies_list(beat):
    bodies = beat.get("bodies", [])
    if not isinstance(bodies, list) or not all(isinstance(b, str) for b in bodies):
        raise BadInput(f"beat {beat['id']} has a bad bodies list")
    return bodies


def _check_beat(beat):
    if not isinstance(beat, dict) or not isinstance(beat.get("id"), str):
        raise BadInput("a budget beat has no id")
    name = beat["id"]
    if not _num(beat.get("t0")) or not _num(beat.get("t1")):
        raise BadInput(f"beat {name} has no numeric t0 and t1")
    if beat["t0"] >= beat["t1"]:
        raise BadInput(f"beat {name} has t0 not under t1")
    if not isinstance(beat.get("moving"), bool):
        raise BadInput(f"beat {name} has no boolean moving")
    bodies = _bodies_list(beat)
    if not beat["moving"]:
        limit = beat.get("max_changed", 0)
        if not _num(limit) or not 0 <= limit <= 1:
            raise BadInput(f"beat {name} has a bad max_changed")
        return
    for key in ("min_changed", "min_distinct", "min_travel"):
        if not _num(beat.get(key)) or beat[key] <= 0:
            raise BadInput(f"moving beat {name} needs {key} above 0")
    if not bodies:
        raise BadInput(f"moving beat {name} needs a non-empty bodies list")


def _load_budget(path):
    budget = _json(path)
    beats = budget.get("beats") if isinstance(budget, dict) else None
    if not isinstance(beats, list):
        raise BadInput("budget has no beats list")
    if not beats:
        raise BadInput("budget has no beats")
    for beat in beats:
        _check_beat(beat)
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()[:12]
    return beats, digest


def window(trace, beat):
    lo, hi = beat["t0"] - EPS, beat["t1"] + EPS
    return [f for f in trace["frames"] if lo <= f["t"] <= hi]


def changed_share(frames):
    """Mean share of pixels that differ between one frame and the next."""
    if len(frames) < 2:
        return 0.0
    shares = []
    for a, b in zip(frames, frames[1:]):  # noqa: RUF007 (pairwise needs Python 3.10)
        pa = memoryview(a["data"]).cast("I")
        pb = memoryview(b["data"]).cast("I")
        diff = sum(1 for x, y in zip(pa, pb) if x != y)
        shares.append(diff / len(pa))
    return sum(shares) / len(shares)


def _distinct_share(frames):
    if not frames:
        return 0.0
    hashes = {hashlib.sha256(f["data"]).digest() for f in frames}
    return len(hashes) / len(frames)


def _body_travel(frames, body, stage_width):
    points = []
    for frame in frames:
        pos = frame.get("bodies", {}).get(body)
        if not isinstance(pos, list) or len(pos) != 3 or not all(_num(v) for v in pos):
            raise BadInput(f"body {body} has no position in frame {frame['file']}")
        points.append(pos)
    length = sum(math.dist(p, q) for p, q in zip(points, points[1:]))  # noqa: RUF007
    return length / stage_width


def _check_bodies(trace, beats):
    known = set()
    for frame in trace["frames"]:
        known.update(frame.get("bodies", {}))
    for beat in beats:
        for body in beat.get("bodies", []):
            if body not in known:
                raise BadInput(f"budget body {body} is not in the trace")


def measure(trace, beat):
    frames = window(trace, beat)
    travels = [_body_travel(frames, b, trace["stage_width"]) for b in beat.get("bodies", [])]
    return {
        "frames": len(frames),
        "changed": changed_share(frames),
        "distinct": _distinct_share(frames),
        "travel": min(travels) if travels else None,
        "travels": travels,
    }


def _judge_beat(beat, m, ref_changed, fps):
    """Return the list of broken rules for one beat."""
    broken = []
    if m["frames"] < (beat["t1"] - beat["t0"]) * fps - 1 - EPS:
        broken.append("M5 too few frames")
    if not beat["moving"]:
        if m["changed"] > beat.get("max_changed", 0) + EPS:
            broken.append("M4 still beat changes")
        return broken
    broken += _moving_rules(beat, m)
    if ref_changed is not None and m["changed"] < ref_changed / 2 - EPS:
        broken.append("M6 under half of reference change")
    return broken


def _moving_rules(beat, m):
    broken = []
    min_changed = beat["min_changed"]
    min_distinct = beat["min_distinct"]
    min_travel = beat["min_travel"]
    if m["changed"] < min_changed - EPS or m["distinct"] < min_distinct - EPS:
        broken.append("M1 picture does not move")
    travel = m["travel"]
    if travel is not None and travel < min_travel - EPS:
        broken.append("M2 bodies do not travel")
    if travel is not None and _contradicts(m, travel, min_changed, min_travel):
        broken.append("M3 contradiction between picture and bodies")
    return broken


def _contradicts(m, travel, min_changed, min_travel):
    if travel >= min_travel - EPS and m["changed"] == 0:
        return True
    return (m["changed"] >= min_changed - EPS and min_travel > 0
            and all(t == 0 for t in m["travels"]))


def _times_rise(trace):
    times = [f["t"] for f in trace["frames"]]
    return all(a < b for a, b in zip(times, times[1:]))  # noqa: RUF007


def _beat_line(beat, m, broken):
    travel = "n/a" if m["travel"] is None else f"{m['travel']:.4f}"
    verdict = "FAIL " + "; ".join(broken) if broken else "ok"
    return (f"beat {beat['id']} changed={m['changed']:.4f} distinct={m['distinct']:.4f} "
            f"travel={travel} frames={m['frames']} {verdict}")


def _reference_changed(reference, beat):
    if not reference or not beat["moving"]:
        return None
    frames = window(reference, beat)
    if len(frames) < 2:
        raise BadInput(f"reference has under 2 frames for beat {beat['id']}")
    return changed_share(frames)


def run(trace_dir, budget_path, reference_dir):
    trace = load_trace(trace_dir)
    reference = load_trace(reference_dir) if reference_dir else None
    beats, digest = _load_budget(budget_path)
    _check_bodies(trace, beats)
    lines, failed = [], 0
    if not _times_rise(trace):
        lines.append("trace frame times are not rising (M5)")
        failed += 1
    for beat in beats:
        ref = _reference_changed(reference, beat)
        m = measure(trace, beat)
        broken = _judge_beat(beat, m, ref, trace["fps"])
        lines.append(_beat_line(beat, m, broken))
        failed += bool(broken)
    word = "FAIL" if failed else "PASS"
    lines.append(f"UI-PROOF {word} motion budget={digest} beats={len(beats)}")
    return lines, 1 if failed else 0


def main(argv):
    args = list(argv)
    if len(args) < 2 or args[0] != "judge" or "--budget" not in args:
        print("UI-PROOF ERROR usage: ui_motion.py judge <trace dir> "
              "--budget <file> [--reference <dir>]")
        return 2
    try:
        trace_dir = args[1]
        budget = args[args.index("--budget") + 1]
        ref = args[args.index("--reference") + 1] if "--reference" in args else None
        lines, code = run(trace_dir, budget, ref)
    except (BadInput, IndexError) as exc:
        print(f"UI-PROOF ERROR {exc}")
        return 2
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
