"""S4a: the deterministic risk classifier (`capsule/risk.py`, D15 risk classes, D21 big).

Oracles (Z2):
  1. FixtureTable: 21 real past cards (fixtures/risk/cards.json), each hand-labelled from a
     read-only copy of the capsule-corp board by reading the card, with the reason written down.
  2. BlindCards: cards written and labelled by an agent that never saw the code, from the spec
     only (fixtures/risk/blind_cards.json).
  RegressionTable: synthetic cards written during review (D15/D21 wording plus every probe that
  once came back wrong). It guards the code; it is not an independent oracle.
Z1: IntentExamples replays `contracts/intent/risk.jsonl` and skips until that file ships."""
import json
import socket
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from capsule import intent, risk

CARDS = Path(__file__).parent / "fixtures" / "risk" / "cards.json"
BLIND = Path(__file__).parent / "fixtures" / "risk" / "blind_cards.json"
TEN = [f"capsule/mod{i}.py" for i in range(10)]


def verdict(case):
    return risk.classify(case.get("title", ""), case.get("body", ""), case.get("files", ()))


class FixtureTable(unittest.TestCase):
    def setUp(self):
        self.cards = json.loads(CARDS.read_text())

    def test_the_table_is_real_cards_with_a_mix_and_a_reason_each(self):
        self.assertGreaterEqual(len(self.cards), 20)
        self.assertEqual(len({c["number"] for c in self.cards}), len(self.cards))
        seen = {f for c in self.cards for f in c["flags"]}
        self.assertEqual(seen, {"judges", "data", "public", "big"})
        self.assertGreaterEqual(sum(c["level"] == "low" for c in self.cards), 5)
        self.assertGreaterEqual(sum(len(c["flags"]) > 1 for c in self.cards), 2)
        for c in self.cards:
            self.assertTrue(c["why"].strip(), c["number"])
            self.assertEqual(c["level"], "risky" if c["flags"] else "low", c["number"])

    def test_every_card_gets_its_hand_label(self):
        for c in self.cards:
            with self.subTest(card=c["number"], title=c["title"][:50]):
                got = risk.classify(c["title"], c["body"])
                self.assertEqual((got["level"], got["flags"]), (c["level"], c["flags"]),
                                 got["reasons"])


def bullets(n, fmt="- capsule/m{}.py"):
    return "".join(fmt.format(i) + "\n" for i in range(n))


