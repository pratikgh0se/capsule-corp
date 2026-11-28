"""R28 (B1b): the blind run in report mode (`capsule/blind/run.py`, hooked in through
`blind_run.start`, the contract job's steps). Nothing here starts a real container or a model: a
stand-in for the strong runner runs the run line on the host, with `/blind` and `/out` mapped onto
the real snapshot and out directories, so the REAL harness runner produces the REAL results.jsonl
and summary line.

Z2 oracles: the harness's results.jsonl and the runner's summary line + exit code (two data
paths); for "worktree stays clean", `git status --porcelain` on the host and the `:ro` mount flag
the tick asked docker for (`docker.run_args`, in test_blind_run_parts.py). Z1: the intent rows are
replayed in test_blind_run_tick.py. This file also holds the helpers the other two import."""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from capsule import config, db, incidents, paths, workspace
from capsule.blind import cli
from capsule.blind import run as blind_run
from capsule.blind import unavailable
from capsule.jsonschema import load_all, validate
from capsule.runners import docker
from capsule.tests import tmpclean

TESTS = """import unittest

from harness import run


class Greeting(unittest.TestCase):
    def test_greets(self):
        self.assertEqual(run("cat hello.txt").stdout, "hi\\n")

    def test_fails(self):
        self.assertEqual(run("echo no").exit, 1)
"""
PASSING = TESTS.replace('run("echo no").exit, 1', 'run("echo no").exit, 0')
THREE = PASSING + """
    def test_third(self):
        self.assertTrue(run("true").exit == 0)
"""
BOARD = "sample-app"


def sh(*args, cwd=None):
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def examples(card, names):
    return "".join(json.dumps({"id": f"{card}.{n}", "given": f"a user runs {n}",
                               "expect": f"{n} behaves", "source": f"{card} acceptance 1"}) + "\n"
                   for n in names)


def blind_repo(root, card, source, names=("test_greets", "test_fails"), board=BOARD):
    """A real blind repo (`capsule blind init`'s own creator) with one card's tests committed."""
    repo = Path(root) / "sample-app-blind"
    cli._create_repo(repo, board, "unittest")
    (repo / "cards" / card).mkdir(parents=True)
    (repo / "cards" / card / "test_it.py").write_text(source)
    (repo / "cards" / card / "examples.jsonl").write_text(examples(card, names))
    sh("git", "-C", str(repo), "add", "-A")
    sh("git", "-C", str(repo), *workspace.IDENTITY, "commit", "-q", "-m", "tests")
    return repo


def record(conn, card, repo):
    """What R31's landing does: the blind repo's HEAD recorded on the card as its blind commit."""
    conn.execute("UPDATE tasks SET blind_commit=? WHERE id=?",
                 (sh("git", "-C", str(repo), "rev-parse", "HEAD").strip(), card))


def make_ws(root):
    """A card worktree shaped like the tick's clones: git dir beside it under `.gitdirs`."""
    ws = Path(root) / "work" / "card"
    gd = Path(root) / "work" / ".gitdirs" / "card"
    gd.parent.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", "--separate-git-dir", str(gd), str(ws))
    (ws / "hello.txt").write_text("hi\n")
    sh("git", "-C", str(ws), "add", "-A")
    sh("git", "-C", str(ws), *workspace.IDENTITY, "commit", "-q", "-m", "base")
    return ws


def rows_of(tick, cfg, run_id, ws, conn, task):
    """The blind rows of one card, the contract job's three steps run in a row on this thread."""
    pending = blind_run.start(tick, cfg, run_id, ws, conn, task, tick.strong)
    if pending is None:
        return []
    pending.run()
    return pending.finish(tick, conn)


class Container:
    """The strong runner's stand-in: records `start`, runs the run line on the host."""

    def __init__(self, ws, after=None, tamper=None, boom=None):
        self.ws, self.after, self.tamper, self.boom = ws, after, tamper, boom
        self.started, self.stopped = [], []

    def start(self, run_id, ws, **kw):
        self.started.append((run_id, str(ws), kw))
        self.ws = Path(ws)              # the checkout the tick mounts, not the card's own worktree
        self.mounts = dict(m.split(":")[:2][::-1] for m in kw.get("mounts", ()))   # dst -> src
        return "capsule-" + run_id[:12]

    def exec(self, name, cmd, timeout):
        if self.boom:
            raise self.boom
        line = cmd.replace("/blind", self.mounts["/blind"]).replace(
            "--card", f"--out {self.mounts['/out']} --card", 1)
        done = subprocess.run(["sh", "-c", line], cwd=self.ws, capture_output=True, text=True,
                              env={**os.environ, "BLIND_WORKSPACE": str(self.ws)})
        if self.after:
            self.after(self.ws)
        text = done.stdout + done.stderr
        return done.returncode, self.tamper(text) if self.tamper else text

    def stop(self, name):
        self.stopped.append(name)


