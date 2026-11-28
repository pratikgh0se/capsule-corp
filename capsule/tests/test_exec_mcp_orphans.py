"""The strong-tier exec tool and orphans: a command's output goes to container files, so a server
it leaves behind never blocks the return and keeps running after a normal exit; only on a timeout
is the command's whole session killed, and the partial output comes back with one closing
`[exec: timed out ...]` line (never a silent empty result).
Fakes for the argv and the text; RealDocker (RUN_DOCKER_TESTS=1, a child process, the local
capsule-python image, never pulled) for the behaviour."""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import paths
from capsule.runners import exec_mcp

IMAGE = "capsule-python:local"


def cfg_for(docker, **kw):
    return argparse.Namespace(container="capsule-x", sandbox_profile=None, spool=tempfile.mkdtemp(),
                              docker=docker, dry_run=kw.get("dry_run", False), workspace=None)


class Construction(unittest.TestCase):
    def test_argv_runs_the_wrapper_under_an_in_container_timeout_with_cmd_as_an_argument(self):
        done = exec_mcp._run_exec(cfg_for("docker", dry_run=True), "echo 'a b'; make", 30)
        argv = json.loads(done["text"])
        self.assertEqual(argv[:6], ["docker", "exec", "-w", "/workspace", "capsule-x", "timeout"])
        self.assertIn("--preserve-status", argv)
        self.assertEqual(argv[-4:-2], ["-c", exec_mcp._WRAP])
        self.assertTrue(argv[-1].endswith("echo 'a b'; make"))   # $1 = the command
        self.assertEqual(argv[-2], "30")                         # $0 = the timeout

    def test_wrapper_files_the_output_and_kills_the_group_only_in_the_term_trap(self):
        wrap = exec_mcp._WRAP
        self.assertIn("setsid", wrap)
        self.assertIn('sh -lc "$1" >$d/o 2>$d/e', wrap)
        trap, normal = wrap.split("' TERM;")
        self.assertIn("kill -KILL -$p", trap)
        self.assertNotIn("kill", normal)      # a normal exit leaves servers running
        self.assertIn("exit $rc", normal)

    def test_dry_run_returns_the_argv(self):
        done = exec_mcp._run_exec(cfg_for("docker", dry_run=True), "true", 5)
        self.assertEqual(done["exit_code"], 0)
        self.assertTrue(json.loads(done["text"])[-1].endswith("true"))


