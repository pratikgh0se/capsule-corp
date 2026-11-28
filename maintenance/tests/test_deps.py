import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deps


class ParseAllowlistGoodShapes(unittest.TestCase):
    def test_pip_and_npm_lines_parse(self):
        text = "pip:httpx==0.27.2\nnpm:vitest@2.1.1\n"
        self.assertEqual(deps.parse_allowlist(text), ["pip:httpx==0.27.2", "npm:vitest@2.1.1"])

    def test_comments_and_blank_lines_are_skipped(self):
        text = "# a header comment\n\npip:httpx==0.27.2\n  # indented comment\n\n"
        self.assertEqual(deps.parse_allowlist(text), ["pip:httpx==0.27.2"])

    def test_duplicates_collapse_to_first_occurrence(self):
        text = "pip:httpx==0.27.2\nnpm:vitest@2.1.1\npip:httpx==0.27.2\n"
        self.assertEqual(deps.parse_allowlist(text), ["pip:httpx==0.27.2", "npm:vitest@2.1.1"])

    def test_order_is_preserved(self):
        text = "npm:vitest@2.1.1\npip:httpx==0.27.2\n"
        self.assertEqual(deps.parse_allowlist(text), ["npm:vitest@2.1.1", "pip:httpx==0.27.2"])

    def test_empty_text_is_empty_list(self):
        self.assertEqual(deps.parse_allowlist(""), [])

    def test_scoped_npm_name(self):
        # fix wave 1 MED-1
        text = "npm:@types/node@20.1.0\nnpm:@vitest/ui@2.1.1\n"
        self.assertEqual(deps.parse_allowlist(text), ["npm:@types/node@20.1.0", "npm:@vitest/ui@2.1.1"])


class ParseAllowlistBadShapes(unittest.TestCase):
    def _line_no(self, text):
        with self.assertRaises(deps.AllowlistError) as ctx:
            deps.parse_allowlist(text)
        return ctx.exception.line_no

    def test_range_operator_ge(self):
        self.assertEqual(self._line_no("pip:httpx>=0.27.2\n"), 1)

    def test_range_operator_tilde_eq(self):
        self.assertEqual(self._line_no("pip:httpx~=0.27\n"), 1)

    def test_range_operator_caret(self):
        self.assertEqual(self._line_no("npm:vitest^2.1.1\n"), 1)

    def test_bare_name_no_version(self):
        self.assertEqual(self._line_no("pip:httpx\n"), 1)

    def test_leading_whitespace_inside_spec(self):
        self.assertEqual(self._line_no("pip: httpx==0.27.2\n"), 1)

    def test_trailing_whitespace_inside_spec(self):
        self.assertEqual(self._line_no("pip:httpx==0.27.2 extra\n"), 1)

    def test_unknown_manager_prefix(self):
        self.assertEqual(self._line_no("conda:httpx==0.27.2\n"), 1)

    def test_url_line(self):
        self.assertEqual(self._line_no("pip:https://example.com/httpx-0.27.2.tar.gz\n"), 1)

    def test_editable_line(self):
        self.assertEqual(self._line_no("pip:-e ./local-pkg\n"), 1)

    def test_line_number_is_1_based_and_points_at_the_bad_line(self):
        text = "pip:httpx==0.27.2\nnpm:vitest^2.1.1\n"
        self.assertEqual(self._line_no(text), 2)

    # fix wave 1 MED-2: a leading '-' reaches pip/npm as an option flag
    # (option injection), not a package name.
    def test_pip_name_starting_with_dash(self):
        self.assertEqual(self._line_no("pip:-rfoo==1.0\n"), 1)

    def test_pip_option_flag_as_name(self):
        self.assertEqual(self._line_no("pip:--config-settings==1\n"), 1)

    def test_npm_name_starting_with_dash(self):
        self.assertEqual(self._line_no("npm:-g@1.0\n"), 1)

    def test_pip_version_starting_with_dash(self):
        self.assertEqual(self._line_no("pip:httpx==-0.27.2\n"), 1)

    def test_path_traversal_name_is_refused(self):
        self.assertEqual(self._line_no("pip:../../evil==1.0\n"), 1)

    def test_scoped_npm_name_starting_with_dash_after_the_slash(self):
        self.assertEqual(self._line_no("npm:@scope/-evil@1.0\n"), 1)

    # fix wave 1 LOW: an extra passes every other check but is silently
    # unusable under --no-deps.
    def test_pip_extra_is_refused(self):
        self.assertEqual(self._line_no("pip:httpx[socks]==0.27.2\n"), 1)


