# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   BoardsListCapsuleBoards.test_the_write_side_callers_still_list_no_boards_by_default,
#   Helpers.test_board_exceptions_and_fence_import_resolve_through_board,
#   WriteFence.test_every_intent_example_replays
"""C29 (decision rows 599/600, plan risk R3): a board marked `.capsule-owned` is Capsule's. Every
Board write verb refuses with BoardFrozen before any argv runs, reads keep working, and
`board_bridge sync` leaves the board alone with one NOTE. Temp boards dirs only."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["AGENT_OPS_NO_SEND"] = "1"
_spool = Path(tempfile.mkdtemp(prefix="events-fence-")) / "e.jsonl"
os.environ.setdefault("EVENTS_SPOOL", str(_spool))
import board  # noqa: E402
import board_bridge as bb  # noqa: E402
import board_fence  # noqa: E402

INTENT = Path(__file__).resolve().parents[2] / "contracts" / "intent" / "board_fence.jsonl"


def _boards(*names, marked=()):
    root = Path(tempfile.mkdtemp(prefix="boards-fence-"))
    for n in names:
        (root / n).mkdir(parents=True)
        (root / n / "kanban.db").write_text("")
    for n in marked:
        (root / n / ".capsule-owned").write_text("")
    return root


def _probe_default_dir(action):
    """(marker path, subprocess result): LEGACY_KANBAN_HOME set, marker written under the
    resolver's `kanban/boards/<board>`, Board opened WITHOUT `boards_dir=`, one `action` tried."""
    from capsule import paths
    home = Path(tempfile.mkdtemp(prefix="hkh-"))
    env = {k: v for k, v in os.environ.items() if k != "LEGACY_KANBAN_HOME"}
    env.update({"LEGACY_KANBAN_HOME": str(home), "AGENT_OPS_NO_SEND": "1"})
    with unittest.mock.patch.dict(os.environ, {"LEGACY_KANBAN_HOME": str(home)}):
        take_dir = paths.legacy_home() / "kanban" / "boards" / "b"
    take_dir.mkdir(parents=True)
    (take_dir / "kanban.db").write_text("")
    (take_dir / board_fence.OWNED_MARKER).write_text("")
    probe = ("import board, board_fence\n"
             "print(board.BOARDS_DIR / 'b' / board_fence.OWNED_MARKER)\n"
             "calls = []\n"
             f"try:\n    board.open_board('b', runner=calls.append).{action}('t_1', 'x')\n"
             "except board.BoardFrozen:\n    print('refused')\n"
             "print('calls=%d' % len(calls))\n")
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True,
                         cwd=Path(board.__file__).parent, timeout=60)
    return take_dir / board_fence.OWNED_MARKER, out