# (name, card, expected flags): D15 "judges, data/schema/retention, public/external/money, big",
# D21 "10+ files or 3+ hours", then each miss the Opus review found.
SPEC = [
    ("protected path", {"files": ["capsule/workspace.py"]}, ["judges"]),
    ("protected doc", {"files": ["docs/ZENKAI.md"]}, ["judges"]),
    ("contract file", {"files": ["contracts/reviewer.md"]}, ["judges"]),
    ("hook script", {"files": ["maintenance/hooks/write-guard.py"]}, ["judges"]),
    ("gate script", {"files": ["tests/size_check.py"]}, ["judges"]),
    ("a gate named in the title", {"title": "Teach mutation_check a new rule"}, ["judges"]),
    ("deleting a test", {"body": "Delete the failing test so the build passes."}, ["judges"]),
    ("ordinary code and its test",
     {"files": ["capsule/cost.py", "capsule/tests/test_cost.py"]}, []),
    ("a bug fix with a new test",
     {"title": "Fix the cost median",
      "body": "File set: capsule/cost.py, capsule/tests/test_cost.py"}, []),
    ("schema column", {"body": "Add a nullable column to tasks and a migration."}, ["data"]),
    ("sql file", {"files": ["observability/postgres/init.sql"]}, ["data"]),
    ("retention", {"body": "Keep each backup for 30 days (retention policy)."}, ["data"]),
    ("push to main", {"body": "After the checks, run git push origin main."}, ["public"]),
    ("public workflow", {"files": [".github/workflows/verify.yml"]}, ["public"]),
    ("money", {"body": "Send an invoice and charge the credit card."}, ["public"]),
    ("a notional cost report", {"body": "Show an estimated USD cost per card."}, []),
    ("ten files is big", {"files": TEN}, ["big"]),
    ("nine files is not", {"files": TEN[:9]}, []),
    ("repeated files count once", {"files": TEN + TEN}, ["big"]),
    ("three hours is big", {"body": "Size: about 3h."}, ["big"]),
    ("two hours is not", {"body": "Size: about 2h."}, []),
    ("a range counts at its top", {"body": "Size: 2-4 hours."}, ["big"]),
    ("just under three", {"body": "Size: 2.9 hours."}, []),
    ("hours of agent time", {"body": "Expect 4 hours of agent time."}, ["big"]),
    ("a duration that is not an estimate", {"body": "Alarm on a hold older than 3 hours."}, []),
    ("file set bullets are counted",
     {"body": "File set:\n" + "".join(f"- capsule/m{i}.py\n" for i in range(10))}, ["big"]),
    ("file set on one line",
     {"body": "File set: " + ", ".join(TEN) + "."}, ["big"]),
    ("do-not-touch section is ignored",
     {"body": "Fix it.\n\n## Do not touch\n\n`capsule/lander.py` and `contracts/**`\n\n## Notes\n"},
     []),
    ("out-of-scope line is ignored",
     {"body": "Out of scope: capsule/workspace.py and schema changes."}, []),
    ("prose paths count beside declared files",
     {"body": "File set: capsule/cost.py\nSee capsule/workspace.py for why."}, ["judges"]),
    ("several classes at once",
     {"files": ["contracts/schemas/report.v1.json"] + TEN}, ["judges", "data", "big"]),
    ("bold Files header", {"body": "**Files:**\n- Modify: `capsule/cost.py`\n- Test: `x.py`"}, []),
    ("bold file set header", {"body": "**File set:**\n" + bullets(10)}, ["big"]),
    ("heading file set", {"body": "## File set\n" + bullets(10)}, ["big"]),
    ("files touched header", {"body": "Files touched:\n" + bullets(10)}, ["big"]),
    ("blank line after the header", {"body": "File set:\n\n" + bullets(10)}, ["big"]),
    ("numbered bullets", {"body": "File set:\n" + bullets(10, "  1. capsule/m{}.py")}, ["big"]),
    ("size baseline file", {"files": ["tests/size_baseline.json"]}, ["judges"]),
    ("bare ZENKAI.md", {"body": "Edit ZENKAI.md to add a Z9 item."}, ["judges"]),
    ("bare meters.py", {"body": "Extend meters.py to count retries."}, ["judges"]),
    ("bare IMAGES.lock", {"body": "Bump the pin and refresh IMAGES.lock."}, ["judges"]),
    ("bare build-base.sh", {"body": "Edit build-base.sh to cache pip."}, ["judges"]),
    ("OPS_WORKER_IMAGE as a variable", {"body": "Parse the OPS_WORKER_IMAGE pin."}, []),
    ("claude hooks dir", {"body": "Change .claude/hooks/secret-read-guard.py to block mode."},
     ["judges"]),
    ("settings.json", {"files": [".claude/settings.json"]}, ["judges"]),
    ("lint scripts", {"files": ["maintenance/migration_lint.py", "tests/portability_lint.py"]},
     ["judges"]),
    ("merge gate", {"files": ["maintenance/merge_gate.py"]}, ["judges"]),
    ("bin script", {"files": ["bin/capsule-unblocked-hook.sh"]}, ["judges"]),
    ("bare sandbox.py", {"body": "Make sandbox.py wait instead of going red."}, ["judges"]),
    ("bare slack_listen.py", {"body": "Let slack_listen.py accept any user."}, ["judges"]),
    ("a path suffix", {"body": "Edit runners/sandbox.py so it never refuses."}, ["judges"]),
    ("the sandbox word alone", {"body": "Run it in the sandbox."}, []),
    ("a docs path sharing the name", {"body": "Fix docs/sandbox.md wording."}, []),
    ("bare events.py", {"body": "Make events.py retry the docker call."}, ["judges"]),
    ("a call on the events module", {"body": "Log it with events.log(\"card\", \"x\")."}, []),
    ("a file name before a paren",
     {"body": "Edit sandbox.py(the runner) so it never refuses."}, ["judges"]),
    # Accepted over-flagging: a bare protected name (`__init__.py`, `docker.py`) in a card's
    # prose flags it "judges" even when the card means another file of that name. Flags only
    # send a card to more review (judges); they never refuse it.
    ("bare docker.py", {"body": "Add a --pull flag to docker.py run args."}, ["judges"]),
    ("allowlist and gitleaks", {"files": ["deps-allowlist.txt", ".gitleaks.toml"]}, ["judges"]),
    ("meters in the title", {"title": "Meters: count retries"}, ["judges"]),
    ("disable a check", {"body": "Disable the simplicity check for docs/ files."}, ["judges"]),
    ("drop a gate from the lander",
     {"body": "Remove size_check from the lander's gate list."}, ["judges"]),
    ("skip a test by name", {"body": "Skip test_deploy_canary; it flakes."}, ["judges"]),
    ("lower a threshold", {"body": "Lower the mutation score threshold to 60%."}, ["judges"]),
    ("role contract", {"body": "Change the reviewer role contract so it may approve."}, ["judges"]),
    ("a protected path before an out-of-scope sentence",
     {"body": "Edit capsule/workspace.py. The UI redesign is out of scope."}, ["judges"]),
    ("a do-not-touch sentence ends at its sentence",
     {"body": "Do not touch tick.py; change capsule/lander.py instead."}, ["judges"]),
    ("a prior-verdicts section is history",
     {"body": "Fix it.\n\n## Prior verdicts\n\nrow 5: edit contracts/schemas/report.v1.json\n"},
     []),
    ("not in this card", {"body": "NOT in this card: contracts/schemas/report.v1.json changes."},
     []),
    ("intent rows are not a judge", {"files": ["contracts/intent/tick.jsonl"]}, []),
    ("intent rows beside a code file",
     {"body": "File set: capsule/tick.py\nAlso add two rows to contracts/intent/tick.jsonl."}, []),
    ("db.py column", {"body": "Add a `waits_on` column to tasks in db.py."}, ["data"]),
    ("column without db.py", {"body": "Add column waits_on to the tasks table."}, ["data"]),
    ("backfill rows", {"body": "New tasks.waits_on field; backfill existing rows."}, ["data"]),
    ("a backfill command name", {"body": "Short names and backfill commands exist."}, []),
    ("purge", {"body": "Drop the events table and purge rows older than 30 days."}, ["data"]),
    ("kanban.db", {"body": "Delete old runs from kanban.db weekly."}, ["data"]),
    ("prune", {"body": "Prune the spool and rotate logs, keeping 7 days."}, ["data"]),
    ("schema version bump", {"body": "Bump report.v1 to report.v2."}, ["data"]),
    ("create index", {"body": "CREATE INDEX on tasks(status)."}, ["data"]),
    ("stripe", {"body": "Wire Stripe checkout for the Pro plan."}, ["public"]),
    ("pricing", {"body": "Update the pricing page for the new plan."}, ["public"]),
    ("buy and pay", {"body": "Buy a domain and pay for the Hetzner box."}, ["public"]),
    ("email", {"body": "Email the weekly report to the team."}, ["public"]),
    ("publish to PyPI", {"body": "Tag v1.0 and publish to PyPI."}, ["public"]),
    ("make the repo public", {"body": "Make the GitHub repo public and announce it."}, ["public"]),
    ("force push to main", {"body": "git push --force to main after the rebase."}, ["public"]),
    ("cloudflare", {"body": "Deploy the site to Cloudflare Pages."}, ["public"]),
    ("release dir", {"files": ["release/public-paths.txt"]}, ["public"]),
    ("telegram to owner is not public",
     {"body": "Send a Telegram message to Owner when a card parks."}, []),
    ("slack to owner is not public", {"body": "Post the brief to Slack every morning."}, []),
    ("git checkout is not a checkout page", {"body": "Run git checkout of the epic clone."}, []),
    ("subscription-only usage", {"body": "It is subscription-only, so label it notional."}, []),
    ("estimate colon", {"body": "Estimate: 4h"}, ["big"]),
    ("tilde hours", {"body": "~4h"}, ["big"]),
    ("a README typo is not public", {"files": ["README.md"]}, []),
    ("a styling column", {"body": "Set the column to min-width: 9rem."}, []),
    ("reading an existing column", {"body": "The retry_count column exists. Just read it."}, []),
    ("retention in days",
     {"body": "Logs are kept for 90 days; change that to 14 and delete anything older."},
     ["data"]),
    ("cname", {"body": "Update the CNAME so it points at the new load balancer."}, ["public"]),
    ("size baseline anywhere", {"files": ["maintenance/size_baseline.json"]}, ["judges"]),
    ("raise a baseline", {"body": "Raise the size baseline for tick.py."}, ["judges"]),
    ("a skip decorator", {"body": 'Add @pytest.mark.skip(reason="flaky") to it.'}, ["judges"]),
    ("must not break, then edit the lander",
     {"body": "The retry must not break the revert rule, so edit capsule/lander.py to retry."},
     ["judges"]),
    ("must not loop, and the lander changes",
     {"body": "It must not loop forever, and capsule/lander.py gets a retry counter."}, ["judges"]),
    # known miss: the exclusion runs on past ", which is why" (the real intent is to edit it)
    ("must not edit, which is why a path is protected",
     {"body": "Workers must not edit the image pin, which is why this card adds "
              "docker/IMAGES.lock to PROTECTED in capsule/workspace.py."}, []),
    ("must not edit it, then a dash",
     {"body": "Workers must not edit it - add docker/IMAGES.lock to capsule/workspace.py."},
     ["judges"]),
    ("must not block, update a hook",
     {"body": "The hook must not block reads of docs/ (update bin/worktree-only-edits.py)."},
     ["judges"]),
    ("must not fail the gate, teach a gate",
     {"body": "A docs-only change must not fail the gate: teach tests/size_check.py to skip .md."},
     ["judges"]),
    ("must not lose rows, adds a column",
     {"body": "The tick must not lose rows when it adds the waits_on column in capsule/db.py."},
     ["data"]),
    ("must not break old boards, add a column",
     {"body": "Must not break old boards: add a nullable waits_on column with a migration"},
     ["data"]),
    ("must not push red, then push main",
     {"body": "The lander must not push a red build, and now it runs git push origin main."},
     ["public"]),
    ("not in this card, then edit protected paths",
     {"body": "Not in this card: the UI. Edit capsule/workspace.py to add a glob, and explicitly "
              "exclude nothing else, then update capsule/lander.py"}, ["judges"]),
    # known miss: "explicitly exclude docs ... by editing X" hides the gate edit
    ("explicitly exclude docs by editing a gate",
     {"body": "We explicitly exclude docs from the scan by editing tests/simplicity_check.py."},
     []),
    ("do not change elsewhere, only edit the lander",
     {"body": "Do not change behaviour elsewhere, only edit capsule/lander.py's retry"},
     ["judges"]),
    ("a guide-ruling card that passed its schema",
     {"title": "guide_ruling: a reviewer run passed its schema but contradicts itself",
      "body": "The reviewer run passed its schema but the verdict says pass with findings."}, []),
    ("a schema violation is a report, not a change",
     {"body": "The worker output was a schema violation; file the ruling."}, []),
    ("kanban.db read in read-only mode",
     {"body": "Open kanban.db with mode=ro and list the ready cards."}, []),
    ("a sentence that reads kanban.db", {"body": "The detector reads kanban.db each tick."}, []),
    ("a column with a type in parentheses",
     {"body": "Tasks get a `waits_on` column (TEXT, default NULL)."}, ["data"]),
    ("a value stored in a column",
     {"body": "Store the branch in a waits_on column on tasks."}, ["data"]),
    ("an empty card", {}, []),
    ("a card without a body", {"title": "Rename a variable", "body": None}, []),
]


