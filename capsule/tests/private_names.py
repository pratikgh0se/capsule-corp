"""Private-name guards for tests: the rules of the private denylist (release/private-denylist.txt),
which exists only in the maintainer's checkout. Tests take retired, personal and project names
from here, never as literals."""
import importlib.util
import re
import unittest

from capsule import legacy_names, paths

DENYLIST = paths.REPO / "release" / "private-denylist.txt"
SKIP_MSG = "release/private-denylist.txt absent (public checkout): no private names to guard"
retired_only = unittest.skipUnless(DENYLIST.exists(), SKIP_MSG)
# the categories that are not the retired system (same list as tests/denylist_term.py)
KNOWN = ("personal-name", "launchd-prefix", "vault", "project")


def _rules():
    src = paths.REPO / "maintenance" / "public_lint.py"
    spec = importlib.util.spec_from_file_location("public_lint", src)
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    return lint._load_denylist(DENYLIST)[0]


def _words(rule):
    return [w for w in rule.split("|") if w]


def retired_names():
    """The plain words of the retired system's rule (alternatives split on |). Skips the test only
    when the denylist is absent (public checkout); a denylist without exactly one such rule
    fails."""
    if not DENYLIST.is_file():
        raise unittest.SkipTest(SKIP_MSG)
    retired = [rx for cat, rx, _ in _rules() if cat not in KNOWN]
    if len(retired) != 1:
        raise AssertionError(f"denylist has {len(retired)} retired-system rules, expected one")
    return _words(retired[0])


def personal_names():
    """The plain words of the personal-name rule, for guards that a name never reaches an output.
    Skips only when the denylist is absent; fails when the rule is missing."""
    if not DENYLIST.is_file():
        raise unittest.SkipTest(SKIP_MSG)
    rules = [rx for cat, rx, _ in _rules() if cat == "personal-name"]
    if len(rules) != 1:
        raise AssertionError(f"denylist has {len(rules)} personal-name rules, expected one")
    return _words(rules[0])


def retired_word():
    """The retired old system's plain name, from the denylist rule of its category; a placeholder
    in the public snapshot, where the guard tests that use it are skipped (`retired_only`)."""
    if not DENYLIST.exists():
        return "retired-name-unavailable"
    return next(rx for cat, rx, _ in _rules() if cat == legacy_names.DENYLIST_CATEGORY)


def project_names():
    """The plain words of the `project` rule (alternatives split on |)."""
    return [n for n in project_name_re().pattern.split("|") if n]


def project_name_re():
    return re.compile("|".join(rx for cat, rx, _ in _rules() if cat == "project"), re.I)
