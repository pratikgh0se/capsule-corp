"""Run a snippet under the legacy system at 98e87ac as the oracle (spec §12.1, plan P11).

Skips unless CAPSULE_LEGACY_PY (an interpreter that can import it) and
CAPSULE_LEGACY_REPO (its clone) are set. HOME and its home variable point
at the test's temp dir, so it never sees the real home directory.
"""
import json
import os
import subprocess
import unittest

from capsule import legacy_names

PY = os.environ.get("CAPSULE_LEGACY_PY")
REPO = os.environ.get("CAPSULE_LEGACY_REPO")
needs_legacy = unittest.skipUnless(
    PY and REPO, "set CAPSULE_LEGACY_PY and CAPSULE_LEGACY_REPO for legacy oracle tests")


def legacy(script, home):
    env = dict(os.environ, HOME=str(home), PYTHONPATH=REPO,
               **{legacy_names.OLD_ENV_HOME: str(home)})
    prelude = f"import json\nfrom {legacy_names.LEGACY_CLI_PKG} import kanban_db as kb\n"
    out = subprocess.run([PY, "-c", prelude + script], env=env, cwd=REPO,
                         capture_output=True, text=True, timeout=180)
    if out.returncode:
        raise AssertionError(out.stderr[-3000:])
    return json.loads(out.stdout.strip().splitlines()[-1])
