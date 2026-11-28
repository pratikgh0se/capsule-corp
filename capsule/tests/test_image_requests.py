"""M137: a worker's missing tool becomes an image-request card a tick step builds (spec section 10).

No real docker, network or repo: a fake `docker` script (scripted by env), a temp CAPSULE_HOME, a
bare git repo as `repo_url`, a temp board db, injected `now`, `strong_up` and free disk. `_spawn`
runs the same `image_requests.run` the CLI runs, synchronously, so a pass starts a try and the
next pass reads its result. The contract change is not landed yet (request.v1 forbids `tool`, the
`capsule.toml` PROTECTED glob is absent), so those tests patch the schema and PROTECTED in memory;
the unpatched schema is asserted to refuse `tool` (the path is unreachable until it lands)."""
import copy
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from capsule import blocks, db, docker_down, image_build, image_ledger, image_requests as ir
from capsule import image_request_run as run, lease, paths, spool
from capsule.jsonschema import load_all

H2, H3, H4 = ("b" * 64, "c" * 64, "d" * 64)
NEW = f"sha256:{H4}"
NOW = 1_800_000_000
FAKE = """#!/bin/sh
case "$1$5" in buildx*|imagecapsule-python@*) ;; *) printf '%s\\n' "$*" >> "$FAKE_LOG";; esac
case "$1" in
build) cat > /dev/null; [ "$FAKE_FAIL" = build ] && exit 1;;
image) case "$2" in
  inspect) case "$4" in
    "{{.Id}} "*) echo "sha256:@H2@ capsule-python:local";;
    "{{.Id}}") echo "sha256:@H4@";;
    *) shift 4; for a in "$@"; do case "$a" in *@H4@*) echo "${FAKE_NEW_SIZE:-500000000}";;
        *) echo "${FAKE_OLD_SIZE:-400000000}";; esac; done;; esac;;
  ls) echo sha256:@H3@;;
  esac;;
run) [ -n "$FAKE_SLEEP" ] && exec sleep "$FAKE_SLEEP"
  [ -n "$FAKE_BAD_LINE" ] && case "$*" in *"$FAKE_BAD_LINE"*) exit 1;; esac;;
esac
exit 0
""".replace("@H4@", H4).replace("@H3@", H3).replace("@H2@", H2)
TOML = ('# the project image\n[image]\nstacks = ["python"]\nsmoke = ["python -V"]  # first\n\n'
        '[other]\nx = 1\n')
