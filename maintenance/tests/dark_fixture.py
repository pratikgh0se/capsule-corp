"""Shared fixture for the dark_test suites: temp HOME/state, stub python3 and launchctl on PATH."""
import contextlib
import datetime
import io
import json
import os
import plistlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dark_test  # noqa: E402
from capsule import legacy_names as _LN  # noqa: E402

STUB = '#!/bin/bash\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'
PY_STUB = STUB + (
    'key=$(echo "${1:-} ${2:-} ${3:-}" | tr " /" "__")\n'
    '[ -f "$STUB_DIR/$key.err" ] && cat "$STUB_DIR/$key.err" >&2\n'
    '[ -f "$STUB_DIR/$key.out" ] && cat "$STUB_DIR/$key.out"\n'
    '[ -f "$STUB_DIR/$key.hang" ] && { sleep 300 & echo $! > "$STUB_DIR/child.pid"; wait; }\n'
    'exit $(cat "$STUB_DIR/$key.rc" 2>/dev/null || echo 0)\n')
DEPLOY_STUB = STUB + (      # `_set("deploy", rc=, out=)` scripts it
    '[ -f "$STUB_DIR/deploy.out" ] && cat "$STUB_DIR/deploy.out"\n'
    'exit $(cat "$STUB_DIR/deploy.rc" 2>/dev/null || echo 0)\n')
LAUNCHCTL_STUB = STUB + (
    'f="$STUB_DIR/launchctl.${2##*/}"\n'
    '[ "$1" = print ] && [ -f "$f" ] && { cat "$f"; exit 0; }\nexit 113\n')
# label: (ProgramArguments template, schedule); {m} is the maintenance dir
PLISTS = {
    "dev.capsule.capsule-corp-heal": (["/bin/bash", "{m}/run.sh", "heal"], {"StartInterval": 300}),
    "dev.capsule.capsule-corp-cleanup": (["/bin/bash", "{m}/run.sh", "cleanup"], {}),
    "dev.capsule.obsidian-report": (["/bin/bash", "{m}/report.sh"], {}),
    "dev.capsule.handoff-canary": (["/bin/bash", "-lc", "/x/handoff-canary/run.sh --catch-up"], {}),
    "dev.capsule.capsule-corp-keeper-watch": (["/bin/bash", "/x/bin/capsule-keeper-watch.sh"], {}),
    "dev.capsule.machine-setup-autosync": (
        ["/bin/bash", "-lc", "P='/y' '/x/setup/autosync.sh'"], {}),
    f"ai.{_LN.NAME}.gateway-telegram": ([f"/x/{_LN.NAME}", "gateway"], {}),
    "dev.capsule.capsule-tick-capsule-corp": (["/bin/bash", "/x/tick.sh"], {}),
    "dev.capsule.ollama-serve": (["/x/ollama", "serve"], {}),
    "dev.capsule.unrelated": (["/usr/bin/true"], {}),
}
INCLUDED = ["dev.capsule.capsule-corp-heal", "dev.capsule.capsule-corp-cleanup",
            "dev.capsule.obsidian-report", "dev.capsule.handoff-canary",
            "dev.capsule.capsule-corp-keeper-watch", "dev.capsule.machine-setup-autosync"]
HEAL = "dev.capsule.capsule-corp-heal"


class DarkBase(unittest.TestCase):
    def setUp(self):
        self.d = d = Path(tempfile.mkdtemp())
        self.maint = d / "maintenance"
        for sub in ("bin", "repo", "maintenance/logs", "LA", "stubs", "state/jobs",
                    "state/outbox"):
            (d / sub).mkdir(parents=True)
        self.log = d / "calls.log"
        self.log.touch()
        self._stub(d / "bin" / "python3", PY_STUB)
        self._stub(d / "bin" / "launchctl", LAUNCHCTL_STUB)
        self._stub(d / "repo" / "deploy.sh", DEPLOY_STUB)
        self.plist("dev.capsule.capsule-corp-heal", extra={
            "StandardErrorPath": str(self.maint / "logs" / "launchd-heal.err")})
        for label in PLISTS:
            if label != HEAL:
                self.plist(label)
            self.launchd(label, runs=10, exit_code=0)
        env = {"HOME": str(d), "LEGACY_KANBAN_HOME": str(d / ".legacy-home"),
               "AGENT_OPS_STATE_DIR": str(d / "state"), "DARK_TEST_LAUNCHAGENTS": str(d / "LA"),
               "CAPSULE_LAUNCHD_UNLOADED": str(d / "no-unloaded-file"),   # never the real list
               "STUB_LOG": str(self.log), "STUB_DIR": str(d / "stubs"),
               "PATH": f"{d / 'bin'}:{os.environ['PATH']}"}
        self.wake_ts = 0        # 0 = wake time unreadable: tests never call sysctl
        for p in (mock.patch.dict(os.environ, env),
                  mock.patch.object(dark_test, "REPO", d / "repo"),
                  mock.patch.object(dark_test, "_last_wake", lambda: self.wake_ts, create=True)):
            p.start()
            self.addCleanup(p.stop)
        # explicit: python3 is a stub here, so no host.json reader may run
        os.environ["CAPSULE_LAUNCHD_PREFIX"] = "dev.capsule"

    def _stub(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    def plist(self, label, extra=None, schedule=None):
        args, sched = PLISTS[label]
        argv = [a.replace("{m}", str(self.maint)) for a in args]
        data = {"Label": label, "ProgramArguments": argv,
                **(schedule if schedule is not None else sched), **(extra or {})}
        path = self.d / "LA" / f"{label}.plist"
        path.write_bytes(plistlib.dumps(data))
        old = time.time() - 30 * 86400      # installed long before any test window
        os.utime(path, (old, old))

    def launchd(self, label, runs, exit_code=0):
        (self.d / "stubs" / f"launchctl.{label}").write_text(
            f"\truns = {runs}\n\tlast exit code = {exit_code}\n")

    def _set(self, key, rc=None, err=None, out=None, hang=False):
        for ext, val in (("rc", rc), ("err", err), ("out", out), ("hang", "1" if hang else None)):
            if val is not None:
                (self.d / "stubs" / f"{key}.{ext}").write_text(str(val))

    def run_cmd(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = dark_test.main(list(argv))
        return rc, buf.getvalue()

    def started(self, ago_s=3600):
        """`start`, then backdate the marker (the baselines stay as `start` recorded them)."""
        self.assertEqual(self.run_cmd("start")[0], 0)
        marker = self.d / "state" / "dark-test" / "start.json"
        base = json.loads(marker.read_text())
        base["start_ts"] = int(time.time()) - ago_s
        marker.write_text(json.dumps(base))
        return base

    def woke(self, ago_s):
        """The host last woke `ago_s` seconds ago."""
        self.wake_ts = int(time.time()) - ago_s

    def job_state(self, job="heal", exit_code=0, log=None, age_s=0):
        when = datetime.datetime.now().astimezone() - datetime.timedelta(seconds=age_s)
        end = when.isoformat(timespec="seconds")
        (self.d / "state" / "jobs" / f"{job}.json").write_text(
            json.dumps({"job": job, "end": end, "exit": exit_code, "log": str(log or "")}))

    def results(self):
        p = self.d / "state" / "dark-test" / "results.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines()]

    def row(self, item):
        return [r for r in self.results() if r["item"] == item][-1]

    def calls(self):
        return self.log.read_text().splitlines()

    def check_heal_ok(self):
        """The heal job ran since start, cleanly: the baseline every FAIL case departs from."""
        self.launchd(HEAL, runs=12)
        self.job_state()