class WriteFence(unittest.TestCase):
    READS = {"show", "try_show", "list", "lock", "emit", "append_ledger", "db_path"}
    WRITES = {
        "create": lambda b: b.create(board.NewCard(title="t")),
        "comment": lambda b: b.comment("t_1", "hi"),
        "block": lambda b: b.block("t_1", "why", kind="needs_input"),
        "unblock": lambda b: b.unblock("t_1"),
        "complete": lambda b: b.complete("t_1", "done"),
        "set_model": lambda b: b.set_model("t_1", "m"),
        "archive": lambda b: b.archive("t_1"),
    }

    def setUp(self):
        self.boards = _boards("capsule-corp", "sample-app", marked=("capsule-corp",))
        self.calls = []

    def runner(self, argv):
        self.calls.append(argv)
        return json.dumps({"task": {"id": "t_1", "status": "ready"}, "id": "t_9"})

    def open(self, name="capsule-corp"):
        return board.open_board(name, runner=self.runner, boards_dir=self.boards)

    def test_every_write_verb_is_refused_on_a_marked_board_and_runs_nothing(self):
        b = self.open()
        for verb, call in self.WRITES.items():
            with self.assertRaises(board.BoardFrozen, msg=verb) as cm:
                call(b)
            self.assertIn(".capsule-owned", str(cm.exception), verb)    # loud: names the marker
            self.assertIn("capsule intake", str(cm.exception), verb)
        self.assertEqual(self.calls, [])                    # oracle 2: no subprocess argv at all

    def test_reads_still_work_on_a_marked_board(self):
        self.assertEqual(self.open().show("t_1").id, "t_1")
        self.assertEqual(len(self.calls), 1)

    def test_an_unmarked_sibling_board_still_writes(self):
        for verb, call in self.WRITES.items():
            self.calls.clear()
            call(self.open("sample-app"))
            self.assertGreaterEqual(len(self.calls), 1, verb)           # the fence is per board

    def test_removing_the_marker_unfreezes(self):
        b = self.open()
        with self.assertRaises(board.BoardFrozen):
            b.comment("t_1", "x")
        (self.boards / "capsule-corp" / ".capsule-owned").unlink()
        b.comment("t_1", "x")
        self.assertEqual(len(self.calls), 1)

    def test_a_new_public_method_must_be_classified_as_read_or_write(self):
        public = {n for n, v in vars(board.Board).items()
                  if not n.startswith("_") and (callable(v) or isinstance(v, property))}
        self.assertEqual(public - self.READS, set(self.WRITES))

    def test_every_intent_example_replays(self):
        """Every `contracts/intent/board_fence.jsonl` example, replayed through the real Board."""
        docs = [json.loads(ln) for ln in INTENT.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(docs), 3)
        marker = self.boards / "capsule-corp" / ".capsule-owned"
        for ex in docs:
            given, expect = ex["given"]["document"], ex["expect"]
            if given.get("legacy_kanban_home_set"):
                home_marker, out = _probe_default_dir(given["action"])
                self.assertEqual((expect["refused"], expect["legacy_argv_run"]),
                                 ("BoardFrozen", False))
                want = [str(home_marker), "refused", "calls=0"]
                self.assertEqual(out.stdout.split(), want, ex["id"] + out.stderr)
                continue
            self.calls.clear()
            if marker.exists():
                marker.unlink()
            if given["board_marked"]:
                marker.write_text("")
            b = self.open()
            action = given["action"]
            call = b.show if action == "show" else self.WRITES[action]
            args = ("t_1",) if action == "show" else (b,)
            if expect["refused"]:
                self.assertEqual(expect["refused"], board.BoardFrozen.__name__)
                with self.assertRaises(board.BoardFrozen, msg=ex["id"]) as cm:
                    call(*args)
                self.assertIn(expect["error_names"], str(cm.exception), ex["id"])
            else:
                call(*args)
            self.assertEqual(bool(self.calls), expect["legacy_argv_run"], ex["id"])


class Helpers(unittest.TestCase):
    def test_unfrozen_names(self):
        boards = _boards("x", "y", "z", marked=("y",))
        self.assertEqual(board_fence.unfrozen_names(boards, ["y"]), ["x", "z"])

    def test_note_and_drop_frozen_drop_the_marked_boards_cards_and_print_one_note(self):
        boards = _boards("x", "y", marked=("y",))
        cards = [types.SimpleNamespace(board="x"), types.SimpleNamespace(board="y")]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            frozen = board_fence.note_frozen(boards)
        kept = board_fence.drop_frozen(cards, frozen)
        self.assertEqual((frozen, [c.board for c in kept]), (["y"], ["x"]))
        self.assertEqual(buf.getvalue().count("NOTE:"), 1)
        self.assertIn("y", buf.getvalue())

    def test_board_exceptions_and_fence_import_resolve_through_board(self):
        import board_errors
        for n, cls in (("BoardError", board.BoardError), ("CardNotFound", board.CardNotFound),
                       ("LegacyCliError", board.LegacyCliError), ("Locked", board.Locked),
                       ("LegacyOutputInvalid", board.LegacyOutputInvalid),
                       ("TransitionRefused", board.TransitionRefused),
                       ("BoardFrozen", board.BoardFrozen)):
            self.assertIs(vars(board_errors)[n], cls, n)
        self.assertTrue(issubclass(board.BoardNotFound, board.BoardError))
        self.assertIs(board.board_fence, board_fence)
        self.assertTrue(issubclass(board.BoardFrozen, board.BoardError))
        self.assertTrue(issubclass(board.LegacyOutputInvalid, board.LegacyCliError))


class MarkerPathMatchesTake(unittest.TestCase):
    """Loophole HIGH 1: the marker lives under paths.legacy_home() (LEGACY_KANBAN_HOME)."""

    def test_default_boards_dir_follows_the_resolver_take_uses_and_refuses_a_write(self):
        marker, out = _probe_default_dir("comment")
        self.assertEqual(out.stdout.split(), [str(marker), "refused", "calls=0"], out.stderr)