SCHEMAS = copy.deepcopy(load_all(paths.SCHEMAS))
SCHEMAS["request.v1"]["properties"].update(tool={"type": "string"}, bin={"type": "string"})
LOCK = f"capsule-python@sha256:{H2}\ncapsule-proj-t@sha256:{H3}\n"


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=str(cwd), check=True, capture_output=True, text=True).stdout


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.bin = self.tmp / "home", self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE)
        (self.bin / "docker").chmod(0o755)
        self.log = self.tmp / "log"
        (self.home / "boards" / "t").mkdir(parents=True)
        (self.home / "images.lock").write_text(LOCK)
        self.make_repo(TOML)
        env = {"AGENT_OPS_DOCKER_BIN": str(self.bin), "FAKE_LOG": str(self.log),
               "OPS_ENV_FILE": str(self.tmp / "n"), "CAPSULE_HOME": str(self.home)}
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, env).start()
        os.environ.pop("OPS_WORKER_IMAGE", None)
        self.free = 100 * 10**9
        mock.patch.object(ir, "_free", lambda home: self.free).start()
        mock.patch.object(ir, "_floor", lambda: 40 * 10**9).start()
        self.spawned, self.dead = [], False
        mock.patch.object(ir, "_spawn", self.spawn).start()
        p = paths.board_db("t", self.home)
        db.create_board(p)
        self.conn = db.connect(p)
        self.addCleanup(self.conn.close)
        self.cfg = {"board": "t", "repo_url": str(self.origin)}

    def make_repo(self, toml):
        self.origin, work = self.tmp / "origin.git", self.tmp / "seed"
        shutil.rmtree(self.origin, True)
        shutil.rmtree(work, True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        (work / "capsule.toml").write_text(toml)
        git(work, "add", ".")
        git(work, "commit", "-q", "-m", "seed")
        git(work, "push", "-q", str(self.origin), "main")

    def spawn(self, board, task, home, timeout, lock):
        self.spawned.append((task, timeout))
        if self.dead:
            return                                  # the builder is gone, no result
        run.run(Namespace(board=board, task=task, capsule_home=str(home), timeout=timeout))

    def waiter(self, n=1):
        out = []
        for _ in range(n):
            tid = db.new_task(self.conn, title="w", status="ready", now=NOW)
            c = lease.claim(self.conn, tid, ttl=900, role="dev")
            run = self.conn.execute("SELECT * FROM task_runs WHERE id=?", (c.run_pk,)).fetchone()
            out.append((tid, c, run))
        return out

    def ask(self, w, **kw):
        """Apply one block/capability request through the real spool path."""
        tid, c, run = w
        sp = spool.make(self.tmp / "spool", c.run_id)
        doc = {"schema": "request.v1", "run_id": c.run_id, "task_id": tid,
               "claim_lock": c.claim_lock, "verb": "block", "kind": "capability",
               "text": "need rg", **kw}
        os.utime(spool.write_doc(sp, "requests", doc), (time.time() - 60,) * 2)
        return spool.apply(self.conn, sp, run, role_verbs={"block"}, schemas=SCHEMAS, now=NOW)

    def cards(self):
        return self.conn.execute("SELECT * FROM tasks WHERE assignee='image-builder'").fetchall()

    def card(self):
        (row,) = self.cards()
        return row["id"]

    def status(self, tid):
        return self.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]

    def step(self, minutes=0, up=True):
        ir.step(self.conn, self.cfg, up, NOW + minutes * 60)

    def filed(self):
        self.ask(self.waiter()[0], tool="ripgrep", bin="rg")
        return self.card()

    def tries(self, card):
        return len(image_ledger.rows(self.home, "try", task=card))

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def reason(self, tid):
        return self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND "
                                 "kind IN ('blocked','block_loop_detected') ORDER BY id DESC",
                                 (tid,)).fetchone()[0]

    def lock_free(self):
        fh = ir._lock(self.home)
        if fh:
            fh.close()
        return fh is not None

    def hold_lock(self):
        """Hold the machine build lock as a live builder would."""
        fh = ir._lock(self.home)
        self.addCleanup(fh.close)