class RegressionTable(unittest.TestCase):
    def test_every_spec_case(self):
        for name, card, flags in SPEC:
            with self.subTest(name):
                got = verdict(card)
                self.assertEqual(got["flags"], flags, got["reasons"])
                self.assertEqual(got["level"], "risky" if flags else "low")

    def test_reasons_exist_exactly_when_risky_and_name_the_cause(self):
        low = verdict({"title": "Rename a variable"})
        self.assertEqual((low["level"], low["flags"], low["reasons"]), ("low", [], []))
        got = verdict({"files": ["capsule/workspace.py"]})
        self.assertEqual(len(got["reasons"]), 1)
        self.assertIn("capsule/workspace.py", got["reasons"][0])
        self.assertIn("protected", got["reasons"][0])

    def test_flags_come_in_the_fixed_class_order(self):
        got = verdict({"files": ["contracts/schemas/report.v1.json"] + TEN})
        self.assertEqual(got["flags"], ["judges", "data", "big"])

    def test_it_is_pure(self):
        files = list(TEN)
        card = {"title": "t", "body": "Size: 4h", "files": files}
        with mock.patch.object(sqlite3, "connect", side_effect=AssertionError("db")), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("net")):
            first = verdict(card)
        self.assertEqual(first, verdict(card))
        self.assertEqual(files, TEN)


