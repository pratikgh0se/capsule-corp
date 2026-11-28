"""secrets_import's project slugs come from host.json `secrets.projects`."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import secrets_import as si  # noqa: E402


class ProjectSlugs(unittest.TestCase):
    def home(self, doc=None):
        home = tempfile.mkdtemp()
        if doc is not None:
            Path(home, "host.json").write_text(json.dumps(doc))
        patcher = mock.patch.dict(os.environ, {"CAPSULE_HOME": home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_is_only_capsule_corp(self):
        self.home()
        self.assertEqual(si.project_slugs(), ["capsule-corp"])

    def test_host_json_adds_slugs_and_the_bridge_identity_reads_them(self):
        self.home({"secrets": {"projects": ["capsule-corp", "x1", "x2"]}})
        self.assertEqual(si.project_slugs(), ["capsule-corp", "x1", "x2"])
        self.assertEqual(si.identity_projects()["bridge"], ["capsule-corp", "x1", "x2"])
        self.assertEqual(si.identity_projects()["backup"], ["capsule-corp"])


class ProjectsEnvMerge(unittest.TestCase):
    def test_entries_for_unlisted_projects_survive(self):
        d = Path(tempfile.mkdtemp())
        (d / "projects.env").write_text("PROJECT_OLD_ONE=abc\nPROJECT_CAPSULE_CORP=stale\n")
        si.write_projects_env(d, {"capsule-corp": "new", "other-app": "o1"})
        lines = (d / "projects.env").read_text().splitlines()
        self.assertEqual(lines, ["PROJECT_OLD_ONE=abc", "PROJECT_CAPSULE_CORP=new",
                                 "PROJECT_OTHER_APP=o1"])

    def test_no_file_yet_writes_just_the_given_projects(self):
        d = Path(tempfile.mkdtemp())
        si.write_projects_env(d, {"capsule-corp": "p"})
        self.assertEqual((d / "projects.env").read_text(), "PROJECT_CAPSULE_CORP=p\n")


if __name__ == "__main__":
    unittest.main()
