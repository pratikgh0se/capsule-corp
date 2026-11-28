"""`golden swap`, `golden routing-diff` and `golden haiku-lint` (EVAL-04 with COST-07): a model
swap for a role must pass its golden set. `swap` replays the cases on the routed model and on
`--to` (no fallback on either, as `golden run`), `routing-diff` lists the roles whose model
changed between two configs, `haiku-lint` bans the word "think" from a small-model role's
contract. Exits: swap 0 PASS, 1 BLOCK, 2 no cases, 3 unreadable; diff 0, 1 changed, 2
unreadable; lint 0, 1 banned word, 3 unreadable. Shares the replay, route and fixtures of
`golden_cli`."""
import argparse
import json
import re
import sys
from pathlib import Path

from capsule import paths

_BANNED = re.compile(r"\bthink(?:s|ing)?\b", re.I)


def add_parser(gsub):
    swap_p = gsub.add_parser("swap", help="replay the golden set on the routed model and on --to;"
                                          " exit 0 PASS, 1 BLOCK, 2 no cases, 3 unreadable")
    swap_p.add_argument("role")
    swap_p.add_argument("--to", required=True, metavar="PROVIDER/MODEL")
    swap_p.add_argument("--runs", type=int, default=1, help="replays per model (default 1)")
    swap_p.add_argument("--board", default=None)
    swap_p.add_argument("--capsule-home", default=None)
    diff_p = gsub.add_parser("routing-diff", help="roles whose routing model changed between two"
                                                  " configs; exit 1 if any, 2 unreadable")
    diff_p.add_argument("old")
    diff_p.add_argument("new")
    lint_p = gsub.add_parser("haiku-lint", help="a small-model role's contract has no 'think'")
    lint_p.add_argument("--board", default=None)
    lint_p.add_argument("--capsule-home", default=None)


def handle(args, parser):
    if args.golden_cmd == "swap":
        return _swap(args, parser)
    return _routing_diff(args) if args.golden_cmd == "routing-diff" else _lint(args)


def _arm(runs, role, adapter, cases_dir, shape):
    """`runs` golden replays on one adapter: (the outcomes, the model-call error classes). A call
    that raises is a failed call too (its exception class), not a traceback."""
    from capsule import golden, golden_cli
    outcomes, errors = [], []
    for _ in range(max(1, runs)):
        replay = golden_cli._Replay(adapter, *shape)
        try:
            outcomes.append(golden.run(role, cases_dir, replay))
        except Exception as exc:
            errors.append(type(exc).__name__)
        errors += replay.errors
    return outcomes, errors


def _setup(args, parser):
    """Both routes, both adapters and the replay shape; raises on anything unreadable."""
    from capsule import contracts, golden_cli
    from capsule.jsonschema import load_all
    routes = [golden_cli._route(argparse.Namespace(**{**vars(args), "route": r}), parser)
              for r in (None, args.to)]
    adapters = [golden_cli.adapter_for(route) for route in routes]
    shape = (load_all(paths.SCHEMAS)[contracts.schema_for(args.role)], contracts.render(args.role))
    return routes, adapters, shape


def _pct(arm):
    return arm["mean"] * 100 if isinstance(arm, dict) else 0


def _swap(args, parser):
    from capsule import golden_cli, harness_gate
    cases_dir = golden_cli.FIXTURES / args.role
    if not cases_dir.is_dir() or not any((d / "case.json").exists() for d in cases_dir.iterdir()):
        print(f"capsule golden: no golden cases under {cases_dir}", file=sys.stderr)
        return 2
    if not all(args.to.partition("/")[::2]):
        print(f"capsule golden: --to must be provider/model, got {args.to!r}", file=sys.stderr)
        return 3
    try:
        (base_route, _), (base_ad, cand_ad), shape = _setup(args, parser)
    except (OSError, ValueError, KeyError) as exc:
        print(f"capsule golden: cannot swap {args.role!r}: {exc!r}", file=sys.stderr)
        return 3
    base, base_errors = _arm(args.runs, args.role, base_ad, cases_dir, shape)
    cand, cand_errors = _arm(args.runs, args.role, cand_ad, cases_dir, shape)
    gate = harness_gate.compare(base, cand)
    for name, rates in gate["per_case"].items():
        print(f"{name}  base={rates['baseline']:.2f} cand={rates['candidate']:.2f}")
    base_p, cand_p = _pct(gate["baseline"]), _pct(gate["candidate"])
    ok = cand_p >= base_p and not (base_errors or cand_errors)
    print(f"swap {args.role} {base_route['provider']}/{base_route['model']} -> {args.to}: "
          f"{'PASS' if ok else 'BLOCK'} base={base_p:.0f}% cand={cand_p:.0f}%")
    for side, errors in (("baseline", base_errors), ("candidate", cand_errors)):
        if errors:
            print(f"capsule golden: {side} model call failed on {len(errors)} case(s)"
                  f" ({', '.join(sorted(set(errors)))}); no fallback", file=sys.stderr)
    return 0 if ok else 1


def _model(entry):
    return f"{entry['provider']}/{entry['model']}"


def _routing_diff(args):
    try:
        old, new = (json.loads(Path(p).read_text())["routing"] for p in (args.old, args.new))
        lines = [f"{r} {_model(old[r])} -> {_model(new[r])}" for r in old
                 if r in new and _model(old[r]) != _model(new[r])]
        lines += [f"+{r}" for r in new if r not in old] + [f"-{r}" for r in old if r not in new]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"capsule golden: routing-diff unreadable: {exc!r}", file=sys.stderr)
        return 2
    if lines:
        print("\n".join(lines))
    return 1 if lines else 0


def _lint(args):
    from capsule import config, contracts
    try:
        home = paths.home(args.capsule_home)
        routing = config.load(paths.resolve_config(home, args.board))["routing"]
        roles = [r for r, e in routing.items() if "haiku" in e["model"].lower()]
        found = {r: _BANNED.search(contracts.render(r)) for r in roles}
    except (OSError, ValueError, KeyError) as exc:
        print(f"capsule golden: haiku-lint unreadable: {exc!r}", file=sys.stderr)
        return 3
    for role, hit in found.items():
        print(f"banned {role}: {hit.group(0)}" if hit else f"ok {role}")
    if not found:
        print("no haiku roles")
    return 1 if any(found.values()) else 0