class Texts(unittest.TestCase):
    def fake(self, body):
        d = Path(tempfile.mkdtemp())
        path = d / "docker"
        # `exec` runs `body`; the daemon probes after a failed exec answer "up and running"
        path.write_text('#!/bin/sh\ncase "$1" in\n  inspect) echo true; exit 0;;\n'
                        '  version) exit 0;;\nesac\n' + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def test_partial_output_survives_a_client_side_expiry_with_the_tip(self):
        ready = Path(tempfile.mkdtemp()) / "ready"
        docker = self.fake(f"echo partial-line\n: > {ready}\nexec sleep 60\n")

        class Gated(subprocess.Popen):
            """The client's expiry fires only once the fake has written (a clock race otherwise:
            under load the shell can start after a 1 s timeout)."""
            def wait(self, timeout=None):
                if timeout is not None and "exec" in self.args:
                    deadline = time.monotonic() + 30
                    while (not ready.exists() and self.poll() is None
                           and time.monotonic() < deadline):
                        time.sleep(0.01)
                    timeout = 0
                return super().wait(timeout)
        old = exec_mcp._CLIENT_TIMEOUT_SLACK
        exec_mcp._CLIENT_TIMEOUT_SLACK = -4      # the client gives up after 1s
        try:
            with mock.patch.object(exec_mcp.subprocess, "Popen", Gated):
                done = exec_mcp._run_exec(cfg_for(docker), "x", 5)
        finally:
            exec_mcp._CLIENT_TIMEOUT_SLACK = old
        self.assertEqual(done["exit_code"], -1)
        self.assertTrue(done["text"].startswith("partial-line\n"))
        self.assertIn("[client-side timeout after 1s", done["text"])
        self.assertIn("redirect output to a file", done["text"])

    def test_exit_code_and_both_streams_pass_through_unchanged(self):
        docker = self.fake("echo out; echo err >&2; exit 7\n")
        self.assertEqual(exec_mcp._run_exec(cfg_for(docker), "x", 5),
                         {"exit_code": 7, "text": "out\nerr\n"})

    def test_missing_docker_binary_is_127(self):
        done = exec_mcp._run_exec(cfg_for("/nonexistent/docker"), "x", 5)
        self.assertEqual(done["exit_code"], 127)


_CHILD = (
    "import argparse, json, os, subprocess, sys, tempfile, time\n"
    "sys.path.insert(0, {repo!r})\n"
    "from capsule.runners import exec_mcp\n"
    "with tempfile.TemporaryDirectory() as root:\n"
    "    name = 'capsule-orphan-test-' + str(os.getpid())\n"
    "    subprocess.run(['docker', 'run', '-d', '--init', '--network=none', '--name', name,\n"
    "                    '-v', root + ':/workspace', {image!r}, 'sleep', 'infinity'],\n"
    "                   check=True, capture_output=True)\n"
    "    out = {{}}\n"
    "    try:\n"
    "        cfg = argparse.Namespace(container=name, sandbox_profile=None, spool=root,\n"
    "                                 docker='docker', dry_run=False, workspace=None)\n"
    "        def count(n):\n"
    "            sh = 'ps -eo args | grep -c \"[s]leep ' + n + '\"'\n"
    "            run = ['docker', 'exec', name, 'sh', '-c', sh]\n"
    "            return subprocess.run(run, capture_output=True, text=True).stdout.strip()\n"
    "        def go(key, cmd, t):\n"
    "            start = time.time()\n"
    "            out[key] = exec_mcp._run_exec(cfg, cmd, t)\n"
    "            out[key]['secs'] = time.time() - start\n"
    "        go('a', 'sleep 301 & echo started', 30)\n"
    "        out['a_left'] = count('301')\n"
    "        go('b', 'nohup sleep 302 > /tmp/s.log 2>&1 &', 30)\n"
    "        out['b_left'] = count('302')\n"
    "        go('c', 'sh -c \"sleep 303 & echo x\" | tail -1', 3)\n"
    "        out['c_left'] = count('303')\n"
    "        go('d', 'echo before; sleep 304 & sleep 304', 3)\n"
    "        out['d_left'] = count('304')\n"
    "        go('pgid', 'ps -o pid=,pgid=,sid= -p $$', 30)\n"
    "    finally:\n"
    "        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)   # our test box\n"
    "print(json.dumps(out))\n")


class RealDocker(unittest.TestCase):
    def test_orphans_die_and_a_timeout_returns_partial_output_and_the_note(self):
        if os.environ.get("RUN_DOCKER_TESTS") != "1":    # in the body: the gate flags a decorator
            self.skipTest("real docker: RUN_DOCKER_TESTS=1")
        if not shutil.which("docker") or subprocess.run(
                ["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0:
            self.skipTest(f"{IMAGE} is not present")
        env = {**os.environ, "OPS_WORKER_IMAGE": IMAGE}
        script = _CHILD.format(repo=str(paths.REPO), image=IMAGE)
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              env=env, timeout=300)
        self.assertTrue(done.stdout.strip(), done.stderr)
        got = json.loads(done.stdout.strip().splitlines()[-1])
        # a leftover server neither blocks the return nor dies with a normal exit
        self.assertEqual((got["a"]["exit_code"], got["a"]["text"]), (0, "started\n"))
        self.assertLess(got["a"]["secs"], 20)
        self.assertEqual(got["a_left"], "1")
        self.assertEqual(got["b"]["exit_code"], 0)
        self.assertEqual(got["b_left"], "1")
        # an orphan holding the inner pipe: the timeout kills the session, names the process
        note = "[exec: timed out after 3s and was killed; still running at timeout:"
        self.assertEqual(got["c"]["exit_code"], 137)
        self.assertIn(note, got["c"]["text"])
        self.assertIn("sleep 303", got["c"]["text"])
        self.assertEqual(got["c_left"], "0")
        text = got["d"]["text"]
        self.assertEqual(got["d"]["exit_code"], 137)
        self.assertTrue(text.startswith("before\n"), text)
        self.assertIn(note, text)
        self.assertIn("redirect output to a file", text)
        self.assertLess(got["d"]["secs"], 20)
        self.assertEqual(got["d_left"], "0")
        pid, pgid, sid = got["pgid"]["text"].split()
        self.assertEqual(pgid, sid)      # the command's shell leads its own session
        self.assertEqual(pid, pgid)

if __name__ == "__main__":
    unittest.main()
