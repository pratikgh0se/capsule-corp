"""`python3 -m capsule golden run <role> [--route provider/model] [--min-pass 1.0]` (EVAL-04,
row 385): replay a role's golden set through the model the board routes it to, so a model swap
is one routing line plus this run. Exit 0 at or above --min-pass, 1 below it (or when the model
call failed), 2 for an empty or missing set, 3 for an unreadable config or an unrouted role.
The run's route has no `fallback`: a judgement role never falls back to a cheaper model (D44),
so an unavailable model fails the run loudly instead of grading a different model."""
import sys

from capsule import golden_swap, paths
from capsule.models import adapter_for

FIXTURES = paths.REPO / "capsule" / "tests" / "fixtures" / "golden"


def add_parser(sub):
    p = sub.add_parser("golden", help="run a role's golden set against its routed model (row 385)")
    gsub = p.add_subparsers(dest="golden_cmd", required=True)
    run_p = gsub.add_parser("run", help="exit 0 pass, 1 below --min-pass or a model call failed,"
                                        " 2 no cases, 3 config or route unreadable")
    run_p.add_argument("role")
    run_p.add_argument("--route", default=None, metavar="PROVIDER/MODEL",
                       help="try this model for one run instead of the routed one")
    run_p.add_argument("--min-pass", type=float, default=1.0,
                       help="pass rate to exit 0 at (default 1.0)")
    run_p.add_argument("--board", default=None, help="default: the legacy config's board")
    run_p.add_argument("--capsule-home", default=None)
    golden_swap.add_parser(gsub)


class _Replay:
    """Gives the adapter the shape a real run has (a system prompt, the role's report schema) and
    keeps each failed call's error class, which `golden.run` folds into a plain fail."""

    def __init__(self, adapter, schema, system):
        self.adapter, self.schema, self.system, self.errors = adapter, schema, system, []

    def run(self, role, bundle, workdir, env):
        limits = {**env, "schema": self.schema}
        result = self.adapter.run(role, {"system": self.system, **bundle}, workdir, limits)
        if result.error_class:
            self.errors.append(result.error_class)
        return result


def _route(args, parser):
    from capsule import config
    home = paths.home(args.capsule_home)
    route = config.route(config.load(paths.resolve_config(home, args.board)), args.role)
    if args.route is not None:
        provider, _, model = args.route.partition("/")
        if not provider or not model:
            parser.error("--route must be provider/model")
        route = {**route, "provider": provider, "adapter": provider, "model": model}
    return {**route, "fallback": None}


def handle(args, parser):
    if args.golden_cmd != "run":
        return golden_swap.handle(args, parser)
    from capsule import contracts, golden
    from capsule.jsonschema import load_all
    cases_dir = FIXTURES / args.role
    if not cases_dir.is_dir() or not any((d / "case.json").exists() for d in cases_dir.iterdir()):
        print(f"capsule golden: no golden cases under {cases_dir}", file=sys.stderr)
        return 2
    try:
        route = _route(args, parser)
        schema = load_all(paths.SCHEMAS)[contracts.schema_for(args.role)]
        system = contracts.render(args.role)
    except (OSError, ValueError, KeyError) as exc:
        print(f"capsule golden: no route or schema for {args.role!r}: {exc!r}", file=sys.stderr)
        return 3
    replay = _Replay(adapter_for(route), schema, system)
    outcome = golden.run(args.role, cases_dir, replay)
    total = outcome["passed"] + outcome["failed"]
    print(f"golden {args.role} on {route['provider']}/{route['model']}")
    for case in outcome["cases"]:
        print(f"{case['case']}  {'PASS' if case['passed'] else 'FAIL'}")
    rate = outcome["passed"] / total
    print(f"passed {outcome['passed']}/{total} ({rate:.0%}), min {args.min_pass:.0%}")
    if replay.errors:
        print(f"capsule golden: model call failed on {len(replay.errors)} case(s)"
              f" ({', '.join(sorted(set(replay.errors)))}); no fallback", file=sys.stderr)
        return 1
    return 0 if rate >= args.min_pass else 1
