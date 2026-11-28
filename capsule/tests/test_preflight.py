"""Pilot prep: `python3 -m capsule check` is read-only and fails loud on a missing precondition."""
import fcntl
import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import db as capsule_db
from capsule import config, paths, preflight, tick_guard
from capsule.runners import docker
from capsule.tests import wal_oracle


_EXCLUSIVE_WRITER = """
import sys
sys.path.insert(0, sys.argv[2])
from capsule import db
conn = db.connect(sys.argv[1])
conn.execute("PRAGMA locking_mode=EXCLUSIVE")
db.new_task(conn, title="held", status="ready")      # one commit, then hold the lock
print("committed", flush=True)
import time
time.sleep(60)
"""


def ok_run(argv, **kw):
    if argv[0] == "ps":
        return subprocess.CompletedProcess(argv, 0, "  PID  PPID ARGS\n", "")   # no live ticks, by default
    return subprocess.CompletedProcess(argv, 0, "28.0\n", "")


def ps_seeing(pid, board):
    """`run_cmd` whose `ps` reports one process, `pid`, running a real tick invocation - the
    argv oracle's half of an "agrees with the lock" row (row 517)."""
    legacy = "786 1 python3 -m capsule tick --board elsewhere"  # another board's tick
    row = f"{pid} 1 python3 -m capsule tick --board {board}\n{legacy}"

    def run(argv, **kw):
        if argv[0] == "ps":
            return subprocess.CompletedProcess(argv, 0, f"  PID  PPID ARGS\n{row}\n", "")
        return subprocess.CompletedProcess(argv, 0, "28.0\n", "")
    return run


