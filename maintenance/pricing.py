#!/usr/bin/env python3
"""pricing.py - USD per million tokens, the one pricing rule. Stdlib only."""
import json, sys
from pathlib import Path

# The prices live in maintenance/prices.json: the one source the Lookout shipper also
# upserts into ops-pg `prices` (spec D6), so the two cannot drift.
PRICES_PATH = Path(__file__).resolve().parent / "prices.json"


def load_prices(path=None):
    doc = json.loads(Path(path or PRICES_PATH).read_text(encoding="utf-8"))
    return {model: dict(v) for model, v in doc["models"].items()}


FALLBACK_MODEL = json.loads(PRICES_PATH.read_text(encoding="utf-8"))["fallback"]


COLS = ("input", "output", "cache_read", "cache_write")


def price_row(row, prices=None):
    """Add `usd_estimate` (and `price_assumed` for an unknown model) to a usage row."""
    try:
        table = prices or load_prices()
        p = table.get(row.get("model") or "") or table[FALLBACK_MODEL]
        cw = p["cache_write"] if p.get("cache_write") is not None else p["in"] * 1.25
        rates = zip(COLS, (p["in"], p["out"], p["cache_read"], cw))
        row["usd_estimate"] = round(sum(int(row.get(k) or 0) * r for k, r in rates) / 1e6, 6)
    except Exception as exc:  # a bad row must not stop the report
        print(f"pricing: {row!r}: {exc}", file=sys.stderr)
        row["usd_estimate"] = 0.0
    else:
        if (row.get("model") or "") not in table:
            row["price_assumed"] = True
    return row