class StubTick:
    host = "testhost"

    def __init__(self, root, strong, base):
        self.home, self.strong, self.base = Path(root) / "home", strong, base
        self.schemas = load_all(paths.SCHEMAS)
        self.root = Path(root)

    def _spool_root(self, cfg):
        return self.root / "spool"

    def _start_sha(self, conn, task_id):
        return self.base


class RunCase(unittest.TestCase):
    """One card, one blind repo, one worktree, one database; `go` runs the seam."""

    mode = "report"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.root)
        board = self.root / "kanban.db"
        db.create_board(board)
        self.conn = db.connect(board)
        self.addCleanup(self.conn.close)
        self.card = db.new_task(self.conn, title="card", assignee="dev", branch="epic/x")
        self.ws = make_ws(self.root)
        self.base = workspace.head(self.ws)
        self.repo = blind_repo(self.root, self.card, TESTS)
        record(self.conn, self.card, self.repo)

    def cfg(self, **over):
        block = {"repo": str(self.repo), "adapter": "unittest"}
        return {"board": BOARD, "redesign": {"blind_tests": self.mode}, "blind_tests": block,
                **over}

    def go(self, container, cfg=None):
        self.tick = StubTick(self.root, container, self.base)
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.card,)).fetchone()
        self.run_id = "ab" * 16
        return rows_of(self.tick, cfg or self.cfg(), self.run_id, self.ws, self.conn, task)

    def sp(self):
        return self.root / "spool" / self.run_id

    def result(self):
        return json.loads((self.sp() / "blind_result.json").read_text())   # the spool, not /out

    def porcelain(self):
        return sh("git", "-C", str(self.ws), "status", "--porcelain")