class ContainerEnv(unittest.TestCase):
    def test_exact_seven_keys(self):
        self.assertEqual(deps.CONTAINER_ENV, {
            "PIP_NO_INDEX": "1",
            "PIP_FIND_LINKS": "/deps/pip",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "npm_config_cache": "/deps/npm",
            "npm_config_offline": "true",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
        })


class CacheRoot(unittest.TestCase):
    def test_default_is_under_home(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_OPS_DEPS_CACHE", None)
            self.assertEqual(deps.cache_root(), Path.home() / ".local" / "share" / "capsule-corp" / "deps")

    def test_honours_override(self):
        with unittest.mock.patch.dict(os.environ, {"AGENT_OPS_DEPS_CACHE": "/tmp/somewhere"}):
            self.assertEqual(deps.cache_root(), Path("/tmp/somewhere"))


class DockerFlags(unittest.TestCase):
    def test_mount_and_env_flags(self):
        with unittest.mock.patch.dict(os.environ, {"AGENT_OPS_DEPS_CACHE": "/tmp/cache-x"}):
            flags = deps.docker_flags()
        self.assertEqual(flags[0:2], ["-v", "/tmp/cache-x:/deps:ro"])
        # one -e KEY=VAL pair per CONTAINER_ENV key, in order
        rest = flags[2:]
        pairs = [rest[i:i + 2] for i in range(0, len(rest), 2)]
        self.assertEqual(len(pairs), len(deps.CONTAINER_ENV))
        for (flag, kv), (k, v) in zip(pairs, deps.CONTAINER_ENV.items()):
            self.assertEqual(flag, "-e")
            self.assertEqual(kv, f"{k}={v}")


class AllowlistsForRepos(unittest.TestCase):
    def test_missing_registry_file_is_empty_list(self):
        self.assertEqual(list(deps.allowlists_for_repos("/nonexistent/repos.json")), [])

    def test_reads_each_repo_allowlist(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            repo_a = t / "repo-a"; repo_a.mkdir()
            (repo_a / "deps-allowlist.txt").write_text("pip:httpx==0.27.2\n")
            repo_b = t / "repo-b"; repo_b.mkdir()
            # repo-b has no allowlist file at all - skipped silently
            repos_json = t / "repos.json"
            repos_json.write_text(json.dumps([
                {"name": "repo-a", "path": str(repo_a)},
                {"name": "repo-b", "path": str(repo_b)},
            ]))
            got = list(deps.allowlists_for_repos(str(repos_json)))
            self.assertEqual(len(got), 1)
            name, path, entries = got[0]
            self.assertEqual(name, "repo-a")
            self.assertEqual(path, repo_a)
            self.assertEqual(entries, ["pip:httpx==0.27.2"])

    def test_a_present_malformed_allowlist_raises(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            repo_a = t / "repo-a"; repo_a.mkdir()
            (repo_a / "deps-allowlist.txt").write_text("pip:httpx>=0.27.2\n")
            repos_json = t / "repos.json"
            repos_json.write_text(json.dumps([{"name": "repo-a", "path": str(repo_a)}]))
            with self.assertRaises(deps.AllowlistError):
                list(deps.allowlists_for_repos(str(repos_json)))

    def test_malformed_repos_json_is_empty_list(self):
        with tempfile.TemporaryDirectory() as t:
            repos_json = Path(t) / "repos.json"
            repos_json.write_text("not json")
            self.assertEqual(list(deps.allowlists_for_repos(str(repos_json))), [])


if __name__ == "__main__":
    unittest.main()