class Filing(Case):
    def test_1_new_card_links_the_waiter_and_counts_one_filed(self):
        w = self.waiter()[0]
        self.assertEqual(self.ask(w, tool="ripgrep", bin="rg"), ["applied"])
        card = self.cards()[0]
        self.assertEqual((card["title"], card["tier"], card["workload"], card["status"]),
                         ("Image request: ripgrep for t", "strong", "upkeep", "ready"))
        self.assertEqual((json.loads(card["file_set"]), card["branch_name"]),
                         (["capsule.toml"], f"image/{card['id']}"))
        self.assertEqual(self.status(w[0]), "todo")
        self.assertEqual(db.open_parent(self.conn, w[0]), card["id"])
        self.assertEqual(len(image_ledger.rows(self.home, "filed")), 1)
        ev = self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='image_requested'").fetchone()[0]
        self.assertEqual(json.loads(ev), {"tool": "ripgrep", "bin": "rg"})

    def test_2_second_ask_comments_and_links_without_a_card(self):
        a, b = self.waiter(2)
        self.ask(a, tool="ripgrep", bin="rg")
        self.ask(b, tool="ripgrep", bin="rg")
        card = self.card()
        self.assertEqual(db.open_parent(self.conn, b[0]), card)
        self.assertEqual(self.status(b[0]), "todo")
        self.assertEqual(len(image_ledger.rows(self.home, "filed")), 1)
        note = self.conn.execute("SELECT body FROM task_comments WHERE task_id=?",
                                 (card,)).fetchone()[0]
        self.assertIn(b[0], note)

    def test_3_bad_tool_is_an_invalid_document(self):
        w = self.waiter()[0]
        self.assertEqual(self.ask(w, tool="rg; curl x", bin="rg"), ["invalid"])
        self.assertEqual(self.ask(self.waiter()[0], tool="rg", bin="Rg Bad"), ["invalid"])
        self.assertEqual(self.ask(self.waiter()[0], tool="rg", verb="comment", kind="capability"),
                         ["invalid"])
        self.assertEqual(self.cards(), [])
        self.assertEqual(self.status(w[0]), "running")

    def test_unlanded_contract_refuses_tool(self):
        if "tool" in load_all(paths.SCHEMAS)["request.v1"]["properties"]:
            self.skipTest("request.v1 now allows tool/bin")
        tid, c, run = self.waiter()[0]
        sp = spool.make(self.tmp / "spool", c.run_id)
        doc = {"schema": "request.v1", "run_id": c.run_id, "task_id": tid, "text": "x",
               "claim_lock": c.claim_lock, "verb": "block", "kind": "capability", "tool": "rg"}
        os.utime(spool.write_doc(sp, "requests", doc), (time.time() - 60,) * 2)
        got = spool.apply(self.conn, sp, run, role_verbs={"block"},
                          schemas=load_all(paths.SCHEMAS), now=NOW)
        self.assertEqual((got, self.cards()), (["invalid"], []))

    def test_4_fourth_distinct_request_today_is_created_blocked_naming_d8(self):
        for i in range(3):
            self.ask(self.waiter()[0], tool=f"tool{i}", bin="x")
        self.ask(self.waiter()[0], tool="fourth", bin="x")
        card = [c for c in self.cards() if "fourth" in c["title"]][0]
        self.assertEqual(card["status"], "blocked")
        self.assertIn("D8 limit", self.reason(card["id"]))
        self.assertEqual(sum(c["status"] == "blocked" for c in self.cards()), 1)

    def test_closed_within_30_days_becomes_a_plain_capability_block(self):
        self.ask(self.waiter()[0], tool="ripgrep", bin="rg")
        card = self.card()
        self.conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (card,))
        w = self.waiter()[0]
        self.ask(w, tool="ripgrep", bin="rg")
        self.assertEqual((len(self.cards()), self.status(w[0])), (1, "blocked"))
        self.assertIn(card, self.reason(w[0]))


