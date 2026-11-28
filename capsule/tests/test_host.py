"""P05a host.json: precedence (env > file > default), refusals, `capsule host get`, and the
`capsule check` host line. Two oracles each: the loader's/CLI's value and the raw file the test
wrote itself. Always a temp CAPSULE_HOME; the three env overrides are scrubbed per case."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import host, host_registry, intent, paths

ENV_KEYS = ("CAPSULE_OWNER_NAME", "CAPSULE_LAUNCHD_PREFIX", "CAPSULE_SECRETS_BACKEND",
            "OBSIDIAN_VAULT_PATH", "CAPSULE_JOBS_DIR")
SECRET = "sk_live_SECRET"     # invalid as a prefix, backend, path and bool


def run_cli(home, *argv, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "capsule", *argv, "--capsule-home", str(home)],
                          cwd=paths.REPO, capture_output=True, text=True, timeout=60, env=env)


class _Home(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.file = self.home / "host.json"
        self.env = {}        # explicit env: no ambient override leaks in

    def write(self, doc):
        if isinstance(doc, bytes):
            self.file.write_bytes(doc)
        else:
            self.file.write_text(doc if isinstance(doc, str) else json.dumps(doc))

    def eff(self):
        return {k: (v, src) for k, v, src in host.effective(self.home, self.env)}

    def refused(self, doc):
        self.write(doc)
        with self.assertRaises(host.HostError, msg=str(doc)) as cm:
            host.effective(self.home, self.env)
        return str(cm.exception)


class ProjectLists(_Home):
    """The public-release keys: private project names live in host.json, never in code."""
    def test_defaults_name_only_this_repo_or_nothing(self):
        e = self.eff()
        self.assertEqual(e["repos"], ({"capsule-corp": str(paths.REPO)}, "default"))
        self.assertEqual(e["quality.repos"], (["capsule-corp"], "default"))
        self.assertEqual(e["secrets.projects"], (["capsule-corp"], "default"))
        self.assertEqual(e["observability.components"], ([], "default"))
        self.assertEqual(e["boards.default"], ("capsule-corp", "default"))
        self.assertEqual(e["maintenance.scan_repos"], ([], "default"))

    def test_configured_values_win_and_paths_expand(self):
        self.write({"repos": {"a": "~/x/a", "b": "/srv/b"}, "quality": {"repos": ["a", "b"]},
                    "secrets": {"projects": ["p"]}, "observability": {"components": ["a"]},
                    "boards": {"default": "a"}, "maintenance": {"scan_repos": ["b"]}})
        e = self.eff()
        self.assertEqual(e["repos"][0], {"a": os.path.expanduser("~/x/a"), "b": "/srv/b"})
        self.assertEqual(e["quality.repos"], (["a", "b"], "host.json"))
        self.assertEqual(e["secrets.projects"][0], ["p"])
        self.assertEqual(e["maintenance.scan_repos"][0], ["b"])
        self.assertEqual(e["boards.default"][0], "a")

    def test_get_returns_a_copy_of_a_list_default(self):
        host.get("quality.repos", self.home, self.env).append("junk")
        host.get("repos", self.home, self.env)["junk"] = "/x"
        self.assertEqual(host.get("quality.repos", self.home, self.env), ["capsule-corp"])
        self.assertNotIn("junk", host.get("repos", self.home, self.env))

    def test_names_and_map_keys_must_be_plain_names(self):
        for bad in ("a b", "*", "../x", "a/b", "", "-x", "a" * 65):
            self.assertIn("must be", self.refused({"quality": {"repos": [bad]}}), bad)
            self.assertIn("must be", self.refused({"repos": {bad: "/x"}}), bad)
        self.write({"quality": {"repos": ["my-app_2.0", "A"]}})
        self.assertEqual(self.eff()["quality.repos"][0], ["my-app_2.0", "A"])

    def test_get_or_default_ignores_other_keys_and_names_the_bad_one(self):
        self.write({"vault": {"path": 5}, "quality": {"repos": ["a"]}})
        self.assertEqual(host.get_or_default("quality.repos", self.home, self.env), ["a"])
        self.write({"quality": {"repos": SECRET}})
        r = run_cli(self.home, "host", "get", "quality.repos", "--only")
        self.assertEqual((r.returncode, SECRET in r.stderr, "quality.repos" in r.stderr),
                         (1, False, True))

    def test_bad_shapes_are_refused_without_echoing_values(self):
        for doc in ({"quality": {"repos": "a"}}, {"quality": {"repos": [""]}},
                    {"repos": {"a": "relative"}}, {"repos": ["a"]}):
            self.assertIn("must be", self.refused(doc))
        self.assertNotIn(SECRET, self.refused({"repos": {"a": SECRET}}))

    def test_cli_prints_a_list_space_separated_and_a_map_as_lines(self):
        self.write({"maintenance": {"scan_repos": ["a", "b"]}, "repos": {"a": "/x/a"}})
        r = run_cli(self.home, "host", "get", "maintenance.scan_repos")
        self.assertEqual((r.returncode, r.stdout), (0, "a b\n"))
        self.assertEqual(run_cli(self.home, "host", "get", "repos").stdout, "a\t/x/a\n")

    def test_registry_joins_repos_urls_and_served_boards(self):
        reg = self.home / "repos.json"
        reg.write_text(json.dumps([{"name": "a", "board": "a"}]))
        os.environ["AGENT_OPS_REPOS_JSON"] = str(reg)
        self.addCleanup(os.environ.pop, "AGENT_OPS_REPOS_JSON", None)
        self.write({"repos": {"b": "/x/b", "a": "/x/a"},
                    "repo_urls": {"a": "https://example.test/a.git"}})
        self.assertEqual(host_registry.registry(self.home, self.env),
                         [{"name": "a", "path": "/x/a", "url": "https://example.test/a.git",
                           "board": "a"},
                          {"name": "b", "path": "/x/b", "url": None, "board": None}])

    def test_repo_urls_must_be_urls_and_never_echo_the_value(self):
        for doc in ({"repo_urls": {"a": "not a url"}}, {"repo_urls": ["x"]},
                    {"repo_urls": {"a b": "https://example.test/a.git"}}):
            self.assertIn("must be", self.refused(doc))
        self.assertNotIn(SECRET, self.refused({"repo_urls": {"a": SECRET}}))

    def test_credential_or_shell_urls_are_refused_without_echoing_them(self):
        for bad in ("https://user:" + SECRET + "@example.test/a.git", "https://example.test/a;rm",
                    "git@example.test:a b", "https://example.test/$(x)",
                    "ssh://u:p@example.test/a"):
            msg = self.refused({"repo_urls": {"a": bad}})
            self.assertIn("repo_urls", msg)
            self.assertNotIn(SECRET, msg)
        for good in ("https://example.test:8443/o/a.git", "git@example.test:o/a.git",
                     "ssh://git@example.test/o/a.git"):
            self.write({"repo_urls": {"a": good}})
            self.assertEqual(host.get("repo_urls", self.home, self.env), {"a": good})

    def test_a_repos_json_serves_only_the_boards_it_names(self):
        reg = self.home / "repos.json"
        rows = [{"name": "capsule-corp", "board": "capsule-corp"},
                {"name": "my-app", "board": "my-app"}, {"name": "read-only", "board": None}]
        reg.write_text(json.dumps(rows), encoding="utf-8")
        boards = host_registry.boards_served(self.home, reg)
        self.assertEqual(boards, ["capsule-corp", "my-app"])

    def test_boards_served_prefers_repos_json_then_config_then_default(self):
        reg = self.home / "repos.json"
        # no file/config
        self.assertEqual(host_registry.boards_served(self.home, reg), ["capsule-corp"])
        (self.home / "config.json").write_text(json.dumps({"unblocked": {"boards": ["q", "b"]}}))
        self.assertEqual(host_registry.boards_served(self.home, reg), ["b", "q"])  # config
        reg.write_text(json.dumps([{"name": "z", "board": "z"}, {"name": "a", "board": "a"},
                                   {"name": "ro", "board": None}]))
        # the file wins
        self.assertEqual(host_registry.boards_served(self.home, reg), ["a", "z"])
        self.write({"repos": {"a": "/x/a", "ro": "/x/ro"}})
        os.environ["AGENT_OPS_REPOS_JSON"] = str(reg)
        self.addCleanup(os.environ.pop, "AGENT_OPS_REPOS_JSON", None)
        rows = host_registry.registry(self.home, self.env)
        self.assertEqual({r["name"]: r["board"] for r in rows}, {"a": "a", "ro": None})

    def test_check_row_shows_names_not_paths(self):
        self.write({"repos": {"a": "/secret/place"}})
        detail = host.check_row(self.home, self.env)[2]
        self.assertIn("repos=a", detail)
        self.assertNotIn("/secret/place", detail)


class GetOneKey(_Home):
    def test_a_bad_other_env_does_not_break_get(self):
        self.write({"secrets": {"backend": "env"}})
        self.env = {"CAPSULE_LAUNCHD_PREFIX": "bad prefix!", "OBSIDIAN_VAULT_PATH": "rel"}
        self.assertEqual(host.get("secrets.backend", self.home, self.env), "env")
        with self.assertRaises(host.HostError):
            host.effective(self.home, self.env)

    def test_get_still_refuses_bad_json_and_a_bad_requested_env(self):
        self.write("{bad")
        with self.assertRaises(host.HostError):
            host.get("secrets.backend", self.home, self.env)
        self.write({})
        self.env = {"CAPSULE_SECRETS_BACKEND": "File"}
        with self.assertRaises(host.HostError):
            host.get("secrets.backend", self.home, self.env)


class DecisionNote(_Home):
    def test_a_bad_glob_in_host_json_never_raises_and_gives_the_default(self):
        vault = self.home / "v"
        (vault / "Decisions").mkdir(parents=True)
        (vault / "Decisions" / "my notes.md").write_text("x")
        self.write({"vault": {"decision_notes": "Decisions/*notes*.md"}})
        self.assertEqual(host.decision_note(vault, self.home, self.env), "Decisions/my notes.md")
        self.write({"vault": {"decision_notes": "/Decisions/*.md"}})
        self.assertEqual(host.decision_note(vault, self.home, self.env), host.DEFAULT_DECISION_NOTE)

    def test_a_glob_the_filesystem_cannot_search_gives_the_default(self):
        vault = self.home / "v"
        vault.mkdir()
        with mock.patch.object(Path, "glob", side_effect=NotImplementedError("x")):
            got = host.decision_note(vault, self.home, self.env)
        self.assertEqual(got, host.DEFAULT_DECISION_NOTE)


class DanglingLink(_Home):
    def test_a_link_to_a_missing_file_is_refused_not_ignored(self):
        self.file.symlink_to(self.home / "nowhere.json")
        with self.assertRaises(host.HostError):
            host.get("secrets.backend", self.home, self.env)


class Loader(_Home):
    def test_missing_file_is_all_defaults(self):
        self.assertFalse(self.file.exists())          # oracle 2: no file at all
        eff = self.eff()
        self.assertEqual(eff["launchd.label_prefix"], (None, "default"))   # auto: not claimed
        self.assertEqual(eff["secrets.backend"], (None, "default"))
        self.assertEqual(eff["vault.path"], (None, "default"))
        self.assertEqual(eff["observability.enabled"], (True, "default"))
        self.assertEqual(eff["tailscale.serve"], (False, "default"))
        self.assertEqual(eff["repo"], (str(paths.REPO), "default"))

    def test_file_beats_default(self):
        doc = {"launchd": {"label_prefix": "com.example"}, "secrets": {"backend": "env"},
               "vault": {"path": "/some/vault", "decision_notes": "Decisions/*log*.md"},
               "observability": {"enabled": False}, "tailscale": {"serve": True},
               "repo": "/r/capsule"}
        self.write(doc)
        raw = json.loads(self.file.read_text())      # oracle 2: the raw file
        eff = self.eff()
        self.assertEqual(eff["launchd.label_prefix"], (raw["launchd"]["label_prefix"], "host.json"))
        self.assertEqual(eff["secrets.backend"], (raw["secrets"]["backend"], "host.json"))
        self.assertEqual(eff["vault.path"], (raw["vault"]["path"], "host.json"))
        self.assertEqual(eff["vault.decision_notes"],
                         (raw["vault"]["decision_notes"], "host.json"))
        self.assertEqual(eff["observability.enabled"], (False, "host.json"))
        self.assertEqual(eff["tailscale.serve"], (True, "host.json"))
        self.assertEqual(eff["repo"], ("/r/capsule", "host.json"))

    def test_env_beats_file(self):
        self.write({"launchd": {"label_prefix": "com.file"}, "vault": {"path": "/file/vault"}})
        self.env = {"CAPSULE_LAUNCHD_PREFIX": "com.env", "OBSIDIAN_VAULT_PATH": "/env/vault"}
        eff = self.eff()
        self.assertEqual(eff["launchd.label_prefix"], ("com.env", "env CAPSULE_LAUNCHD_PREFIX"))
        self.assertEqual(eff["vault.path"], ("/env/vault", "env OBSIDIAN_VAULT_PATH"))
        self.assertEqual(json.loads(self.file.read_text())["launchd"]["label_prefix"], "com.file")

    def test_empty_env_is_unset(self):
        self.write({"secrets": {"backend": "infisical"}})
        self.env = {"CAPSULE_SECRETS_BACKEND": ""}
        self.assertEqual(self.eff()["secrets.backend"], ("infisical", "host.json"))

    def test_null_vault_is_none_and_tilde_is_expanded(self):
        self.write({"vault": {"path": None}, "repo": "~/CodeBase/capsule-corp"})
        self.assertIsNone(host.get("vault.path", self.home, self.env))
        self.assertEqual(host.get("repo", self.home, self.env),
                         os.path.expanduser("~/CodeBase/capsule-corp"))

    def test_flat_dotted_keys_are_accepted_in_the_file(self):
        self.write({"secrets.backend": "env"})
        self.assertEqual(host.get("secrets.backend", self.home, self.env), "env")

    def test_refusals(self):
        for doc, why in (({"nope": 1}, "unknown key nope"),
                         ({"vault": {"nope": 1}}, "unknown key vault.nope"),
                         ({"launchd": {"label_prefix": "bad prefix"}}, "label_prefix must be"),
                         ({"secrets": {"backend": "vault"}}, "file or infisical or env"),
                         ({"observability": {"enabled": "yes"}}, "true or false"),
                         ({"vault": {"path": 5}}, "vault.path must be an absolute path"),
                         ({"repo": "relative/dir"}, "repo must be an absolute path"),
                         ({"vault": {"path": "vault"}}, "vault.path must be an absolute path"),
                         ({"vault": {"decision_notes": ""}}, "non-empty relative glob"),
                         ({"vault": {"decision_notes": "/Decisions/*.md"}}, "relative glob"),
                         ({"vault": {"decision_notes": "~/x/*.md"}}, "relative glob"),
                         ([1, 2], "must be a JSON object"),
                         ("{not json", "not valid JSON")):
            self.assertIn(why, self.refused(doc))

    def test_a_null_or_scalar_section_is_refused_clearly(self):
        self.assertIn("vault must be an object, got null", self.refused({"vault": None}))
        self.assertIn("launchd must be an object, got a str", self.refused({"launchd": "x"}))

    def test_a_key_given_nested_and_flat_is_refused(self):
        msg = self.refused({"secrets": {"backend": "file"}, "secrets.backend": "env"})
        self.assertIn("secrets.backend is set twice", msg)

    def test_a_non_utf8_file_is_a_clean_refusal(self):
        self.assertIn("not UTF-8 text", self.refused(b"\xff\xfe{}"))
        self.assertEqual(host.check_row(self.home, self.env)[:2], ("FAIL", "host"))

    def test_a_refusal_never_echoes_a_string_value(self):
        for doc in ({"secrets": {"backend": SECRET}}, {"launchd": {"label_prefix": SECRET}},
                    {"repo": SECRET}, {"vault": {"decision_notes": ""}, "repo": [SECRET]},
                    {"observability": {"enabled": SECRET}}):
            msg = self.refused(doc)
            self.assertNotIn(SECRET, msg)
            self.assertNotIn(SECRET, host.check_row(self.home, self.env)[2])
        self.assertIn("got a str", self.refused({"secrets": {"backend": SECRET}}))

    def test_a_bad_env_value_names_the_variable_not_the_value(self):
        self.env = {"CAPSULE_SECRETS_BACKEND": SECRET}
        with self.assertRaises(host.HostError) as cm:
            host.effective(self.home, self.env)
        self.assertIn("env CAPSULE_SECRETS_BACKEND", str(cm.exception))
        self.assertNotIn(SECRET, str(cm.exception))

    def test_unknown_key_in_get_is_a_key_error(self):
        with self.assertRaises(KeyError):
            host.get("nope", self.home, self.env)

    def test_the_schema_holds_names_and_paths_not_secrets(self):
        words = ("token", "password", "key")
        self.assertFalse([k for k in host.KEYS if any(w in k for w in words)])


class PrefixParity(unittest.TestCase):
    """Z2: bin/lib_prefix.sh and host.PREFIX_RE accept and refuse the same ASCII prefixes."""

    def shell_accepts(self, prefix):
        script = f'. "{paths.REPO}/bin/lib_prefix.sh"; capsule_launchd_prefix'
        env = {**os.environ, "LC_ALL": "C", "CAPSULE_LAUNCHD_PREFIX": prefix}
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env,
                           timeout=30)
        return r.returncode == 0

    def test_same_accept_refuse_as_the_shell(self):
        for prefix in ("x.y", "a b", "a_b", "a/b", "-", "..", "dev.capsule", "dev.capsule", "A-1.z",
                       "a;b", "a\tb", "a$b"):
            self.assertEqual(host.PREFIX_RE.fullmatch(prefix) is not None,
                             self.shell_accepts(prefix), prefix)


class HostGetCli(_Home):
    def test_prefix_and_backend_with_no_file_print_an_empty_line(self):
        for key in ("launchd.label_prefix", "secrets.backend"):
            r = run_cli(self.home, "host", "get", key)
            self.assertEqual((r.returncode, r.stdout), (0, "\n"), r.stderr)
        self.assertFalse(self.file.exists())

    def test_prints_the_file_value_and_lowercase_booleans(self):
        self.write({"launchd": {"label_prefix": "dev.capsule"}, "tailscale": {"serve": True}})
        self.assertEqual(run_cli(self.home, "host", "get", "launchd.label_prefix").stdout,
                         "dev.capsule\n")
        self.assertEqual(run_cli(self.home, "host", "get", "tailscale.serve").stdout, "true\n")

    def test_the_default_of_a_plain_key_is_printed(self):
        r = run_cli(self.home, "host", "get", "observability.enabled")
        self.assertEqual((r.returncode, r.stdout), (0, "true\n"), r.stderr)

    def test_null_vault_prints_an_empty_line(self):
        r = run_cli(self.home, "host", "get", "vault.path")
        self.assertEqual((r.returncode, r.stdout), (0, "\n"), r.stderr)

    def test_unknown_key_exits_2_and_a_bad_file_exits_1(self):
        r = run_cli(self.home, "host", "get", "nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown key 'nope'", r.stderr)
        self.write({"surprise": 1})
        r = run_cli(self.home, "host", "get", "repo")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown key surprise", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_a_fifo_or_directory_host_json_is_refused_without_reading(self):
        path = self.home / "host.json"
        os.mkfifo(path)
        for only in (False, True):
            with self.assertRaisesRegex(host.HostError, "not a regular file"):
                host.get("secrets.backend", self.home, self.env, only=only)
        self.assertEqual(host.check_row(self.home, self.env)[:2], ("FAIL", "host"))
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(host.HostError, "not a regular file"):
            host.get("secrets.backend", self.home, self.env)

    def test_only_refuses_a_non_object_section_like_the_full_reader(self):
        for doc in ({"secrets": "infisical"}, {"secrets": None}, {"secrets": ["env"]}):
            self.write(doc)
            r = run_cli(self.home, "host", "get", "secrets.backend", "--only")
            self.assertEqual((r.returncode, r.stdout), (1, ""), doc)
            self.assertIn("secrets must be an object", r.stderr)

    def test_only_ignores_other_keys_but_not_the_file_or_its_own_key(self):
        self.write({"surprise": 1, "vault": 5, "secrets": {"backend": "env"}})
        r = run_cli(self.home, "host", "get", "secrets.backend", "--only")
        self.assertEqual((r.returncode, r.stdout), (0, "env\n"), r.stderr)
        self.write({"surprise": 1})
        r = run_cli(self.home, "host", "get", "secrets.backend", "--only")
        self.assertEqual((r.returncode, r.stdout), (0, "\n"), r.stderr)
        for doc in ({"secrets": {"backend": "vault"}}, [1], "{not json",
                    {"secrets": {"backend": "env"}, "secrets.backend": "file"}):
            self.write(doc)
            r = run_cli(self.home, "host", "get", "secrets.backend", "--only")
            self.assertEqual((r.returncode, r.stdout), (1, ""), doc)


class CheckLine(_Home):
    def host_line(self, **kw):
        r = run_cli(self.home, "check", **kw)
        lines = [ln for ln in r.stdout.splitlines() if ln.split(" ", 1)[-1].startswith("host:")]
        self.assertEqual(len(lines), 1, r.stdout)
        return lines[0]

    def test_line_shows_every_value_with_its_source(self):
        self.write({"launchd": {"label_prefix": "dev.capsule"}, "vault": {"path": None}})
        line = self.host_line()
        raw = json.loads(self.file.read_text())           # oracle 2: the raw file
        self.assertTrue(line.startswith("ok host:"), line)
        self.assertIn(f"launchd.label_prefix={raw['launchd']['label_prefix']} (host.json)", line)
        self.assertIn("secrets.backend=auto (default)", line)
        self.assertIn("observability.enabled=true (default)", line)
        self.assertIn("vault.decision_notes=Decisions/*decisions*.md (default)", line)

    def test_line_never_prints_the_vault_path_repo_path_or_note(self):
        vault = self.home / "private-vault-dir"
        vault.mkdir()
        self.write({"vault": {"path": str(vault)},
                    "repo": "/home/someone/secret-area/capsule-x"})
        line = self.host_line()
        for leak in ("private-vault-dir", "/home/someone"):
            self.assertNotIn(leak, line)
        self.assertIn("vault configured=yes, directory exists (host.json)", line)
        self.assertIn("repo=capsule-x (host.json)", line)
        skill = self.home / "private-skill-dir"
        skill.mkdir()
        self.write({"vault": {"path": str(vault), "skill_root": str(skill),
                              "decision_notes": "Decisions/Private Note*.md"}})
        line = self.host_line()
        for leak in ("private-skill-dir", str(self.home), "Private Note"):
            self.assertNotIn(leak, line)
        self.assertIn("vault skill_root configured=yes, directory exists (host.json)", line)
        self.assertIn("vault.decision_notes=custom (host.json)", line)
        self.write({"vault": {"path": str(self.home / "gone")}})
        self.assertIn("vault configured=yes, directory missing", self.host_line())
        self.write({})
        self.assertIn("vault configured=no (default)", self.host_line())

    def test_no_file_shows_auto_not_a_claimed_value(self):
        line = self.host_line()
        self.assertIn("launchd.label_prefix=auto (default)", line)
        self.assertNotIn("dev.capsule", line)

    def test_env_source_is_named(self):
        line = self.host_line(extra_env={"CAPSULE_SECRETS_BACKEND": "env"})
        self.assertIn("secrets.backend=env (env CAPSULE_SECRETS_BACKEND)", line)

    def test_a_bad_file_fails_the_line_and_the_exit_without_the_value(self):
        self.write({"secrets": {"backend": SECRET}})
        line = self.host_line()
        self.assertTrue(line.startswith("FAIL host:"))
        self.assertNotIn(SECRET, line)
        self.assertEqual(run_cli(self.home, "check").returncode, 1)


# Z1: the handlers behind contracts/intent/host_config.jsonl. Each case is a host.json (or none),
# an env and what the operator then sees from `host get` and `capsule check`.
CASES = {
    "missing-host-json-keeps-prefix-and-backend-on-auto": (
        {"host_json": "absent"},
        {"prefix_get": "", "backend_get": "", "check_says_auto": True, "check_host_row": "ok"}),
    "typo-or-bad-value-fails-loudly-naming-the-key": (
        {"host_json": {"launchd": {"label_prefx": "com.example"}}, "names": "label_prefx"},
        {"check_host_row": "FAIL", "check_names_key": True, "get_exit": 1, "get_names_key": True}),
    "env-beats-file-and-empty-env-is-unset": (
        {"host_json": {"launchd": {"label_prefix": "com.file"},
                       "secrets": {"backend": "infisical"}},
         "env": {"CAPSULE_LAUNCHD_PREFIX": "com.env", "CAPSULE_SECRETS_BACKEND": ""}},
        {"prefix_get": "com.env", "backend_get": "infisical"}),
    "project-lists-default-to-this-repo-or-nothing": (
        {"host_json": "absent"},
        {"quality_repos_get": "capsule-corp", "secrets_projects_get": "capsule-corp",
         "scan_repos_get": "", "components_get": "", "repos_get_names": "capsule-corp",
         "repo_urls_get": ""}),
    "list-get-space-separated-check-shows-names-not-paths": (
        {"host_json": {"maintenance": {"scan_repos": ["a", "b"]},
                       "repos": {"a": "/private/path/a"}}},
        {"scan_repos_get": "a b", "check_names_repos": True, "check_shows_paths": False}),
    "list-key-wrong-type-refused-naming-key-no-value": (
        {"host_json": {"quality": {"repos": "SECRETVALUE"}}, "names": "quality.repos",
         "value": "SECRETVALUE"},
        {"list_get_exit": 1, "list_err_names_key": True, "list_err_echoes_value": False}),
    "repo-urls-default-to-nothing": (
        {"host_json": {"repos": {"a": "/private/path/a"}}},
        {"repo_urls_get": "", "repos_get_names": "a"}),
    "repo-urls-bad-value-refused-naming-key-no-value": (
        {"host_json": {"repo_urls": {"a": "SECRETVALUE"}}, "names": "repo_urls",
         "value": "SECRETVALUE"},
        {"repo_urls_get_exit": 1, "repo_urls_err_names_key": True,
         "repo_urls_err_echoes_value": False}),
}
BAD_VALUE = ({"host_json": {"secrets": {"backend": "infisicl"}}, "names": "secrets.backend"},
             {"check_host_row": "FAIL", "check_names_key": True, "get_exit": 1,
              "get_names_key": True})


class JobsConfigDir(_Home):
    """`config_dir`: the jobs' runtime dir. env > host.json > ~/.config/capsule-corp."""

    def test_default_is_under_the_home_and_never_the_old_name(self):
        with mock.patch.dict(os.environ, {"HOME": "/h"}):
            self.assertEqual(host.config_dir(self.home, self.env),
                             Path("/h/.config/capsule-corp"))

    def test_host_json_beats_default_and_env_beats_host_json(self):
        self.write({"config_dir": "/cfg/file"})
        self.assertEqual(host.config_dir(self.home, self.env), Path("/cfg/file"))
        self.env["CAPSULE_JOBS_DIR"] = "/cfg/env"
        self.assertEqual(host.config_dir(self.home, self.env), Path("/cfg/env"))

    def test_a_bad_value_gives_the_default_not_a_crash(self):
        self.write({"config_dir": "relative/dir"})
        with mock.patch.dict(os.environ, {"HOME": "/h"}):
            self.assertEqual(host.config_dir(self.home, self.env), Path("/h/.config/capsule-corp"))

    def test_cli_get_only_reads_the_top_level_key_for_shell_callers(self):
        self.write({"config_dir": "/cfg/file", "vault": {"path": "/v"}})
        proc = run_cli(self.home, "host", "get", "--only", "config_dir")
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "/cfg/file"))
        self.assertEqual(json.loads(self.file.read_text())["config_dir"], "/cfg/file")   # raw file

    def test_check_row_shows_set_or_default_never_the_path(self):
        self.write({"config_dir": "/cfg/private-name"})
        line = host.check_row(self.home, self.env)[2]
        self.assertIn("config_dir=set (host.json)", line)
        self.assertNotIn("private-name", line)


