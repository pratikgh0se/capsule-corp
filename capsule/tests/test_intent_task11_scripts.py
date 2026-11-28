"""Zenkai Z1 (row 526, card t_1758e0bd): the intent examples of the two plain scripts the Task 11
leftovers touch, replayed through the real scripts.

`lib_jobs`: each example's `line` names a launchd/unloaded file shape; the real bin/lib_jobs.sh is
sourced over it and the example's `level` must hold (`dropped`: the label left DISPATCHING and
UNLOADED_LABELS holds it bare; `ignored`: no label at all). The `capsule_tick_sh` and
`deploy` examples are replayed at the tail of tests/test_capsule_tick_sh.sh and tests/test_deploy.sh
(each names a check of that suite that must have passed; no second run of a 40 s suite here)."""
import os
import subprocess
import unittest

from capsule import intent, paths
from capsule.tests.public_tree import PRIVATE_TREE

LABEL = "com.example.capsule-tick-capsule-corp"
FILES = {
    "trailing-space-and-tab": f"{LABEL} \t\n",
    "crlf": f"{LABEL}\r\n",
    "indented-comment": f"  # {LABEL}\n",
}


def setUpModule():
    os.environ["CAPSULE_LAUNCHD_PREFIX"] = "com.example"  # the cases assert the author's labels


def tearDownModule():
    os.environ.pop("CAPSULE_LAUNCHD_PREFIX", None)


class LibJobsExamples(unittest.TestCase):
    def test_every_example_holds_on_the_real_script(self):
        import tempfile
        examples = intent.load("lib_jobs")
        self.assertEqual(len(examples), 3)          # an absent file would loop over nothing and pass
        for ex in examples:
            with self.subTest(ex["id"]), tempfile.TemporaryDirectory() as tmp:
                f = os.path.join(tmp, "unloaded")
                with open(f, "w", newline="") as fh:
                    fh.write(FILES[ex["given"]["document"]["line"]])
                out = subprocess.run(
                    ["bash", "-c", 'REPO="$1"; . "$1/bin/lib_jobs.sh"; printf "[%s]" "${UNLOADED_LABELS[@]}"; '
                     'echo; printf "%s\\n" "${DISPATCHING[@]}"', "_", str(paths.REPO)],
                    env={**os.environ, "CAPSULE_LAUNCHD_UNLOADED": f}, capture_output=True, text=True, timeout=60).stdout
                held, _, dispatching = out.partition("\n")
                if ex["expect"]["level"] == "dropped":
                    self.assertEqual(held, f"[{LABEL}]", ex["id"])
                    self.assertNotIn(LABEL, dispatching.split(), ex["id"])
                else:
                    self.assertEqual(held, "[]", ex["id"])
                    self.assertIn(LABEL, dispatching.split(), ex["id"])

    def test_prefix_from_env(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        def tick_label(env):
            cmd = 'REPO="$1"; . "$1/bin/lib_jobs.sh"; printf "%s\\n" "${DISPATCHING[@]}"'
            e = {**{k: v for k, v in os.environ.items() if k != "CAPSULE_LAUNCHD_PREFIX"}, **env,
                 "CAPSULE_LA_DIR": tmp}
            r = subprocess.run(["bash", "-c", cmd, "_", str(paths.REPO)], env=e,
                               capture_output=True, text=True)
            return r.stdout.split()
        self.assertIn("dev.capsule.capsule-tick-capsule-corp", tick_label({}))
        got = tick_label({"CAPSULE_LAUNCHD_PREFIX": "x.y"})
        self.assertIn("x.y.capsule-tick-capsule-corp", got)


class RealUnloadedFile(unittest.TestCase):
    """The committed launchd/unloaded no longer names the retired automation gateway (D198/D202)."""

    def test_unloaded_file_has_no_automation_gateway(self):
        env = {k: v for k, v in os.environ.items() if k != "CAPSULE_LAUNCHD_UNLOADED"}
        out = subprocess.run(
            ["bash", "-c", 'REPO="$1"; . "$1/bin/lib_jobs.sh"; '
             'printf "[%s]\n" "${UNLOADED_LABELS[@]}"; '
             'for g in ${GATEWAYS[@]+"${GATEWAYS[@]}"}; do echo "G:$g"; done; '
             'classify_check', "_", str(paths.REPO)],
            env=env, capture_output=True, text=True, timeout=60).stdout
        self.assertNotIn("gateway-automation", out)
        if PRIVATE_TREE:
            unloaded = (paths.REPO / "launchd" / "unloaded").read_text()
            self.assertNotIn("gateway-automation", unloaded)
        self.assertNotIn("G:", out)   # gateway class retired: no entries at all
        self.assertNotIn("gateway-telegram", out)   # D229: the Telegram label is gone
        self.assertNotIn("unclassified", out.lower())


if __name__ == "__main__":
    unittest.main()