class ReportMode(RunCase):
    def test_a_report_run_gives_one_row_per_test_and_never_a_verdict(self):
        box = Container(self.ws)
        rows = self.go(box)
        by_cmd = {r["cmd"]: r for r in rows}
        self.assertEqual(sorted(by_cmd), [f"blind:{self.card}.test_fails",
                                          f"blind:{self.card}.test_greets"])
        self.assertEqual([by_cmd[f"blind:{self.card}.test_greets"]["exit"],
                          by_cmd[f"blind:{self.card}.test_fails"]["exit"]], [0, 1])
        self.assertTrue(all(r["blind"] == "report" for r in rows))
        text = by_cmd[f"blind:{self.card}.test_fails"]["output"]
        for want in ("given: a user runs test_fails", "expect: test_fails behaves",
                     "observed: echo no -> exit 0", "no"):
            self.assertIn(want, text)
        self.assertNotIn("assert", text.lower())      # no assertion text, no traceback
        self.assertEqual(incidents.listing(self.conn), [])

    def test_the_rendered_result_is_blind_result_v1_and_blind_out_keeps_only_two_files(self):
        self.go(Container(self.ws))
        doc = self.result()
        self.assertEqual(validate(doc, self.tick.schemas["blind_result.v1"]), [])
        self.assertEqual((doc["red"], doc["counts"]),     # no base-commit red check was run
                         ("not-run", {"pass": 1, "fail": 1, "error": 0, "skipped": 0}))
        self.assertEqual({t["id"]: t["result"] for t in doc["tests"]},
                         {f"{self.card}.test_greets": "pass", f"{self.card}.test_fails": "fail"})
        self.assertEqual((doc["claim_lock"], doc["base_sha"], doc["head_sha"]),
                         (f"testhost:{self.run_id}", self.base, workspace.head(self.ws)))
        self.assertEqual(doc["blind_commit"], sh("git", "-C", str(self.repo), "rev-parse",
                                                 "HEAD").strip())
        self.assertEqual({t["id"]: t["source"] for t in doc["tests"]},
                         {f"{self.card}.test_greets": f"{self.card} acceptance 1",
                          f"{self.card}.test_fails": f"{self.card} acceptance 1"})
        self.assertEqual(sorted(p.name for p in (self.sp() / "blind-out").iterdir()),
                         ["results.jsonl"])        # the product's mount holds only its own file
        self.assertTrue((self.sp() / "blind_result.json").is_file())   # the result is outside it

    def test_the_snapshot_is_gone_and_the_container_was_sealed(self):
        box = Container(self.ws)
        self.go(box)
        self.assertFalse((self.sp() / "blind").exists())
        self.assertFalse((self.sp() / "blind.tar").exists())
        (run_id, ws, kw), = box.started
        work = paths.board_dir(BOARD, self.tick.home) / "work"   # the neutral checkout, not self.ws
        self.assertEqual((Path(ws).parent.parent, Path(ws).name), (work, "card"))
        self.assertEqual((kw["ro"], kw["tmpdir"]), (True, docker.CONTRACT_TMPDIR))
        self.assertEqual(kw["mounts"], (f"{self.sp() / 'blind'}:/blind:ro",
                                        f"{self.sp() / 'blind-out'}:/out"))
        self.assertEqual(kw["git_dir"], str(workspace.gitdir(Path(ws))))
        self.assertFalse(Path(ws).parent.exists())              # the checkout is removed too
        self.assertEqual(box.stopped, ["capsule-" + run_id[:12]])

    def test_the_worktree_is_clean_after_the_run(self):
        before = self.porcelain()
        self.go(Container(self.ws))
        self.assertEqual((before, self.porcelain()), ("", ""))
        self.assertEqual(self.result()["counts"]["fail"], 1)  # a real run, not a skipped one

    def test_a_container_that_writes_the_worktree_makes_the_run_inconclusive(self):
        box = Container(self.ws, after=lambda ws: (ws / "planted.txt").write_text("x"))
        rows = self.go(box)
        self.assertEqual([r["cmd"] for r in rows], ["blind:inconclusive"])
        self.assertIn("changed its read-only checkout of the card's work", rows[0]["output"])
        self.assertEqual(self.result()["red"], "inconclusive")
        self.assertEqual([i["kind"] for i in incidents.listing(self.conn)], ["review_infra"])

    def test_a_planted_count_mismatch_is_inconclusive_and_opens_an_incident(self):
        repo = blind_repo(self.root / "r2", self.card, THREE,
                          ("test_greets", "test_fails", "test_third"))
        record(self.conn, self.card, repo)
        box = Container(self.ws, tamper=lambda text: text.replace("Ran 3 tests", "Ran 2 tests"))
        rows = self.go(box, self.cfg(blind_tests={"repo": str(repo), "adapter": "unittest"}))
        doc = self.result()
        self.assertEqual((doc["red"], doc["tests"], [r["cmd"] for r in rows]),
                         ("inconclusive", [], ["blind:inconclusive"]))
        self.assertIn("3 rows but the summary says: Ran 2", rows[0]["output"])
        found = incidents.listing(self.conn)
        self.assertEqual([(i["kind"], i["task_id"], i["run_id"]) for i in found],
                         [("review_infra", self.card, self.run_id)])
        self.assertEqual(found[0]["role"], f"blind_runner:{self.card}")    # check name + card
        self.assertNotIn("Ran 2", found[0]["evidence"])     # no run text in the incident (S25)
        self.assertFalse((self.sp() / "blind").exists())

    def test_the_incident_is_per_card_not_per_problem_text(self):
        first = self.card
        other = db.new_task(self.conn, title="o", assignee="dev", branch="epic/y")
        names = ("test_greets", "test_fails", "test_third")
        for card, claimed in ((first, 2), (first, 9), (other, 2)):
            repo = blind_repo(self.root / f"{card}-{claimed}", card, THREE, names)
            record(self.conn, card, repo)
            self.card = card
            self.go(Container(self.ws, tamper=lambda t, n=claimed: t.replace(
                "Ran 3 tests", f"Ran {n} tests")),
                self.cfg(blind_tests={"repo": str(repo), "adapter": "unittest"}))
        # the same card with another problem text is the same incident; another card is a new one
        self.assertEqual(sorted(i["task_id"] for i in incidents.listing(self.conn)),
                         sorted([first, other]))

    def test_an_exit_code_that_disagrees_with_the_rows_is_inconclusive(self):
        box = Container(self.ws)
        exec_ = box.exec
        box.exec = lambda name, cmd, timeout: (0, exec_(name, cmd, timeout)[1])   # a failing run
        rows = self.go(box)                                                       # reads as OK
        self.assertEqual([r["cmd"] for r in rows], ["blind:inconclusive"])
        self.assertIn("disagree", rows[0]["output"])

    def test_a_timeout_is_inconclusive_and_the_container_is_still_removed(self):
        import subprocess as sp
        box = Container(self.ws, boom=sp.TimeoutExpired("x", 1))
        rows = self.go(box)
        self.assertEqual([r["cmd"] for r in rows], ["blind:inconclusive"])
        self.assertEqual(len(box.stopped), 1)
        self.assertFalse((self.sp() / "blind").exists())

    def test_a_crash_is_one_unavailable_row_never_an_exception(self):
        box = Container(self.ws, boom=RuntimeError("docker is gone"))
        rows = self.go(box)
        self.assertEqual([r["cmd"] for r in rows], ["blind:unavailable"])
        self.assertIn("docker is gone", rows[0]["output"])
        self.assertEqual(incidents.listing(self.conn), [])    # a card's own crash
        self.assertEqual(len(box.stopped), 1)
        self.assertFalse((self.sp() / "blind").exists())

    def test_a_failed_docker_run_is_a_masked_row_and_its_command_goes_to_the_log(self):
        import io
        from unittest import mock
        argv = ["docker", "run", "-v", "/srv/BLINDLEAK/x:/blind:ro", "img"]
        box = Container(self.ws, boom=subprocess.CalledProcessError(125, argv))
        with mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            rows = self.go(box)
        self.assertIn("<masked>:/blind:ro", rows[0]["output"])
        self.assertNotIn("BLINDLEAK", rows[0]["output"])
        self.assertEqual(log.getvalue().count("/srv/BLINDLEAK/x:/blind:ro"), 1)