class IntentCases(unittest.TestCase):
    def observe(self, document):
        home = Path(tempfile.mkdtemp())
        if document["host_json"] != "absent":
            (home / "host.json").write_text(json.dumps(document["host_json"]))
        env = document.get("env")
        names = document.get("names", "")
        get = {k: run_cli(home, "host", "get", k, extra_env=env)
               for k in ("launchd.label_prefix", "secrets.backend")}
        check = run_cli(home, "check", extra_env=env)
        line = next(ln for ln in check.stdout.splitlines() if " host:" in f" {ln}")
        lists = {k: run_cli(home, "host", "get", k, extra_env=env) for k in (
            "quality.repos", "secrets.projects", "maintenance.scan_repos",
            "observability.components", "repos", "repo_urls")}
        err = lists["quality.repos"].stderr
        value = document.get("value", "")
        return {"quality_repos_get": lists["quality.repos"].stdout.strip(),
                "secrets_projects_get": lists["secrets.projects"].stdout.strip(),
                "scan_repos_get": lists["maintenance.scan_repos"].stdout.strip(),
                "components_get": lists["observability.components"].stdout.strip(),
                "repos_get_names": " ".join(ln.split("\t")[0] for ln in
                                            lists["repos"].stdout.splitlines()),
                "repo_urls_get": lists["repo_urls"].stdout.strip(),
                "repo_urls_get_exit": lists["repo_urls"].returncode,
                "repo_urls_err_names_key": bool(names) and names in lists["repo_urls"].stderr,
                "repo_urls_err_echoes_value": bool(value) and value in lists["repo_urls"].stderr,
                "check_names_repos": "repos=a " in line + " ",
                "check_shows_paths": "/private/path" in line,
                "list_get_exit": lists["quality.repos"].returncode,
                "list_err_names_key": bool(names) and names in err,
                "list_err_echoes_value": bool(value) and value in err,
                "prefix_get": get["launchd.label_prefix"].stdout.strip(),
                "backend_get": get["secrets.backend"].stdout.strip(),
                "get_exit": max(r.returncode for r in get.values()),
                "get_names_key": bool(names) and names in get["secrets.backend"].stderr + get[
                    "launchd.label_prefix"].stderr,
                "check_host_row": line.split(" ")[0],
                "check_names_key": bool(names) and names in line,
                "check_says_auto": "launchd.label_prefix=auto (default)" in line
                and "secrets.backend=auto (default)" in line}

    def assert_case(self, name, document, want):
        got = self.observe(document)
        self.assertTrue(intent.matches(want, got), f"{name}: wanted {want}, got {got}")

    def test_the_built_in_expectations(self):
        for name, (document, want) in CASES.items():
            self.assert_case(name, document, want)
        self.assert_case("bad-value", *BAD_VALUE)

    def test_the_shipped_rows(self):
        if not intent.role_path("host_config").is_file():
            self.skipTest("contracts/intent/host_config.jsonl is not shipped yet")
        rows = intent.load("host_config")
        # a row for each built-in case, and each row's expectation holds
        self.assertEqual({r["id"] for r in rows}, set(CASES))
        for row in rows:
            self.assert_case(row["id"], row["given"]["document"], row["expect"])


if __name__ == "__main__":
    unittest.main()
