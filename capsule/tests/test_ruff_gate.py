"""Zenkai Z1/Z2/Z4 for tests/ruff_gate.py and tests/portability_lint.py (LAND-07, ledger row 171).
Test names follow the intent row ids in contracts/intent/land_reliability.jsonl. Oracle 1 runs
each gate for real through its command line on a fixture repo built fresh per class; oracle 2
(`SecondOracle`) asks the real `ruff check --select E9,F63,F7,F82` and `grep -n 'stat -f'` the
same fixture commits and requires the same verdict. A missing ruff is red here too (Z4); only the
public snapshot, which has no private denylist, skips the real-ruff tests."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import ruff_gate  # noqa: E402
from capsule import paths  # noqa: E402
from capsule.tests.public_tree import needs_ruff  # noqa: E402

UNDEFINED = "def f():\n    return undefined_name\n"
STAT_F = '#!/bin/bash\nmode="$(stat -f %Lp "$1")"\n'
GNU_SAFE = ('#!/bin/bash\nperm() { if stat -c %a / >/dev/null 2>&1; then stat -c %a "$1"; '
            'else stat -f %Lp "$1"; fi; }\n')


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} failed: {p.stdout}{p.stderr}"
    return p.stdout.strip()


def commit(root, files, message):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def run_gate(script, root, base, head, path=None):
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    return subprocess.run([sys.executable, str(ROOT / "tests" / script), "--repo", str(root),
                           "--base", base, "--head", head],
                          capture_output=True, text=True, env=env)


class Fixture(unittest.TestCase):
    """base is clean; each later commit adds one thing, so a range is one case."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        git(cls.root, "init", "-q", "-b", "main")
        git(cls.root, "config", "user.email", "test@example.com")
        git(cls.root, "config", "user.name", "Test")
        cls.base = commit(cls.root, {"mod.py": "def f():\n    return 1\n",
                                     "tests/test_x.sh": "#!/bin/bash\necho ok\n"}, "base")
        cls.clean = commit(cls.root, {"mod.py": "def f():\n    return 2\n"}, "clean")
        cls.undefined = commit(cls.root, {"mod.py": UNDEFINED}, "undefined name")
        cls.stat_f = commit(cls.root, {"tests/test_x.sh": STAT_F}, "stat -f")
        cls.safe = commit(cls.root, {"tests/test_x.sh": GNU_SAFE}, "gnu-safe helper")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)


