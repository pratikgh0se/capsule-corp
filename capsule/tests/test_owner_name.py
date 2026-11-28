"""D251: the owner's name is the `owner.name` setting (default "owner"), not a literal in code."""
import contextlib
import io
import itertools
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import host, human, preflight, report, row_schemas

class HostSetting(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.env = {}

    def test_default_owner_name_is_owner(self):
        self.assertEqual(host.get("owner.name", self.home, self.env), "owner")
        self.assertEqual(host.owner_name(self.home, self.env), "owner")

    def test_owner_copyright_is_empty_by_default_and_read_from_host_json(self):
        self.assertEqual(host.get("owner.copyright", self.home, self.env), "")
        (self.home / "host.json").write_text(json.dumps({"owner": {"copyright": "Ada Lovelace"}}))
        self.assertEqual(host.get("owner.copyright", self.home, self.env), "Ada Lovelace")
        self.assertEqual(host.owner_name(self.home, self.env), "owner")   # a separate setting

    def test_host_json_overrides_and_env_beats_it(self):
        (self.home / "host.json").write_text(json.dumps({"owner": {"name": "Ada"}}))
        self.assertEqual(host.owner_name(self.home, self.env), "Ada")
        self.assertEqual(host.owner_name(self.home, {"CAPSULE_OWNER_NAME": "Bo"}), "Bo")

    def test_bad_value_is_refused_by_get_and_defaults_for_the_accessor(self):
        (self.home / "host.json").write_text(json.dumps({"owner": {"name": 5}}))
        with self.assertRaises(host.HostError):
            host.get("owner.name", self.home, self.env)
        self.assertEqual(host.owner_name(self.home, self.env), "owner")


    def test_roles_and_blank_names_are_refused(self):
        for bad in ("cos", "COS", "opus", " ", "", "9lives"):
            with self.assertRaises(host.HostError, msg=repr(bad)):
                host.get("owner.name", self.home, {"CAPSULE_OWNER_NAME": bad or " "})
            (self.home / "host.json").write_text(json.dumps({"owner": {"name": bad}}))
            with self.assertRaises(host.HostError, msg=repr(bad)):
                host.get("owner.name", self.home, self.env)
            self.assertEqual(host.owner_name(self.home, {"CAPSULE_OWNER_NAME": bad or " "}),
                             "owner")
        self.assertEqual(host.owner_name(self.home, {"CAPSULE_OWNER_NAME": "Ada"}), "Ada")

    def test_warns_when_default_name_but_vault_ledger_names_someone(self):
        vault = Path(tempfile.mkdtemp())
        (self.home / "host.json").write_text(json.dumps({"vault": {"path": str(vault)}}))
        self.assertIsNone(preflight._owner_warning(self.home, {}))
        (vault / "Capsule Corp - Ada acts ledger.md").write_text("x")
        self.assertIn("Ada", preflight._owner_warning(self.home, {}))
        self.assertIsNone(preflight._owner_warning(self.home, {"CAPSULE_OWNER_NAME": "Ada"}))
        self.assertIsNone(preflight._owner_warning(Path(tempfile.mkdtemp()), {}))   # no vault


class Parity(unittest.TestCase):
    """The stdlib reader in `human` must give what `host.owner_name` gives, input for input."""

    def test_both_readers_agree_on_every_input(self):
        def nested(v):
            return json.dumps({"owner": {"name": v}})

        docs = [None, nested("Ada"), json.dumps({"owner.name": "Ada"}),
                json.dumps({"owner": {"name": "Ada"}, "owner.name": "Bo"}), "{not json",
                json.dumps([1]), json.dumps({"owner": "Ada"}), nested(5), nested("Zo\u00eb"),
                nested("Ada!"), nested("A\nB"), nested("Ada "), nested("cos"), nested("Opus"),
                nested(""), nested("Ada Lovelace"),
                json.dumps({"repo": 5, "owner": {"name": "Bo"}}),
                "FIFO", "BROKEN_SYMLINK"]
        for doc, env in itertools.product(docs, ("", "Cy", "cos", "Ada!")):
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                if doc == "FIFO":
                    os.mkfifo(home / "host.json")
                elif doc == "BROKEN_SYMLINK":
                    os.symlink(home / "missing.json", home / "host.json")
                elif doc is not None:
                    (home / "host.json").write_text(doc)
                with contextlib.redirect_stderr(io.StringIO()):
                    want = host.owner_name(home, {"CAPSULE_OWNER_NAME": env})
                human._owner_name.cache_clear()
                self.assertEqual(human._owner_name(env, str(home)), want, (doc, env))


class Authors(unittest.TestCase):
    def setUp(self):
        human._owner_name.cache_clear()
        self.addCleanup(human._owner_name.cache_clear)

    def test_cos_env_never_makes_cos_the_owner(self):
        with mock.patch.dict(os.environ, {"CAPSULE_OWNER_NAME": "cos"}):
            self.assertEqual(human.canonical_author("cos"), "cos")

    def test_legacy_and_configured_names_are_owner(self):
        with mock.patch.dict(os.environ, {"CAPSULE_OWNER_NAME": "Ada"}):
            for who in ("owner", human.LEGACY_OWNER, human.LEGACY_OWNER.title(), "ada", "Ada"):
                self.assertEqual(human.canonical_author(who), "owner", who)
        self.assertEqual(human.canonical_author("cos"), "cos")
        self.assertEqual(human.canonical_author("robot"), "robot")

    def stored(self, name, author):
        env = {"CAPSULE_OWNER_NAME": name or "", "CAPSULE_HOME": tempfile.mkdtemp()}
        with mock.patch.dict(os.environ, env):
            human._owner_name.cache_clear()
            path = human.write(Path(tempfile.mkdtemp()), "b", "t_00000001", "unblock", "go",
                               now=5, author=author)
        return json.loads(path.read_text())["author"]

    def test_write_stores_owner_when_nothing_is_configured(self):
        self.assertEqual(self.stored(None, "owner"), "owner")     # nothing configured
        self.assertEqual(self.stored("Alex", "cos"), "cos")

    def test_a_name_the_pinned_schema_does_not_allow_is_stored_as_owner(self):
        self.assertEqual(self.stored("Alice", "owner"), "owner")
        self.assertEqual(self.stored("Alice", human.LEGACY_OWNER), "owner")
        enum = row_schemas.load("intervention.v1")["properties"]["author"]["enum"]
        self.assertIn(human.LEGACY_OWNER, enum)

    def test_host_json_name_is_never_what_a_file_records(self):
        home = Path(tempfile.mkdtemp())
        (home / "host.json").write_text(json.dumps({"owner": {"name": human.LEGACY_OWNER.title()}}))
        with mock.patch.dict(os.environ, {"CAPSULE_HOME": str(home)}):
            os.environ.pop("CAPSULE_OWNER_NAME", None)
            human._owner_name.cache_clear()
            path = human.write(home, "b", "t_00000001", "unblock", "go", now=5, author="owner")
        self.assertEqual(json.loads(path.read_text())["author"], "owner")

    def test_the_legacy_owner_name_is_never_written_again(self):
        legacy = human.LEGACY_OWNER
        self.assertEqual(self.stored(legacy.title(), "owner"), "owner")
        self.assertEqual(self.stored(legacy.title(), legacy), "owner")

    def test_old_files_read_as_owner(self):
        # an old spool file: no author, or the legacy name, is the owner (outcomes reads it so)
        self.assertEqual(human.canonical_author(human.LEGACY_OWNER), "owner")
        self.assertNotEqual(human.canonical_author("cos"), "owner")

    def test_waiting_on_and_assignee_accept_the_configured_name(self):
        from capsule import unblocked
        with mock.patch.dict(os.environ, {"CAPSULE_OWNER_NAME": "Ada"}):
            human._owner_name.cache_clear()
            self.assertEqual(unblocked.evaluate_blocker(
                " ada ", run_status_by_commit={}, allow_owner=True), (True, "waits: owner"))
            self.assertEqual(unblocked.evaluate_blocker(
                "bob", run_status_by_commit={}, allow_owner=True), (False, ""))


class OldReports(unittest.TestCase):
    def test_owner_get_reads_new_then_old_keys(self):
        old, new = {human.LEGACY_OWNER + "_touches_per_card": 2.0}, {"owner_touches_per_card": 1.0}
        self.assertEqual(report.owner_get(old, "touches_per_card"), 2.0)
        self.assertEqual(report.owner_get({**old, **new}, "touches_per_card"), 1.0)
        self.assertIsNone(report.owner_get({}, "touches_per_card"))

    def test_ledger_name_follows_the_owner_name(self):
        with mock.patch.dict(os.environ, {"CAPSULE_OWNER_NAME": "Ada"}):
            self.assertEqual(report.ledger_name(), "Capsule Corp - Ada acts ledger.md")


if __name__ == "__main__":
    unittest.main()
