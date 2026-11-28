"""bin/brainstorm-handoff.py (M203; docs/architecture/components/intake.md).
Z2: the render function is
tested with a caller-supplied id (the id exists before any note text), and the CLI is run as a
real subprocess for the second oracle."""
import importlib.util
import re
import subprocess
import sys
import unittest

from capsule import intent, paths

SCRIPT = paths.REPO / "bin" / "brainstorm-handoff.py"


class _Missing:
    """Stands in for an absent script: any use fails the test, never the import (mutation gate)."""
    def __getattr__(self, name):
        raise AssertionError(f"{SCRIPT} is missing (asked for {name})")


try:
    _spec = importlib.util.spec_from_file_location("brainstorm_handoff", SCRIPT)
    bh = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(bh)
except FileNotFoundError:
    bh = _Missing()

SID = "11111111-2222-4333-8444-555555555555"
OTHER = "99999999-8888-4777-8666-555555555555"


class Render(unittest.TestCase):
    def setUp(self):
        self.out = bh.render("Fix the Slack handoff!", 3, "/work/proj dir", SID)

    def test_same_id_in_all_three_outputs(self):
        for key in ("note_header", "card_comment", "message"):
            self.assertIn(SID, self.out[key], key)
        self.assertEqual(self.out["session_id"], SID)

    def test_id_is_an_input_not_generated_by_render(self):
        other = bh.render("Fix the Slack handoff!", 3, "/work/proj dir", OTHER)
        self.assertNotIn(SID, other["message"])
        self.assertIn(OTHER, other["note_header"])

    def test_render_rejects_a_non_uuid_id(self):
        with self.assertRaises(ValueError):
            bh.render("t", 1, "/p", "not-a-uuid")

    def test_message_order_and_commands(self):
        lines = self.out["message"].split("\n")
        self.assertEqual(lines, [
            "Session: fix-the-slack-handoff",
            f"Id: {SID}",
            "Cost: 3 open questions - ~15 min",
            "cd '/work/proj dir'",
            f"claude --session-id {SID} -n fix-the-slack-handoff",
            "Later: claude -r fix-the-slack-handoff",
            bh.ONE_TERMINAL,
            "[ Open the note ] [ Not now, queue it ]",
        ])
        self.assertIn("one terminal", bh.ONE_TERMINAL.lower())

    def test_name_first_then_id(self):
        m = self.out["message"]
        self.assertLess(m.index("fix-the-slack-handoff"), m.index(SID))

    def test_cost_singular_and_zero(self):
        self.assertEqual(bh._cost_line(1), "1 open question - ~5 min")
        self.assertEqual(bh._cost_line(0), "0 open questions - ~0 min")

    def test_name_slug(self):
        self.assertEqual(bh._name_from_title("  Hello, World -- v2 "), "hello-world-v2")
        self.assertEqual(bh._name_from_title("!!!"), "brainstorm")
        self.assertLessEqual(len(bh._name_from_title("a" * 200)), bh.MAX_NAME)
        self.assertFalse(bh._name_from_title("x" * 39 + " y").endswith("-"))


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              timeout=10)

    def test_two_runs_mint_different_uuid4s(self):
        ids = []
        for _ in range(2):
            p = self.run_cli("t")
            ids.append(re.search(r"^Id: (\S+)", p.stdout, re.M).group(1))
        self.assertNotEqual(*ids)
        self.assertEqual({len(i) for i in ids}, {36})
        self.assertTrue(all(i[14] == "4" for i in ids))

    def test_negative_questions_rejected(self):
        p = self.run_cli("t", "--questions", "-1")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--questions must be 0 or more", p.stderr)

    def test_prints_the_block_with_a_fresh_uuid(self):
        p = subprocess.run([sys.executable, str(SCRIPT), "My topic", "--questions", "2",
                            "--project-dir", "/tmp/p"], capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        ids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}", p.stdout)
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])
        self.assertIn("Cost: 2 open questions - ~10 min", p.stdout)
        self.assertIn(f"claude --session-id {ids[0]} -n my-topic", p.stdout)
        self.assertIn("cd /tmp/p", p.stdout)


def problems(rows):
    """Every way a render() of each row's given misses its expect keys (empty list = agrees)."""
    bad = []
    for row in rows:
        doc = row["given"]["document"]
        sid = doc["session_id"]
        out = bh.render(doc["title"], doc["open_questions"], doc["project_dir"], sid)
        lines = out["message"].split("\n")
        for key, want in row["expect"].items():
            if key.endswith("_has_id"):
                ok = (sid in out[key[:-len("_has_id")]]) == want
            elif key == "first_lines":
                ok = lines[:len(want)] == want
            elif key in ("cd_line", "start_line"):
                ok = want in lines
            elif key == "resume_line":
                start = row["expect"]["start_line"]
                ok = want in lines and lines.index(want) == lines.index(start) + 1
            elif key == "last_line":
                ok = lines[-1] == want
            else:
                ok = False    # an expect key this executor does not know is a failure
            if not ok:
                bad.append(f"{row['id']}: {key}")
    return bad


class IntentRows(unittest.TestCase):
    def setUp(self):
        self.rows = intent.load("brainstorm_handoff")

    def test_rows_exist_and_agree_with_render(self):
        self.assertEqual(len(self.rows), 3, "brainstorm_handoff intent file: need 3 rows")
        self.assertEqual(problems(self.rows), [])

    def test_a_changed_expectation_is_caught(self):
        mutated = [{**r, "expect": {k: ("Later: claude -r other" if k == "resume_line" else v)
                                    for k, v in r["expect"].items()}} for r in self.rows]
        self.assertEqual(problems(mutated), ["cd-and-start-line-then-buttons-last: resume_line"])


if __name__ == "__main__":
    unittest.main()