class ProductPlantsInOut(RunCase):
    """/out is writable by the product: a planted link, FIFO or huge file must change nothing
    outside the spool, never block the tick, and never read as a result."""

    def plant(self, how):
        victim = self.root / "victim.txt"
        victim.write_text("keep")

        def after(ws):
            how(Path(self.box.mounts["/out"]), victim)
        self.box = Container(self.ws, after=after)
        self.victim = victim
        return self.go(self.box)

    def test_a_link_at_the_result_path_writes_nothing_outside_the_spool(self):
        rows = self.plant(lambda out, victim: (out / "blind_result.json").symlink_to(victim))
        self.assertEqual(self.victim.read_text(), "keep")
        self.assertEqual(len(rows), 2)                      # the run itself was unharmed
        self.assertTrue((self.sp() / "blind_result.json").is_file())
        self.assertFalse((self.sp() / "blind_result.json").is_symlink())

    def test_a_link_at_results_jsonl_is_not_followed(self):
        def link(out, victim):
            victim.write_text((out / "results.jsonl").read_text())     # valid rows, elsewhere
            (out / "results.jsonl").unlink()
            (out / "results.jsonl").symlink_to(victim)
        rows = self.plant(link)
        self.assertEqual([r["cmd"] for r in rows], ["blind:inconclusive"])
        self.assertIn("no usable results.jsonl", rows[0]["output"])

    def test_a_fifo_at_results_jsonl_is_inconclusive_and_does_not_block(self):
        def fifo(out, victim):
            (out / "results.jsonl").unlink()
            os.mkfifo(out / "results.jsonl")
        signal.signal(signal.SIGALRM, lambda *a: self.fail("the tick blocked on a FIFO"))
        signal.alarm(20)
        self.addCleanup(signal.alarm, 0)
        rows = self.plant(fifo)
        self.assertEqual([r["cmd"] for r in rows], ["blind:inconclusive"])

    def test_a_directory_at_results_jsonl_is_inconclusive(self):
        def folder(out, victim):
            (out / "results.jsonl").unlink()
            (out / "results.jsonl").mkdir()
        opened = len(os.listdir("/dev/fd"))
        rows = self.plant(folder)
        self.assertEqual([r["cmd"] for r in rows], ["blind:inconclusive"])
        self.assertEqual(len(os.listdir("/dev/fd")), opened)         # no descriptor leaked

    def test_an_oversize_results_file_is_inconclusive(self):
        rows = self.plant(lambda out, victim: (out / "results.jsonl").write_text(
            "x" * (blind_run.MAX_READ + 1)))
        self.assertEqual([r["cmd"] for r in rows], ["blind:inconclusive"])
        self.assertIn("no usable results.jsonl", rows[0]["output"])