class Preflight(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        self.cfg["repo_url"] = str(self.home)
        env = mock.patch.dict("os.environ", {"LEGACY_KANBAN_HOME": tempfile.mkdtemp(),
                                         "CAPSULE_HOME": str(self.home)})  # C06
        env.start()
        self.addCleanup(env.stop)

    def write(self):
        (self.home / "config.json").write_text(json.dumps(self.cfg))

    def hold_lock(self, pid, board=None):
        """A real `fcntl.flock` on this board's lock file, holder `pid` - the lock oracle's half
        of an "agrees with the argv scan" row (row 517); closed at test teardown."""
        path = tick_guard.lock_path(self.home, board or self.cfg["board"])
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        f.truncate()
        f.write(str(pid))
        f.flush()
        self.addCleanup(f.close)
        return path

    def levels(self, **kw):
        with mock.patch.dict("os.environ", {"OPS_WORKER_IMAGE": "img:1"}), \
                mock.patch.object(Path, "is_dir", lambda self: True):
            return {name: level for level, name, _ in preflight.run(self.home, **kw)}

    def test_a_missing_config_is_the_only_line(self):
        rows = preflight.run(self.home, run_cmd=ok_run, which=lambda n: "/bin/claude")
        want = [("FAIL", "tick"), ("ok", "host"), ("FAIL", "config")]     # host.json row, P05a
        self.assertEqual([(r[0], r[1]) for r in rows], want)

    def test_all_green_with_the_board_not_yet_taken(self):
        self.write()
        self.hold_lock(99999)
        ps = ps_seeing(99999, self.cfg["board"])
        levels = self.levels(run_cmd=ps, which=lambda n: "/bin/claude")
        self.assertEqual(levels["board_db"], "info")
        self.assertEqual(levels["tick"], "ok")
        self.assertEqual({k: v for k, v in levels.items() if v == "FAIL"}, {})

    def test_missing_image_and_claude_fail(self):
        self.write()

        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1 if "inspect" in argv else 0, "", "")
        levels = self.levels(run_cmd=run, which=lambda n: None)
        self.assertEqual((levels["worker_image"], levels["claude"]), ("FAIL", "FAIL"))

    def image_row(self, record=None, have=True, pin=None, home=None):
        """The `worker_image` row, with `record` as the home's images.lock (None: no file);
        `have`: what `docker image inspect` answers."""
        self.write()
        if record is not None:
            (self.home / "images.lock").write_bytes(record)
        inspected = []

        def run(argv, **kw):
            inspected.append(argv)
            rc = 0 if have or "inspect" not in argv else 1
            return subprocess.CompletedProcess(argv, rc, "", "")
        world = {"OPS_ENV_FILE": str(self.home / "no-ops.env")}
        with mock.patch.dict("os.environ", world):
            os.environ.pop("OPS_WORKER_IMAGE", None)
            if pin:
                os.environ["OPS_WORKER_IMAGE"] = pin
            rows = preflight.run(home or self.home, run_cmd=run, which=lambda n: "/bin/claude")
        return next(r for r in rows if r[1] == "worker_image"), inspected

    ROW = b"capsule-python@sha256:" + b"ab" * 32

    def test_a_board_runs_the_recorded_image(self):
        row, inspected = self.image_row(self.ROW + b"\n")
        self.assertEqual(row, ("ok", "worker_image", self.ROW.decode()))
        self.assertIn(["image", "inspect", self.ROW.decode()], [a[-3:] for a in inspected])

    def test_a_board_with_no_image_yet_fails_and_names_the_build_command(self):
        for record, have in ((None, True), (b"", True), (b"capsule-python:local\n", True),
                             (self.ROW + b"\n", False)):
            with self.subTest(record=record, have=have), mock.patch("sys.stderr"):
                row, inspected = self.image_row(record, have)
                self.assertEqual(row[:2], ("FAIL", "worker_image"))
                self.assertIn("docker/build-base.sh base && docker/build-base.sh python", row[2])
                self.assertIn("40 GB", row[2])
                # nothing recorded: docker is not even asked about an image
                self.assertEqual(any("inspect" in a for a in inspected), record == self.ROW + b"\n")

    def test_an_operator_pin_is_said_out_loud(self):
        for have in (True, False):
            with self.subTest(have=have):
                row, _ = self.image_row(self.ROW + b"\n", have, pin="op:1")
                self.assertEqual(row[0], "ok" if have else "FAIL")
                self.assertTrue(row[2].startswith("op:1"), row)
                self.assertIn("OPS_WORKER_IMAGE override is active", row[2])
                self.assertNotIn("build-base", row[2])
        row, _ = self.image_row(pin="op:1")      # no record at all: the pin still wins
        self.assertEqual(row[:2], ("ok", "worker_image"))
        self.assertTrue(row[2].startswith("op:1"), row)

    def test_check_reads_the_record_of_the_home_it_is_given_not_the_environments(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        (other / "images.lock").write_bytes(self.ROW + b"\n")
        (other / "config.json").write_text(json.dumps(self.cfg))
        row, _ = self.image_row(home=other)   # the env home is empty
        self.assertEqual(row, ("ok", "worker_image", self.ROW.decode()))

    def fallback_rows(self, record, board, pin=None, toml=None, ops_pin=None):
        self.cfg["board"] = board
        self.write()
        (self.home / "images.lock").write_bytes(record)
        if toml is not None:
            (self.home / "capsule.toml").write_text(toml)   # the repo_url dir is the home
        world = {"OPS_ENV_FILE": str(self.home / "no-ops.env")}
        if ops_pin:
            (self.home / "ops.env").write_text(f"OPS_WORKER_IMAGE={ops_pin}\n")
            world["OPS_ENV_FILE"] = str(self.home / "ops.env")
        with mock.patch.dict("os.environ", world):
            os.environ.pop("OPS_WORKER_IMAGE", None)
            if pin:
                os.environ["OPS_WORKER_IMAGE"] = pin
            rows = preflight.run(self.home, run_cmd=ok_run, which=lambda n: "/bin/claude")
        return [r for r in rows if r[1] == "worker_image_fallback"]

    def test_a_board_on_the_generic_image_is_warned_once(self):
        rows = self.fallback_rows(self.ROW + b"\n", "demo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "info")
        self.assertIn("board demo runs the generic python image", rows[0][2])
        self.assertIn("image build --project DIR --board demo", rows[0][2])

    def test_a_non_python_or_missing_image_table_is_warned(self):
        for toml in ('[image]\nstacks = ["web"]\n', "[other]\n", "not toml ["):
            with self.subTest(toml=toml):
                self.assertEqual(len(self.fallback_rows(self.ROW + b"\n", "demo", toml=toml)), 1)

    def test_a_git_url_repo_is_warned_as_unchecked_naming_the_source(self):
        self.cfg["repo_url"] = "https://example.invalid/r.git"
        rows = self.fallback_rows(self.ROW + b"\n", "demo")
        self.assertEqual(len(rows), 1)
        self.assertIn("board demo runs the generic python image", rows[0][2])
        self.assertIn("repo is not a local directory, cannot check capsule.toml", rows[0][2])
        self.assertIn("image build --project DIR --board demo", rows[0][2])
        rows = self.fallback_rows(self.ROW + b"\n", "demo", ops_pin="op:1")
        self.assertIn("runs the machine-wide pinned image", rows[0][2])
        self.assertIn("cannot check capsule.toml", rows[0][2])
        self.assertEqual(self.fallback_rows(self.ROW + b"\n", "demo", pin="op:1"), [])

    def test_no_fallback_warning_when_intended_or_not_in_effect(self):
        proj = b"capsule-proj-demo@sha256:" + b"cd" * 32 + b"\n"
        self.assertEqual(self.fallback_rows(self.ROW + b"\n" + proj, "demo"), [])
        py = '[image]\nstacks = ["python"]\n'
        self.assertEqual(self.fallback_rows(self.ROW + b"\n", "demo", toml=py), [])
        self.cfg["repo_url"] = str(self.home)
        self.assertEqual(self.fallback_rows(self.ROW + b"\n", "demo", pin="op:1"), [])   # env pin
        self.assertEqual(self.fallback_rows(b"", "demo"), [])   # no image: worker_image FAILs

    def test_a_board_on_the_ops_env_pin_is_warned_naming_the_pinned_image(self):
        rows = self.fallback_rows(self.ROW + b"\n", "demo", ops_pin="op:1")
        self.assertEqual(len(rows), 1)
        self.assertIn("board demo runs the machine-wide pinned image", rows[0][2])
        self.assertNotIn("generic python image", rows[0][2])
        py = '[image]\nstacks = ["python"]\n'
        self.assertEqual(self.fallback_rows(self.ROW + b"\n", "demo", ops_pin="op:1", toml=py), [])
        proj = b"capsule-proj-demo@sha256:" + b"cd" * 32 + b"\n"   # the project row beats the pin
        self.assertEqual(self.fallback_rows(self.ROW + b"\n" + proj, "demo", ops_pin="op:1"), [])

    def test_a_missing_local_repo_fails(self):
        self.cfg["repo_url"] = str(self.home / "nope")
        self.write()
        self.assertEqual(self.levels(run_cmd=ok_run, which=lambda n: "/c")["repo_url"], "FAIL")

    def test_no_tick_running_fails_the_tick_row_and_the_exit_code(self):
        """Row 517: the tick row's FAIL exits `capsule check` non-zero on its own, everything
        else green (Z2 second oracle: `main`'s exit code observes the same fact `run`'s rows do,
        through a different code path)."""
        self.write()
        with mock.patch.dict("os.environ", {"OPS_WORKER_IMAGE": "img:1"}), \
                mock.patch.object(Path, "is_dir", lambda self: True):
            rows = preflight.run(self.home, run_cmd=ok_run, which=lambda n: "/bin/claude")
        self.assertEqual({name: level for level, name, _ in rows}["tick"], "FAIL")
        self.assertEqual(1 if any(level == "FAIL" for level, _, _ in rows) else 0, 1)

    def test_lock_held_but_ps_unavailable_fails_the_tick_row(self):
        """I1: `ps` failing means "unknown", never "no tick running" - the argv oracle must not
        let a broken `ps` silently agree with a real held lock going unreported."""
        self.write()
        self.hold_lock(99999)

        def broken_ps(argv, **kw):
            raise OSError("ps: not found")
        self.assertEqual(self.levels(run_cmd=broken_ps, which=lambda n: "/bin/claude")["tick"], "FAIL")

    def test_the_board_kwarg_resolves_the_per_board_config(self):
        """`run(home, board="other")` must read `boards/other/config.json`, not the legacy
        `config.json` (which is left unwritten here on purpose)."""
        self.cfg["board"] = "other"
        p = paths.config_path(self.home, "other")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.cfg))
        self.hold_lock(99999, board="other")
        ps = ps_seeing(99999, "other")
        levels = {name: (level, detail) for level, name, detail
                 in preflight.run(self.home, board="other", run_cmd=ps,
                                  which=lambda n: "/bin/claude")}
        self.assertEqual(levels["config"][0], "ok")
        self.assertIn(str(p), levels["config"][1])
        self.assertEqual(levels["tick"][0], "ok")

    def test_a_board_that_does_not_exist_is_reported_not_replaced_by_the_legacy_config(self):
        self.write()                                   # the legacy config.json is there and valid
        rows = preflight.run(self.home, board="typo", run_cmd=ps_seeing(99999, "typo"),
                             which=lambda n: "/bin/claude")
        (config,) = [r for r in rows if r[1] == "config"]
        self.assertEqual(config[0], "FAIL")
        self.assertIn("no such board 'typo'", config[2])
        self.assertIn(str(paths.config_path(self.home, "typo")), config[2])
        self.assertNotIn(str(self.home / "config.json"), config[2])

    def test_a_legacy_board_with_no_per_board_config_is_served_from_the_legacy_config(self):
        """`boards/my-app/kanban.db` and a legacy config.json whose `board` is my-app, no
        boards/my-app/config.json: `ok config` (maintenance/restore.sh reads that row)."""
        self.cfg["board"] = "my-app"
        self.write()
        (self.home / "boards" / "my-app").mkdir(parents=True)
        (self.home / "boards" / "my-app" / "kanban.db").touch()
        rows = preflight.run(self.home, board="my-app", run_cmd=ps_seeing(99999, "my-app"),
                             which=lambda n: "/bin/claude")
        (config,) = [r for r in rows if r[1] == "config"]
        self.assertEqual(config[0], "ok")
        self.assertIn(f"{self.home / 'config.json'}: board my-app,", config[2])
        self.assertGreater(len(rows), 10)                # the whole check ran, not three rows

    def test_a_board_folder_does_not_vouch_for_a_legacy_config_naming_another(self):
        self.cfg["board"] = "my-app"
        self.write()
        (self.home / "boards" / "other").mkdir(parents=True)
        (self.home / "boards" / "other" / "kanban.db").touch()
        (config,) = [r for r in preflight.run(self.home, board="other", run_cmd=ok_run,
                                              which=lambda n: "/bin/claude") if r[1] == "config"]
        self.assertEqual(config[0], "FAIL")

    def test_an_unreadable_legacy_config_is_no_claim_on_the_board(self):
        (self.home / "config.json").write_text("not json")
        (config,) = [r for r in preflight.run(self.home, board="x", run_cmd=ok_run,
                                              which=lambda n: "/bin/claude") if r[1] == "config"]
        self.assertEqual(config[0], "FAIL")
        self.assertIn("no such board 'x'", config[2])

    def intent_row(self, intent_dir):
        """The (level, detail) of `preflight.run`'s intent row with paths.INTENT pointed at `intent_dir`."""
        self.write()
        with mock.patch.object(paths, "INTENT", intent_dir), \
                mock.patch.dict("os.environ", {"OPS_WORKER_IMAGE": "img:1"}), \
                mock.patch.object(Path, "is_dir", lambda self: True):
            (row,) = [r for r in preflight.run(self.home, run_cmd=ok_run, which=lambda n: "/c") if r[1] == "intent"]
        return row[0], row[2]

    def copy_intent(self):
        d = Path(tempfile.mkdtemp()) / "intent"
        shutil.copytree(paths.INTENT, d)
        return d

    def test_the_intent_row_is_ok_on_the_shipped_examples(self):
        self.assertEqual(self.intent_row(self.copy_intent()), ("ok", "SHA256SUMS match, lint clean"))

    def test_a_tampered_intent_file_fails_the_intent_row(self):
        """Test-honesty audit item 4: `preflight.run` itself must report a hash mismatch, not only `_sums_ok`."""
        d = self.copy_intent()
        with (d / "guide.jsonl").open("a") as f:
            f.write("\n")
        level, detail = self.intent_row(d)
        self.assertEqual(level, "FAIL")
        self.assertIn("'guide.jsonl'", detail)

    def test_an_intent_file_failing_lint_fails_the_intent_row(self):
        """A re-pinned file whose example no longer lints still fails: the row runs `intent.lint_all` too."""
        d = self.copy_intent()
        (d / "guide.jsonl").write_text("{\"id\": \"x\"}\n")
        sums = [l for l in (d / "SHA256SUMS").read_text().splitlines() if not l.endswith("guide.jsonl")]
        sums.append(hashlib.sha256((d / "guide.jsonl").read_bytes()).hexdigest() + "  guide.jsonl")
        (d / "SHA256SUMS").write_text("\n".join(sums) + "\n")
        level, detail = self.intent_row(d)
        self.assertEqual(level, "FAIL")
        self.assertIn("guide.jsonl", detail)
        self.assertNotIn("mismatch", detail)


