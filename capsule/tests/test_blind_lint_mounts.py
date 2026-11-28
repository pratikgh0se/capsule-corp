"""The lint's one mount table check (`lint._mount_problems`): a card file whose raw bytes name the
mount table (`mountinfo`, `mountstats`, `/etc/mtab`) is refused. It catches accidents only; the
red check's mount shape is what keeps the check unseen. It must not raise and must not run long
on any input. No docker.

Z2 oracles: the problem list `lint.problems` gives and, for the allowed cases, the same files
without the one line (an empty list)."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from capsule.blind import lint
from capsule.tests import tmpclean

CARD = "t_0a1b2c3d"
HASH = "h" * 64
GOOD = ("import unittest\n\nfrom harness import run\n\n\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n        run('true')\n")
EXAMPLE = json.dumps({"id": f"{CARD}.test_a", "given": "g", "expect": "e", "source": "s"})


def files(**over):
    base = {"test_it.py": GOOD, "examples.jsonl": EXAMPLE + "\n",
            "HEADER.json": json.dumps({"card": CARD, "card_hash": HASH, "statements": [1]})}
    base.update(over)
    return {f"cards/{CARD}/{name}": (data if isinstance(data, bytes) else data.encode())
            for name, data in base.items() if data is not None}


def problems(**over):
    return lint.problems(files(**over), CARD, HASH)


class Refused(unittest.TestCase):
    def test_a_file_that_names_the_mount_table_is_refused_with_the_plain_reason(self):
        for name, text in (("test_it.py", GOOD + "x = open('/proc/self/mountinfo')\n"),
                           ("test_it.py", GOOD + "# mountstats\n"),
                           ("test_it.py", GOOD + "p = '/etc/mtab'\n"),
                           ("helper.py", "y = 'mountinfo'\n"),
                           ("run.sh", "cat /proc/self/mountinfo\n"),
                           ("data.txt", "/etc/mtab\n"),
                           ("test_it.py", GOOD + "x = open('/proc/mounts').read()\n"),
                           ("helper.py", "p = '/proc/self/mounts'\n"),
                           ("run.sh", "cat /proc/mounts\n"),
                           ("blob.bin", b"\x00\xff/proc/self/mounts\x00"),
                           ("blob.bin", b"\x00\xffmountinfo\x00")):
            with self.subTest(name + str(text)[:20]):
                (found,) = problems(**{name: text})
                self.assertTrue(found.startswith(f"cards/{CARD}/{name}: names the mount table;"))
                for want in ("refused", "mount table", "red check", "real run"):
                    self.assertIn(want, found)

    def test_each_file_is_one_problem_however_often_it_names_it(self):
        twice = "x = 'mountinfo mountinfo'\ny = 'mountstats'\n"
        found = problems(**{"a.py": twice, "b.txt": "mountinfo"})
        self.assertEqual([f.split(":")[0] for f in found],
                         [f"cards/{CARD}/a.py", f"cards/{CARD}/b.txt"])


class Allowed(unittest.TestCase):
    def test_ordinary_files_temp_folders_and_proc_stay_allowed(self):
        for line in ("open('data.txt', 'w')", "import tempfile\ntempfile.mkdtemp()",
                     "assert '/proc' not in run('ls').stdout", "open('/proc/self/status')",
                     "run('mount')", "run('echo amount')", "x = '/proc/self/mem'",
                     "y = '/proc/mountpoint'"):
            with self.subTest(line):
                self.assertEqual(problems(**{"test_it.py": GOOD + line + "\n"}), [])

    def test_the_examples_and_header_may_mention_the_words(self):
        row = json.dumps({"id": f"{CARD}.test_a", "given": "the tool prints mountinfo",
                          "expect": "no /etc/mtab", "source": "s"})
        self.assertEqual(problems(**{"examples.jsonl": row + "\n"}), [])

    def test_a_card_with_helper_files_that_name_nothing_of_it_passes(self):
        self.assertEqual(problems(**{"helper.py": "x = 1\n", "fixture.txt": "a mount list\n"}), [])


class AnyInput(unittest.TestCase):
    def test_a_file_that_does_not_parse_is_the_compile_checks_problem_only(self):
        self.assertEqual(problems(**{"test_it.py": "def (:\n"}),
                         [f"cards/{CARD}/test_it.py: does not compile"])

    def test_big_deep_and_odd_files_neither_raise_nor_run_long(self):
        odd = ("x = " + "'a' + " * 3000 + "'a'\n", "chr(0x110000)\n", "(" * 500 + ")" * 500,
               "\x00" * 100000, "a" * 150000, "é\ud800".encode("utf-8", "surrogatepass"),
               b"\xff\xfe" * 50000)
        for source in odd:
            with self.subTest(str(source)[:20]):
                start = time.monotonic()
                self.assertIsInstance(problems(**{"helper.py": source}), list)
                self.assertLess(time.monotonic() - start, 5)

    def test_collect_empties_the_files_over_the_cap_so_the_lint_needs_no_size_guard(self):
        clone = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, clone)
        card = clone / "cards" / CARD
        card.mkdir(parents=True)
        (card / "big.txt").write_bytes(b"mountinfo" + b"x" * lint.MAX_BYTES)
        found, why = lint.collect(clone, CARD)
        self.assertEqual((found, len(why)), ({}, 1))

    def test_a_big_file_is_scanned_whole(self):
        big = b"x" * lint.MAX_BYTES + b"mountinfo"
        (found,) = problems(**{"big.txt": big})
        self.assertIn("big.txt: names the mount table", found)