class RuffGate(Fixture):
    @needs_ruff
    def test_ruff_undefined_name_fails_gate(self):
        r = run_gate("ruff_gate.py", self.root, self.clean, self.undefined)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("mod.py:2", r.stdout)
        self.assertIn("F821", r.stdout)

    @needs_ruff
    def test_clean_range_passes(self):
        r = run_gate("ruff_gate.py", self.root, self.base, self.clean)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_ruff_missing_is_red_not_skipped(self):
        # PATH holds git and nothing else: no ruff, no uvx. A clean range must still be red.
        bare = tempfile.mkdtemp()
        os.symlink(shutil.which("git"), os.path.join(bare, "git"))
        for head in (self.clean, self.undefined):
            r = run_gate("ruff_gate.py", self.root, self.base, head, path=bare)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("neither ruff nor uvx", r.stdout)
        shutil.rmtree(bare)

    @needs_ruff
    def test_a_path_with_spaces_is_linted(self):
        spaced = commit(self.root, {"my mod.py": UNDEFINED}, "spaced path")
        r = run_gate("ruff_gate.py", self.root, self.safe, spaced)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("my mod.py:2", r.stdout)
        (self.root / "my mod.py").unlink()  # leave the shared fixture as it was
        commit(self.root, {}, "remove spaced path")

    @needs_ruff
    def test_only_head_content_is_linted(self):
        # the undefined name is fixed again at the head of the range, so the range is clean
        fixed = commit(self.root, {"mod.py": "def f():\n    return 3\n"}, "fixed")
        r = run_gate("ruff_gate.py", self.root, self.clean, fixed)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class PortabilityLint(Fixture):
    def test_macos_only_stat_flag_fails_portability_lint(self):
        r = run_gate("portability_lint.py", self.root, self.undefined, self.stat_f)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("tests/test_x.sh:2", r.stdout)

    def test_gnu_safe_helper_passes_portability_lint(self):
        r = run_gate("portability_lint.py", self.root, self.stat_f, self.safe)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_clean_range_passes(self):
        r = run_gate("portability_lint.py", self.root, self.base, self.clean)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def lint(self, path, text):
        root = Path(tempfile.mkdtemp())
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "user.name", "Test")
        base = commit(root, {"README": "x\n"}, "base")
        head = commit(root, {path: text}, "add")
        r = run_gate("portability_lint.py", root, base, head)
        shutil.rmtree(root)
        return r

    def test_every_macos_only_construct_is_flagged(self):
        for line in ("stat -f %Lp f", "sed -i '' s/a/b/ f", 'sed -i "" s/a/b/ f', "date -j -f %s 1",
                     "echo x | pbcopy", "sw_vers -productVersion", "/usr/bin/open f"):
            r = self.lint("bin/tool", f"#!/bin/bash\n{line}\n")
            self.assertEqual(r.returncode, 1, f"{line}: {r.stdout}{r.stderr}")
            self.assertIn("bin/tool:2", r.stdout)

    def test_guards_and_scope(self):
        ok = {"maintenance/a.sh": "#!/bin/bash\nsw_vers  # macos-only\n",
              "tests/b.sh": '#!/bin/bash\n[ "$(uname)" = Darwin ] &&\n  pbcopy < f\n',
              "tests/c.sh": "#!/bin/bash\nsed -i.bak s/a/b/ f\n",
              "docs/d.sh": "#!/bin/bash\nstat -f %Lp f\n",
              "tests/e.py": "import os\nos.system('pbcopy')\n"}
        for path, text in ok.items():
            r = self.lint(path, text)
            self.assertEqual(r.returncode, 0, f"{path}: {r.stdout}{r.stderr}")

    def test_a_guard_on_the_line_above_must_be_a_real_uname_or_darwin_check(self):
        raw = 'size="$(stat -f %z "$f")"\n'
        for guard in (GNU_SAFE.splitlines()[1] + "\n",   # the perm() helper line itself
                      "os=$(uname -m)\n",                # a uname that guards nothing
                      "x=1  # macos-only\n"):            # the marker counts on the same line only
            r = self.lint("tests/t.sh", "#!/bin/bash\n" + guard + raw)
            self.assertEqual(r.returncode, 1, f"{guard}: {r.stdout}{r.stderr}")
            self.assertIn("tests/t.sh:3", r.stdout)
        for guard in ('if [ "$(uname)" = Darwin ]; then\n', "uname | grep -q Darwin &&\n"):
            r = self.lint("tests/t.sh", "#!/bin/bash\n" + guard + raw)
            self.assertEqual(r.returncode, 0, f"{guard}: {r.stdout}{r.stderr}")

    def test_only_added_lines_are_checked(self):
        r = run_gate("portability_lint.py", self.root, self.stat_f, commit(
            self.root, {"tests/test_x.sh": STAT_F + "echo more\n"}, "touch only"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class IntentRows(unittest.TestCase):
    """Replays the stamped Z1 rows `ruff-undefined-name-fails-gate` and
    `macos-only-stat-flag-fails-portability-lint` (contracts/intent/land_reliability.jsonl):
    each case's changed_files become one commit on a clean base; the gate runs on that range."""

    def row(self, row_id):
        text = (paths.INTENT / "land_reliability.jsonl").read_text()
        rows = [json.loads(x) for x in text.splitlines()]
        return next(r for r in rows if r["id"] == row_id)

    def replay(self, row_id):
        row = self.row(row_id)
        doc = row["given"]["document"]
        script = doc["check"] + ".py"
        pins = {"strict_select": ruff_gate.STRICT_SELECT, "strict_ignore": ruff_gate.STRICT_IGNORE}
        for key, value in pins.items():
            if key in doc:
                self.assertEqual(doc[key], value)
        for name, case in doc["ranges"].items():
            with self.subTest(case=name):
                root = Path(tempfile.mkdtemp())
                git(root, "init", "-q", "-b", "main")
                git(root, "config", "user.email", "test@example.com")
                git(root, "config", "user.name", "Test")
                base = commit(root, {"README": "x\n", **case.get("base_files", {})}, "base")
                head = commit(root, case["changed_files"], "case")
                bare = None
                if case.get("ruff") == "absent":
                    bare = tempfile.mkdtemp()
                    os.symlink(shutil.which("git"), os.path.join(bare, "git"))
                r = run_gate(script, root, base, head, path=bare)
                self.check_case(row["expect"][name], r)
                shutil.rmtree(root)
        return len(doc["ranges"])

    def check_case(self, want, r):
        self.assertEqual(r.returncode, want["exit"], r.stdout + r.stderr)
        if want.get("skipped") is False:
            self.assertNotIn(": ok", r.stdout)
        if "hits" in want:
            hits = want["hits"] if isinstance(want["hits"], dict) else {}
            got = {}
            for line in r.stdout.splitlines():
                if m := re.match(r"\s*(\S+?:\d+)[: ]", line):
                    got[m.group(1)] = got.get(m.group(1), "") + line
            self.assertEqual(set(got), set(hits), r.stdout)
            for where, token in hits.items():
                self.assertIn(token, got[where])

    @needs_ruff
    def test_ruff_undefined_name_fails_gate(self):
        self.assertEqual(self.replay("ruff-undefined-name-fails-gate"), 2)

    @needs_ruff
    def test_ruff_strict_changed_lines(self):
        self.assertEqual(self.replay("ruff-strict-changed-lines"), 6)

    def test_macos_only_stat_flag_fails_portability_lint(self):
        self.assertEqual(self.replay("macos-only-stat-flag-fails-portability-lint"), 3)


class SecondOracle(Fixture):
    """Independent check: the real tools on the same fixture ranges agree with the gates."""

    @needs_ruff
    def test_ruff_gate_matches_real_ruff(self):
        for base, head in ((self.base, self.clean), (self.clean, self.undefined)):
            gate = run_gate("ruff_gate.py", self.root, base, head).returncode
            files = git(self.root, "show", f"{head}:mod.py") + "\n"
            real = subprocess.run([*ruff_gate.ruff_cmd(), "check", "--isolated", "--select",
                                   "E9,F63,F7,F82", "--stdin-filename", "mod.py", "-"],
                                  input=files, capture_output=True, text=True).returncode
            self.assertEqual(gate, 1 if real else 0)

    def test_portability_lint_matches_grep(self):
        for base, head in ((self.base, self.clean), (self.undefined, self.stat_f)):
            gate = run_gate("portability_lint.py", self.root, base, head).returncode
            added = git(self.root, "diff", "-U0", f"{base}..{head}", "--", "tests/test_x.sh")
            hits = subprocess.run(["grep", "-n", "^+.*stat -f"], input=added, capture_output=True,
                                  text=True).returncode == 0
            self.assertEqual(gate, 1 if hits else 0)


if __name__ == "__main__":
    unittest.main()
