"""Advisory before/after comparator over N golden.run results per arm (EVAL-05 S2).
Pure: no I/O. Baseline is main, candidate is the change. The verdict never blocks."""


def _rates(runs):
    seen = {}
    for run in runs:
        for c in run["cases"]:
            seen.setdefault(c["case"], []).append(c["passed"])
    return {name: sum(v) / len(v) for name, v in seen.items()}


def _arm(runs):
    fractions = [r["passed"] / (r["passed"] + r["failed"]) for r in runs if r["cases"]]
    if not fractions:
        return "unmeasured"
    return {"mean": sum(fractions) / len(fractions), "spread": max(fractions) - min(fractions)}


def compare(baseline, candidate):
    base, cand = _rates(baseline), _rates(candidate)
    shared = sorted(set(base) & set(cand))
    per_case = {n: {"baseline": base[n], "candidate": cand[n]} for n in shared}
    worse = [n for n in shared if cand[n] < base[n]]
    better = [n for n in shared if cand[n] > base[n]]
    parts = [f"{w} on {', '.join(n)}" for w, n in (("worse", worse), ("better", better)) if n]
    verdict = "; ".join(parts) or ("no change" if shared else "unmeasured")
    return {"per_case": per_case, "baseline": _arm(baseline), "candidate": _arm(candidate),
            "skipped": sorted(set(base) ^ set(cand)), "verdict": verdict}
