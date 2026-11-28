"""quality_nightly's default repo rotation comes from host.json `quality.repos`."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quality_nightly as qn  # noqa: E402


class DefaultRepos(unittest.TestCase):
    def rotation(self, home):
        """The repo list main() hands to next_repo when --repos is not given."""
        seen = []
        env = {"CAPSULE_HOME": home, "MERGE_GATE_STATE": tempfile.mkdtemp()}
        with mock.patch.dict(os.environ, env), mock.patch.object(
                qn, "next_repo", side_effect=lambda repos, state: seen.append(repos)):
            qn.main(["--dry-run"])
        return list(seen[0])

    def test_no_host_json_means_only_capsule_corp(self):
        self.assertEqual(self.rotation(tempfile.mkdtemp()), ["capsule-corp"])

    def test_host_json_sets_the_rotation(self):
        home = tempfile.mkdtemp()
        Path(home, "host.json").write_text(json.dumps({"quality": {"repos": ["a", "b"]}}))
        self.assertEqual(self.rotation(home), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