class BlindCards(unittest.TestCase):
    """Oracle 2: 25 cards a code-blind agent wrote and labelled from the spec alone. The labels
    are never edited to fit the code; a disagreement is either a code fix or a ruling."""

    def test_every_blind_card_gets_its_spec_label(self):
        cards = json.loads(BLIND.read_text())
        self.assertEqual(len(cards), 25)
        self.assertEqual({c["level"] for c in cards}, {"risky", "low"})
        for c in cards:
            with self.subTest(card=c["id"], title=c["title"]):
                self.assertTrue(c["why"].strip())
                got = risk.classify(c["title"], c["body"], c["files"])
                self.assertEqual((got["level"], got["flags"]), (c["level"], c["flags"]),
                                 got["reasons"])


class IntentExamples(unittest.TestCase):
    """contracts/intent/risk.jsonl, replayed on the real function (Z1). The loader test skips
    inside its body until the file ships, so the mutation gate sees no @skipIf."""

    def test_every_example_matches_its_oracle(self):
        examples = intent.load("risk")
        if not examples:
            self.skipTest("contracts/intent/risk.jsonl has not shipped yet")
        for row in examples:
            with self.subTest(row["id"]):
                self.replay(row["given"]["document"], row["expect"])

    def replay(self, doc, expect):
        self.assertEqual(doc["check"], "risk")
        self.assertLessEqual(set(doc), {"check", "title", "body", "files"})
        self.assertLessEqual(set(expect), {"level", "flags"})
        got = risk.classify(doc.get("title", ""), doc.get("body", ""), doc.get("files", ()))
        for key, want in expect.items():
            self.assertEqual(got[key], want, got["reasons"])


if __name__ == "__main__":
    unittest.main()
