# mutation-check: targets-predate-range 6b792dc2 --
#   subject is the shell wrappers (bin, maintenance, deploy.sh), revert check is .py only ::
#   PythonPFlag.test_every_repo_module_call_has_dash_p,
#   PythonPFlag.test_scanner_self_check
"""Security sweep guard: a shell wrapper running a repo module (`python3 -m capsule...`) must
pass -P, so the cwd (a worker worktree can be the cwd) is never put on sys.path first. The
trusted repo goes on PYTHONPATH instead. Backtick-quoted text (message strings) is ignored.
Launchd templates are exempt: they start in WorkingDirectory=$CAPSULE_HOME/harness/main."""
import re
import subprocess
import unittest

from capsule import paths

GLOBS = ["bin/*", "maintenance/*.sh", "deploy.sh", "install.sh", "launchd/templates/*"]
EXEMPT = ("launchd/templates/",)  # relies on WorkingDirectory=$CAPSULE_HOME/harness/main
RUN = re.compile(
    r"""(?<![`\w])["']?(?:python[\d.]*|\$\w+|\$\{[^}]+\})["']?\s+(?:-\S+\s+)*-m\s+capsule\b"""
)
HAS_P = re.compile(r"\s-\w*P\w*\s")
QUOTED = re.compile(r"\\`.*?\\`|`[^`]*`")


def scan_line(line):
    line = QUOTED.sub("", line)
    return [m.group(0) for m in RUN.finditer(line) if not HAS_P.search(m.group(0))]


def tracked():
    out = subprocess.run(["git", "ls-files", *GLOBS], cwd=paths.REPO, check=True,
                         capture_output=True, text=True).stdout.split()
    return [f for f in out if not f.startswith(EXEMPT)]


def unsafe_calls(rel):
    lines = (paths.REPO / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    return [f"{rel}:{n}: {line.strip()}" for n, line in enumerate(lines, 1)
            if not line.lstrip().startswith("#") and scan_line(line)]


class PythonPFlag(unittest.TestCase):
    def test_every_repo_module_call_has_dash_p(self):
        bad = [x for rel in tracked() for x in unsafe_calls(rel)]
        self.assertEqual(bad, [], "python -m capsule without -P")

    def test_scanner_self_check(self):
        for bare in ('python3 -m capsule tick', '"$PY" -m capsule x',
                     '"${V:-python3}" -m capsule x', '$PYTHON -m capsule x',
                     'PYTHONPATH=x python3.11 -u -m capsule x'):
            self.assertTrue(scan_line(bare), bare)
        for ok in ('python3 -P -m capsule tick', '"$PY" -P -m capsule x',
                   'echo "run \\`python3 -m capsule x\\` now"', 'echo `python3 -m capsule x`'):
            self.assertEqual(scan_line(ok), [], ok)
        self.assertTrue(scan_line('echo "run \\`a\\`"; python3 -m capsule x'))
        self.assertTrue(tracked())


if __name__ == "__main__":
    unittest.main()