class Step(Case):
    def test_5_docker_down_counts_no_try_and_the_card_waits_in_docker_down(self):
        card = self.filed()
        self.step(up=False)
        self.assertEqual((self.spawned, self.calls(), self.tries(card)), ([], [], 0))
        self.assertIn(card, docker_down.waiting(self.conn))

    def test_low_disk_waits_without_a_try(self):
        self.filed()
        self.free = 10**9
        self.step()
        self.assertEqual(self.spawned, [])

    def test_6_smoke_failure_retries_once_then_blocks(self):
        os.environ["FAKE_BAD_LINE"] = "command -v rg"
        self.addCleanup(os.environ.pop, "FAKE_BAD_LINE", None)
        card = self.filed()
        self.step(0)
        self.step(1)
        self.assertEqual((self.tries(card), self.status(card)), (1, "ready"))
        self.step(2)
        self.step(3)
        self.assertEqual((self.tries(card), self.status(card)), (2, "blocked"))
        self.assertIn("D8 limit", self.reason(card))
        self.assertNotIn(f"capsule-proj-t@{NEW}", (self.home / "images.lock").read_text())

    def test_7_success_records_pushes_completes_and_resumes_the_waiter(self):
        w = self.waiter()[0]
        self.ask(w, tool="ripgrep", bin="rg")
        card = self.card()
        self.step(0)
        self.assertEqual(self.status(card), "ready")           # started, result not read yet
        self.step(1)
        self.assertEqual(self.status(card), "done")
        done = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND "
                                 "kind='completed'", (card,)).fetchone()
        doc = json.loads(done[0])
        self.assertEqual(doc["gates"], {"image_render": "pass", "image_smoke": "pass"})
        self.assertEqual(doc["commit"], git(self.origin, "rev-parse", f"image/{card}").strip())
        lock = (self.home / "images.lock").read_text()
        self.assertIn(f"capsule-proj-t@{NEW}", lock)
        self.assertNotIn(f"capsule-proj-t@sha256:{H3}", lock)
        toml = git(self.origin, "show", f"image/{card}:capsule.toml")
        self.assertIn('apt = ["ripgrep"]', toml)
        self.assertIn('smoke = ["python -V", "command -v rg"]  # first', toml)
        self.assertEqual(self.status(w[0]), "ready")
        self.assertTrue(self.lock_free())
        built = [c for c in self.calls() if c.startswith("build")][0]
        self.assertIn("capsule.kind=test", built)
        self.assertIn(f"capsule-proj-t:req-{card}", built)
        runs = [c for c in self.calls() if c.startswith("run")]
        self.assertTrue(runs and all("--network=none" in c for c in runs))
        author = git(self.origin, "log", "-1", "--format=%an", f"image/{card}").strip()
        self.assertEqual(author, "capsule-image-builder")

    def test_8_minutes_left_cap_the_timeout_and_zero_blocks(self):
        card = self.filed()
        image_ledger.append(self.home, "try", NOW, board="t", task="t_other", minutes=55)
        self.step(0)
        self.assertEqual(self.spawned, [(card, 300)])
        self.step(1)
        self.assertEqual(self.status(card), "done")
        image_ledger.append(self.home, "try", NOW, board="t", task="t_other", minutes=5)
        self.ask(self.waiter()[0], tool="fd", bin="fd")
        new = [c["id"] for c in self.cards() if "fd" in c["title"]][0]
        self.step(2)
        self.assertEqual((self.status(new), len(self.spawned)), ("blocked", 1))
        self.assertIn("D8 limit", self.reason(new))

    def test_human_unblock_is_an_override_with_one_extra_try(self):
        card = self.filed()
        for _ in range(2):
            image_ledger.append(self.home, "try", NOW, board="t", task=card, minutes=1)
        self.step(0)
        self.assertEqual(self.status(card), "blocked")
        blocks.unblock(self.conn, card, by="human", now=NOW)
        self.step(1)
        self.assertEqual(len(image_ledger.rows(self.home, "override")), 1)
        self.assertEqual(len(self.spawned), 1)

    def test_9_no_stacks_blocks_for_a_decision_without_a_build(self):
        self.make_repo('[image]\nsmoke = ["true"]\n')
        card = self.filed()
        self.step()
        self.assertEqual(self.status(card), "blocked")
        self.assertIn("stack", self.reason(card))
        self.assertEqual((self.spawned, self.calls()), ([], []))
        self.assertTrue(self.lock_free())

    def test_worker_image_env_blocks(self):
        card = self.filed()
        with mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "x"}):
            self.step()
        self.assertEqual((self.status(card), self.spawned), ("blocked", []))

    def test_died_run_is_a_failed_try(self):
        card = self.filed()
        self.dead = True
        self.step(0)
        self.step(1)
        self.assertEqual((self.tries(card), self.status(card)), (1, "ready"))
        self.assertTrue(self.lock_free())

    def test_a_live_builder_with_no_result_is_waited_for(self):
        card = self.filed()
        self.dead = True
        self.step(0)
        self.hold_lock()                    # the builder is alive: it holds the lock
        self.step(30)
        self.assertEqual((self.tries(card), self.status(card)), (0, "ready"))

    def test_disk_check_after_build_blocks_without_recording_or_retry(self):
        card = self.filed()
        self.step(0)
        self.free = 10 * 10**9
        self.step(1)
        self.assertEqual((self.status(card), self.tries(card)), ("blocked", 1))
        self.assertNotIn(NEW, (self.home / "images.lock").read_text())
        left = self.conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='completed'")
        self.assertEqual(left.fetchone()[0], 0)

    def test_growth_over_1gb_is_a_decision_not_an_automatic_addition(self):
        os.environ["FAKE_NEW_SIZE"] = str(2 * 10**9)
        self.addCleanup(os.environ.pop, "FAKE_NEW_SIZE", None)
        card = self.filed()
        self.step(0)
        self.step(1)
        self.assertEqual(self.status(card), "blocked")
        self.assertIn("your call", self.reason(card))
        self.assertNotIn(NEW, (self.home / "images.lock").read_text())

    def test_one_build_per_machine(self):
        self.filed()
        self.hold_lock()
        self.step()
        self.assertEqual(self.spawned, [])


if __name__ == "__main__":
    unittest.main()