class WalHealthRow(unittest.TestCase):
    """row 466 (review C1): `capsule check`'s `wal_health` row is the real `mode=ro` open
    itself (`db.connect_ro_tolerant`, via `preflight.run`) - never a prediction of what
    sqlite would do. Fixtures come from `wal_oracle.make`, a genuinely separate writer
    process (I2): a same-process writer/reader pair would silently share sqlite's own
    `-shm` mapping and hide the very failures a real reader hits."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        self.cfg["repo_url"] = str(self.home)
        (self.home / "config.json").write_text(json.dumps(self.cfg))
        env = mock.patch.dict("os.environ", {"LEGACY_KANBAN_HOME": tempfile.mkdtemp(),
                                         "CAPSULE_HOME": str(self.home)})  # C06
        env.start()
        self.addCleanup(env.stop)
        self.board_dir = paths.board_db(self.cfg["board"], self.home).parent

    def levels(self):
        with mock.patch.dict("os.environ", {"OPS_WORKER_IMAGE": "img:1"}), \
                mock.patch.object(Path, "is_dir", lambda self: True):
            return {name: (level, detail) for level, name, detail
                    in preflight.run(self.home, run_cmd=ok_run, which=lambda n: "/bin/claude")}

    def test_healthy_wal_state_passes(self):
        wal_oracle.make(self.board_dir, frames=True)
        levels = self.levels()
        self.assertEqual(levels["wal_health"][0], "ok")
        self.assertNotEqual(levels["migrations"][0], "FAIL")

    def test_idle_board_and_dir_not_writable_passes_via_the_immutable_fallback(self):
        """The commonest real state (row 466 review): a clean close leaves both sidecars
        gone. `capsule check` must read it, not crash on it - this was the bug fix round 1
        missed (`wal_cantopen_risk` predicted "always safe" here, but the plain `mode=ro`
        open genuinely raises; only the `immutable=1` fallback actually succeeds, and it's
        exact here since there is no uncheckpointed WAL for it to hide). Root ignores
        directory write bits (maintenance/tests/test_release_gate.py:276 precedent) - a
        plain `self.skipTest` in the body, not `@skipIf`, keeps the mutation gate's static
        flags from reading a legitimate root-skip as a red flag on a new test."""
        if os.geteuid() == 0:
            self.skipTest("root ignores directory write bits")
        wal_oracle.make(self.board_dir, frames=False)
        self.board_dir.chmod(0o555)
        self.addCleanup(self.board_dir.chmod, 0o755)
        levels = self.levels()
        self.assertEqual(levels["wal_health"][0], "ok")
        self.assertNotEqual(levels["migrations"][0], "FAIL")

    def test_frames_no_shm_and_dir_not_writable_fails_without_crashing(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory write bits")
        wal_oracle.make(self.board_dir, frames=True)
        (self.board_dir / "kanban.db-shm").unlink()
        self.board_dir.chmod(0o555)
        self.addCleanup(self.board_dir.chmod, 0o755)
        levels = self.levels()   # must not raise sqlite3.OperationalError itself (row 466)
        self.assertEqual(levels["wal_health"][0], "FAIL")
        self.assertIn("-shm", levels["wal_health"][1])
        self.assertEqual(levels["migrations"][0], "FAIL")
        self.assertIn("skipped", levels["migrations"][1])


class ConnectRoTolerant(unittest.TestCase):
    """row 466 fix round 2 (review "Required", row 459): the write-refusal and fail-fast
    guards row 470 I7/I8 relied on (`unblocked._connect_ro`/`_immutable_fallback_safe`,
    deleted in round 1's move) were lost with them - restored here, against the function's
    new home, with real `wal_oracle` fixtures. Lives beside `WalHealthRow` (not in
    test_db.py) for the same reason `WalCantopenRisk` did in round 1: every one of these
    tests is explained by the same one pure-addition symbol, `connect_ro_tolerant` - the
    mutation gate's revert-mutation check needs an independent failure shape in the same
    file to trust that explanation, and `WalHealthRow`'s `KeyError` (a reverted
    `preflight.py` has no `wal_health` row at all) supplies it."""

    # A complete row (every NOT NULL column set): only the connection's own mode may refuse it.
    VALID_INSERT = ("INSERT INTO tasks (id, title, status, created_at, created_by) "
                    "VALUES ('t_ro', 'x', 'ready', 1, 'capsule')")

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_refuses_a_write_on_the_plain_path(self):
        """I8: the plain `mode=ro` connection (a healthy db, dir writable - no fallback
        needed) must itself refuse a write, not merely go unwritten by this code's own
        callers."""
        path = wal_oracle.make(self.dir, frames=True)
        conn = capsule_db.connect_ro_tolerant(path)
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            conn.execute(self.VALID_INSERT)

    def test_refuses_a_write_on_the_immutable_fallback_path(self):
        """I8, the other half: the `immutable=1` fallback connection (idle board, dir not
        writable) must refuse a write too."""
        if os.geteuid() == 0:
            self.skipTest("root ignores directory write bits")
        path = wal_oracle.make(self.dir, frames=False)
        self.dir.chmod(0o555)
        self.addCleanup(self.dir.chmod, 0o755)
        conn = capsule_db.connect_ro_tolerant(path)
        self.addCleanup(conn.close)
        # t_ad6b9ffe m2: the 555 dir would refuse the write by itself, hiding a read-write
        # fallback - reopen the dir so only the connection's own mode can refuse the INSERT.
        self.dir.chmod(0o755)
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            conn.execute(self.VALID_INSERT)

    def test_empty_wal_no_shm_dir_not_writable_opens_via_the_fallback_and_reads(self):
        """I7 `ImmutableFallback`'s missing case: `-wal` present but truncated to 0 bytes
        (not absent) is exactly as safe for the fallback as no `-wal` at all - and the
        fallback must still return real, committed data, not merely open without error."""
        if os.geteuid() == 0:
            self.skipTest("root ignores directory write bits")
        path = wal_oracle.make_empty_wal(self.dir)
        (Path(str(path) + "-shm")).unlink()
        self.dir.chmod(0o555)
        self.addCleanup(self.dir.chmod, 0o755)
        conn = capsule_db.connect_ro_tolerant(path)
        self.addCleanup(conn.close)
        self.assertEqual([r[0] for r in conn.execute("SELECT title FROM tasks")], ["committed"])

    def test_a_board_locked_by_another_process_raises_within_about_three_seconds(self):
        """I7 / t_ad6b9ffe m1, behavioural: a writer PROCESS in `locking_mode=EXCLUSIVE` commits
        once (so `-wal` holds real frames and the `immutable=1` fallback is refused) and then
        sleeps holding the lock; `connect_ro_tolerant` must give up after its 1 s timeout, not
        wait out a 5 s / 120 s one. (A plain WAL writer never blocks a reader, hence EXCLUSIVE.)
        Same lock shape as test_unblocked_locked_board.ContentionFailsFastNotSlow."""
        path = wal_oracle.make(self.dir, frames=False)
        writer = subprocess.Popen([sys.executable, "-c", _EXCLUSIVE_WRITER, str(path), str(paths.REPO)],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(writer.wait)
        self.addCleanup(writer.kill)
        self.addCleanup(writer.stdout.close)
        self.assertEqual(writer.stdout.readline().strip(), "committed")
        self.assertGreater(os.path.getsize(str(path) + "-wal"), 0)
        started = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            capsule_db.connect_ro_tolerant(path)
        # Only has to separate the 1 s timeout from the old 5 s one;
        # 3.0 flaked at 3.05 s under a parallel suite (inc_2f3fcfa1).
        self.assertLess(time.monotonic() - started, 4.5)

    def test_timeout_is_one_second_not_five(self):
        """I7: cheap source companion to the behavioural test above (kept, not replaced): the
        one-second timeout literal is still the one in `connect_ro_tolerant`."""
        src = inspect.getsource(capsule_db.connect_ro_tolerant)
        self.assertIn("timeout=1", src)
        self.assertNotIn("timeout=5", src)


class SlackOwnerRowTests(unittest.TestCase):
    """The `slack_owner_id` row, against a config.json in a temp home (never the real one)."""

    def _row(self, cfg):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.json").write_text(json.dumps(cfg))
            return preflight._slack_owner_row(Path(d)), d

    def test_set_prints_ok_and_never_the_value(self):
        (level, name, detail), _ = self._row({"slack_owner_id": "U0ABC1234"})
        self.assertEqual((level, name, detail), ("ok", "slack_owner_id", "set"))

    def test_unset_is_a_warning_naming_the_config(self):
        (level, name, detail), d = self._row({})
        self.assertEqual((level, name), ("WARN", "slack_owner_id"))
        self.assertEqual(detail, "NOT SET (Slack button answers are refused until it is set; "
                                 f"set slack_owner_id in {Path(d) / 'config.json'})")

    def test_unset_with_slack_buttons_enabled_is_only_a_warning(self):
        """preflight.jsonl: slack-owner-id-unset-with-slack-on-warns-never-fails (architect
        2026-11-14: the owner postponed it on purpose, so no incident per dark-test run)."""
        cfg = {"slack": {"identity": "bot", "channels": {"inbox": "C0123456789"}}}
        (level, _, detail), _ = self._row(cfg)
        self.assertEqual(level, "WARN")
        self.assertIn("Slack button answers are refused", detail)

    def test_unset_with_slack_disabled_stays_a_warning(self):
        cfg = {"slack": {"enabled": False, "identity": "bot", "channels": {"inbox": "C0123456789"}}}
        (level, _, _), _ = self._row(cfg)
        self.assertEqual(level, "WARN")


if __name__ == "__main__":
    unittest.main()