class BoardsListCapsuleBoards(unittest.TestCase):
    """The security job renders `board_bridge.py boards` into its prompt and refuses on an empty
    line. With the old store gone, that line must come from CAPSULE_HOME's boards, never
    the old store's dir."""

    def _boards_cli(self, *names, legacy_dir=False):
        capsule, legacy = (Path(tempfile.mkdtemp(prefix="bb-")) for _ in range(2))
        for n in names:
            (capsule / "boards" / n).mkdir(parents=True)
            (capsule / "boards" / n / "kanban.db").write_text("")
        if legacy_dir:
            (legacy / "kanban" / "boards" / "legacy").mkdir(parents=True)
        env = dict(os.environ, CAPSULE_HOME=str(capsule), LEGACY_KANBAN_HOME=str(legacy),
                   AGENT_OPS_NO_SEND="1", EVENTS_SPOOL=str(capsule / "e.jsonl"))
        script = Path(board.__file__).with_name("board_bridge.py")
        return subprocess.run([sys.executable, str(script), "boards"], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_two_capsule_boards_are_printed_on_one_line(self):
        out = self._boards_cli("capsule-corp", "sample-app")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "capsule-corp sample-app"),
                         out.stderr)

    def test_no_boards_prints_nothing_so_the_security_job_still_refuses(self):
        out = self._boards_cli()
        self.assertEqual((out.returncode, out.stdout.strip()), (0, ""), out.stderr)
        run_sh = (Path(board.__file__).with_name("run.sh")).read_text()
        self.assertIn('[ -z "$BOARDS" ]', run_sh)
        self.assertIn("refusing to guess a board list", run_sh)

    def test_add_finding_checks_the_board_name_against_capsule_home_boards(self):
        """No `boards_dir=`: names come from $CAPSULE_HOME/boards. Temp home and temp vault only;
        the verb appends one text line to Boards/Intake.md and touches no board DB."""
        import intake
        home, vault = (Path(tempfile.mkdtemp(prefix="af-")) for _ in range(2))
        (home / "boards" / "capsule-corp").mkdir(parents=True)
        (home / "boards" / "capsule-corp" / "kanban.db").write_text("")
        (vault / "Boards").mkdir()
        path = vault / "Boards" / "Intake.md"
        path.write_text(f"{intake.NEW_WORK}\n{intake.FINDINGS}\n", encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {"CAPSULE_HOME": str(home)}):
            _, appended = intake.add_finding("capsule-corp", "med", "Default dir", "sec 1",
                                             vault=vault)
            with self.assertRaises(ValueError) as cm:
                intake.add_finding("sample-app", "med", "Not here", "sec 1", vault=vault)
        self.assertTrue(appended)
        self.assertIn("(have: capsule-corp)", str(cm.exception))
        self.assertIn("- repo: capsule-corp | risk: med | Default dir (sec 1)", path.read_text())
        self.assertEqual(sorted(p.name for p in (home / "boards" / "capsule-corp").iterdir()),
                         ["kanban.db"])

    def test_a_legacy_dir_with_no_boards_does_not_hide_capsule_boards(self):
        out = self._boards_cli("capsule-corp", legacy_dir=True)
        self.assertEqual(out.stdout.strip(), "capsule-corp", out.stderr)

    def test_the_write_side_callers_still_list_no_boards_by_default(self):
        """decisions, merge_gate and intake.consume resolve `bb.KANBAN_BOARDS_DIR` /
        `board.BOARDS_DIR`; with live Capsule boards in CAPSULE_HOME they must stay blind (the
        `.capsule-owned` fence exists only under the legacy dir)."""
        capsule, legacy = (Path(tempfile.mkdtemp(prefix="blind-")) for _ in range(2))
        (capsule / "boards" / "capsule-corp").mkdir(parents=True)
        (capsule / "boards" / "capsule-corp" / "kanban.db").write_text("")
        env = dict(os.environ, CAPSULE_HOME=str(capsule), LEGACY_KANBAN_HOME=str(legacy),
                   AGENT_OPS_NO_SEND="1", EVENTS_SPOOL=str(capsule / "e.jsonl"))
        probe = ("import board, board_bridge as bb, decisions\n"
                 "print(board.board_names(), board.board_names(bb.KANBAN_BOARDS_DIR))\n"
                 "print(decisions.blocked_decisions(kanban=lambda *a, **k: ''))\n"
                 "print(bb.KANBAN_BOARDS_DIR == board.BOARDS_DIR)\n"
                 "import importlib.util\n"
                 "print(importlib.util.find_spec('repair') is None)\n")
        out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                             text=True, timeout=60, cwd=Path(board.__file__).parent)
        self.assertEqual(out.stdout.splitlines(), ["[] []", "[]", "True", "True"], out.stderr)


if __name__ == "__main__":
    unittest.main()