class NothingToRun(RunCase):
    def test_the_mode_off_starts_nothing_and_creates_no_spool(self):
        box = Container(self.ws)
        rows = self.go(box, self.cfg(redesign={"blind_tests": "off"}))
        self.assertEqual((rows, box.started, (self.root / "spool").exists()), ([], [], False))
        self.assertEqual(config.redesign({})["blind_tests"], "off")      # the shipped default

    def test_no_redesign_block_at_all_is_off(self):
        cfg = self.cfg()
        del cfg["redesign"]
        box = Container(self.ws)
        self.assertEqual((self.go(box, cfg), box.started), ([], []))

    def test_a_card_without_blind_tests_runs_nothing(self):
        other = db.new_task(self.conn, title="other", assignee="dev", branch="epic/y")
        box = Container(self.ws)
        self.tick = StubTick(self.root, box, self.base)
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (other,)).fetchone()
        rows = rows_of(self.tick, self.cfg(), "cd" * 16, self.ws, self.conn, task)
        self.assertEqual((rows, box.started), ([], []))

    def test_a_missing_blind_repo_is_one_unavailable_row_and_no_container(self):
        shutil.rmtree(self.repo)
        box = Container(self.ws)
        rows = self.go(box)
        self.assertEqual([(r["cmd"], r["exit"], r["blind"]) for r in rows],
                         [("blind:unavailable", 2, "report")])
        self.assertIn("does not exist", rows[0]["output"])
        self.assertEqual(box.started, [])
        self.assertEqual([i["kind"] for i in incidents.listing(self.conn)], ["silent_failure"])

    def commit_toml(self, text):
        (self.repo / "blind.toml").write_text(text)
        sh("git", "-C", str(self.repo), "add", "-A")
        sh("git", "-C", str(self.repo), *workspace.IDENTITY, "commit", "-q", "-m", "toml")

    def test_a_stale_run_line_in_the_working_copy_or_at_head_starts_no_container(self):
        safe = (self.repo / "blind.toml").read_text()
        stale = safe.replace("exec ", "")
        for where in ("working copy", "HEAD"):
            with self.subTest(where=where):
                if where == "HEAD":
                    self.commit_toml(stale)
                (self.repo / "blind.toml").write_text(stale if where == "working copy" else safe)
                box = Container(self.ws)
                rows = self.go(box)
                self.assertEqual([(r["cmd"], r["exit"]) for r in rows], [("blind:unavailable", 2)])
                self.assertIn("is not the safe form", rows[0]["output"])
                self.assertNotIn("the blind run failed", rows[0]["output"])
                self.assertEqual(box.started, [])
                self.assertEqual(len(incidents.listing(self.conn)), 1)   # one per board and reason

    def test_a_commit_holding_a_stale_or_hostile_line_still_runs_exactly_the_safe_line(self):
        """The run never reads the line from the file: only the adapter picks it (HEAD is fixed)."""
        safe = (self.repo / "blind.toml").read_text()
        want = cli.RUN_LINE.format(card=self.card)
        for line in (cli.RUN_LINE.replace("exec ", ""), "touch /tmp/pwned; true",
                     f"sh -c '{cli.RUN_LINE}'", "FOO=1 " + cli.RUN_LINE):
            with self.subTest(line=line):
                self.commit_toml(safe.replace(json.dumps(cli.RUN_LINE), json.dumps(line)))
                recorded = sh("git", "-C", str(self.repo), "rev-parse", "HEAD").strip()
                self.commit_toml(safe)
                self.assertEqual(blind_run._run_line(self.repo, recorded, self.card), want)

    def test_a_commit_naming_an_adapter_with_no_runner_is_refused_whatever_its_line(self):
        safe = (self.repo / "blind.toml").read_text()
        for adapter in ('"pytest"', '"node-test"', '"nonsense"', '["unittest"]', "5"):
            with self.subTest(adapter=adapter):
                self.commit_toml(safe.replace('adapter = "unittest"', f"adapter = {adapter}"))
                recorded = sh("git", "-C", str(self.repo), "rev-parse", "HEAD").strip()
                with self.assertRaisesRegex(ValueError, "no blind runner"):
                    blind_run._run_line(self.repo, recorded, self.card)
                self.commit_toml(safe)

    def test_a_board_without_a_blind_block_is_unavailable_too(self):
        cfg = self.cfg()
        del cfg["blind_tests"]
        rows = self.go(Container(self.ws), cfg)
        self.assertEqual([r["cmd"] for r in rows], ["blind:unavailable"])


class Refusing(Container):
    """A runner that refuses to start the container, the way docker.py does."""

    def __init__(self, ws, error):
        super().__init__(ws)
        self.error = error

    def start(self, run_id, ws, **kw):
        err = self.error
        if isinstance(err, subprocess.CalledProcessError) and isinstance(err.stderr, str):
            text = err.stderr.replace("@CHECKOUT@", ws).replace("@GITDIR@", kw["git_dir"])
            err = subprocess.CalledProcessError(err.returncode, err.cmd, err.output, text)
        raise err


class BoardWideUnavailable(RunCase):
    """A reason that holds for the whole board opens one incident and one alert, however many
    cards it hits (`capsule/blind/unavailable.py`)."""

    def setUp(self):
        super().setUp()
        self.other = db.new_task(self.conn, title="other", assignee="dev", branch="epic/y")

    def hit(self, card=None, cfg=None, box=None):
        self.card = card or self.card
        return self.go(box or Container(self.ws), cfg)

    def missing_repo(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def alerts(self):
        return [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE channel != 'local'"
            " AND body LIKE '%blind tests unavailable%'")]

    def test_the_first_card_opens_one_incident_naming_reason_and_card_and_one_alert(self):
        self.missing_repo()
        self.hit()
        inc, = incidents.listing(self.conn)
        self.assertEqual((inc["kind"], inc["board"], inc["task_id"], inc["status"]),
                         ("silent_failure", BOARD, self.card, "open"))
        self.assertIn("repo_missing", inc["root_key"])
        self.assertEqual(json.loads(inc["evidence"]),
                         ["reason: repo_missing", f"card: {self.card}"])
        alert, = self.alerts()
        self.assertIn("repo_missing", alert)
        self.assertIn(self.card, alert)

    def test_more_cards_with_the_same_reason_add_evidence_and_send_nothing(self):
        self.missing_repo()
        for card in (self.card, self.other, self.other, self.card):
            self.hit(card)
        inc, = incidents.listing(self.conn)
        cards = [e for e in json.loads(inc["evidence"]) if e.startswith("card: ")]
        self.assertEqual(cards, [f"card: {self.card}", f"card: {self.other}"])
        self.assertEqual(len(self.alerts()), 1)

    def test_a_later_day_hit_while_the_incident_is_open_still_opens_nothing(self):
        self.missing_repo()
        self.hit()
        self.conn.execute("UPDATE incidents SET opened_at = opened_at - ?", (3 * 86400,))
        self.hit(self.other)
        self.assertEqual(len(incidents.listing(self.conn)), 1)
        self.assertEqual(len(self.alerts()), 1)

    def test_a_different_board_wide_reason_opens_its_own_incident(self):
        self.missing_repo()
        self.hit()
        cfg = self.cfg()
        del cfg["blind_tests"]
        self.hit(self.other, cfg)
        incs = incidents.listing(self.conn)
        self.assertEqual(len(incs), 2)
        self.assertEqual(sorted(("no_block" in i["root_key"], "repo_missing" in i["root_key"])
                                for i in incs), [(False, True), (True, False)])
        self.assertEqual(len(self.alerts()), 2)

    def test_a_missing_image_and_a_blind_mount_refusal_are_board_wide_reasons(self):
        run = ["docker", "run", "-d", "img", "sleep", "infinity"]
        image = subprocess.CalledProcessError(125, run, "", "Unable to find image")
        hung = subprocess.TimeoutExpired(run, 300)
        refusal = RuntimeError(f"{docker.BLIND_CHECK}: {self.root}/blind-spool [run capsule-ab12]")
        for error, reason in ((image, "runner_start_failed"), (refusal, "blind_mount_refused"),
                              (hung, "runner_start_timeout")):
            with self.subTest(reason=reason):
                rows = self.hit(box=Refusing(self.ws, error))
                self.assertEqual([r["cmd"] for r in rows], ["blind:unavailable"])
                keys = [i["root_key"] for i in incidents.listing(self.conn)]
                self.assertEqual(sum(reason in k for k in keys), 1)
        self.assertEqual(len(self.alerts()), 3)

    def test_the_runners_own_mount_probe_failure_is_one_cards_not_the_boards(self):
        probe = subprocess.CalledProcessError(1, docker._PROBE_CMD, "gone", "bind mounts missing")
        self.hit(box=Refusing(self.ws, probe))
        self.assertEqual((incidents.listing(self.conn), self.alerts()), ([], []))

    def test_a_docker_run_refusal_is_the_boards_unless_it_names_only_the_cards_own_paths(self):
        run = ["docker", "run", "-d", "img"]
        said = (("docker: Error response: No such image: img", True),
                ("pull access denied for img", True), ("manifest unknown", True),
                (b"Cannot connect to the Docker daemon at unix:///x", True),
                ("Is the docker daemon running?", True),
                (f"invalid mount config for {self.repo} {self.root}/spool/x/blind", True),
                ("invalid mount config: no such mount source", True),
                (f"invalid mount config: bind source path does not exist: {self.ws}", False),
                ("invalid mount config: bind source path does not exist: @CHECKOUT@", False),
                ("invalid mount config: bind source path does not exist: @GITDIR@", False),
                ("bind source @CHECKOUT@ and @GITDIR@", False),
                (f"bind source @CHECKOUT@ and {self.repo}", True),
                (f"invalid mount config for {self.root}/spool/{'ab' * 16}/blind", True),
                (f"bind source {self.ws} and {self.root}/spool/{'ab' * 16}/blind", True),
                (f"bind source {self.ws} and {self.repo}", True),
                (f"bind source {str(self.ws).upper()}/x and {self.repo}".upper(), True),
                ("docker: some unrelated refusal", True))
        for text, board_wide in said:
            with self.subTest(text=text):
                self.conn.execute("DELETE FROM incidents")
                error = subprocess.CalledProcessError(125, run, "", text)
                rows = self.hit(box=Refusing(self.ws, error))
                self.assertEqual([r["cmd"] for r in rows], ["blind:unavailable"])
                found = incidents.listing(self.conn)
                self.assertEqual(len(found), int(board_wide))
                self.assertNotIn(str(self.repo), json.dumps(found))     # no path in the incident

    def test_a_refusal_naming_the_clone_and_the_resolved_blind_mount_is_the_boards(self):
        mount = self.root / "link" / "blind"
        (self.root / "real").mkdir()
        os.symlink(self.root / "real", self.root / "link")
        text = f"bind source {self.ws} and {os.path.realpath(self.root)}/real/blind".upper()
        exc = subprocess.CalledProcessError(125, ["docker", "run"], "", text)
        cfg = self.cfg()
        self.assertEqual(unavailable._reason((), exc, cfg, (self.ws,), (mount,)),
                         "runner_start_failed")
        only = subprocess.CalledProcessError(125, ["docker", "run"], "", f"bind {self.ws}")
        self.assertIsNone(unavailable._reason((), only, cfg, (self.ws,), (mount,)))

    def test_a_classification_that_raises_keeps_the_row_and_opens_nothing(self):
        odd = subprocess.TimeoutExpired(5, 300)             # a cmd that is not iterable
        rows = self.hit(box=Refusing(self.ws, odd))
        self.assertEqual([r["cmd"] for r in rows], ["blind:unavailable"])
        self.assertEqual((incidents.listing(self.conn), self.alerts()), ([], []))

    def test_the_reason_of_a_preflight_problem_or_an_error(self):
        def reason(problems, exc):
            return unavailable._reason(problems, exc, self.cfg(), ())

        self.assertEqual(reason(["blind_tests.tester_may_read: must be a list"], None), "config")
        self.assertEqual(reason(["tester_may_read 'src/x': names production code"], None),
                         "tester_may_read")
        self.assertEqual(reason(["the blind repo /x does not exist"], None), "repo_missing")
        self.assertIsNone(reason((), ValueError("x")))
        self.assertIsNone(reason((), None))

    def test_a_git_timeout_is_not_a_docker_run_timeout(self):
        self.hit(box=Refusing(self.ws, subprocess.TimeoutExpired(["git", "ls-tree"], 5)))
        self.assertEqual((incidents.listing(self.conn), self.alerts()), ([], []))

    def test_a_config_error_is_config_and_a_refused_glob_is_tester_may_read(self):
        for block, reason in (({"repo": str(self.repo), "adapter": "unittest",
                                "tester_may_read": "docs"}, "config"),
                              ({"repo": str(self.repo), "adapter": "unittest",
                                "tester_may_read": ["src/x.py"]}, "tester_may_read")):
            with self.subTest(reason=reason):
                self.hit(cfg=self.cfg(blind_tests=block))
                keys = [i["root_key"] for i in incidents.listing(self.conn)]
                self.assertTrue(any(f"blind unavailable: {reason}" in k for k in keys), keys)

    def test_the_alert_threads_under_the_board_not_the_first_card(self):
        self.missing_repo()
        self.hit()
        keys = [r[0] for r in self.conn.execute(
            "SELECT thread_key FROM outbox WHERE channel != 'local'"
            " AND body LIKE '%blind tests unavailable%'")]
        self.assertEqual(keys, [BOARD])

    def test_a_card_specific_reason_opens_no_incident(self):
        rows = self.hit(box=Container(self.ws, boom=RuntimeError("one card's snapshot broke")))
        self.assertEqual([r["cmd"] for r in rows], ["blind:unavailable"])
        self.assertEqual((incidents.listing(self.conn), self.alerts()), ([], []))

    def test_a_repeat_after_a_close_is_the_incident_modules_page_and_the_only_alert(self):
        self.missing_repo()
        self.hit()
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=?", (int(time.time()),))
        before = len(self.alerts())
        local = self.conn.execute("SELECT COUNT(*) FROM outbox WHERE channel='local'").fetchone()[0]
        self.hit(self.other)
        (now_local,) = self.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE channel='local'").fetchone()
        self.assertEqual(now_local, local + 1)               # one Mac page
        again = [i for i in incidents.listing(self.conn) if i["status"] == "open"]
        self.assertEqual([i["kind"] for i in again], ["repeat"])
        self.assertEqual(len(self.alerts()), before)        # no second line of our own
        pages = [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE channel != 'local' AND body LIKE '%REPEAT incident%'")]
        self.assertEqual(len(pages), 1)

    def test_the_mode_off_and_a_paused_board_open_nothing(self):
        self.missing_repo()
        self.hit(cfg=self.cfg(redesign={"blind_tests": "off"}))
        self.assertEqual(incidents.listing(self.conn), [])
        db.guard_set(self.conn, "pause:board", {"until": time.time() + 3600})
        rows = self.hit()
        self.assertEqual([r["cmd"] for r in rows], ["blind:unavailable"])   # the row stays
        self.assertEqual((incidents.listing(self.conn), self.alerts()), ([], []))

    def test_no_incident_or_alert_holds_a_blind_path(self):
        refusal = RuntimeError(f"{docker.BLIND_CHECK}: {self.root}/blind-spool [run capsule-ab12]")
        self.hit(box=Refusing(self.ws, refusal))
        self.missing_repo()
        self.hit(self.other)
        text = json.dumps([dict(i) for i in incidents.listing(self.conn)]) + "".join(
            r[0] for r in self.conn.execute("SELECT body FROM outbox"))
        for secret in (str(self.root), str(self.repo), "blind-spool"):
            self.assertNotIn(secret, text)
        self.assertIn("path-", text)            # the refusal stays as its digest only

    def test_the_incident_and_the_alert_are_one_transaction(self):
        self.missing_repo()
        real = unavailable.outbox.notify
        unavailable.outbox.notify = lambda *a, **k: (_ for _ in ()).throw(OSError("outbox down"))
        self.addCleanup(setattr, unavailable.outbox, "notify", real)
        rows = self.hit()
        self.assertEqual([r["cmd"] for r in rows], ["blind:unavailable"])   # a card still moves
        self.assertEqual(incidents.listing(self.conn), [])      # no incident without its alert


if __name__ == "__main__":
    unittest.main()
