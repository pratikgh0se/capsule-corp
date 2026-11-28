# mutation-check: targets-predate-range b957739c9 -- module rename only, no behaviour pinned ::
#   RegistryFallbacks.test_registry_rows_match_the_repos_json_shape
"""Public names G: the private repo names are not in the shipped prompt files. run.sh fills
__BOARD_REPOS__ from `capsule host boards` (repos.json board fields, else config
`unblocked.boards`; temp dirs here), so a live host gets the same prompts. Also the registry
fallbacks when maintenance/repos.json is absent."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "maintenance"))
from capsule import host_registry  # noqa: E402

RUN_SH = ROOT / "maintenance" / "run.sh"
DENYLIST = ROOT / "release" / "private-denylist.txt"


def _private_names():
    """The private project names, read from the `project` rule of the (unshipped) denylist, so this
    public file names none of them. Empty when the denylist is absent (a public checkout)."""
    if not DENYLIST.is_file():
        return ()
    import public_lint
    rules, _ = public_lint._load_denylist(DENYLIST)
    return tuple(n for category, rx, _ in rules if category == "project"
                 for n in rx.split("|") if re.fullmatch(r"[a-z][a-z-]*", n))


PRIVATE = _private_names()
ALWAYS = ("agent-ops",)   # the old internal name: not in the denylist, still guarded everywhere
from capsule.tests.public_tree import private_only  # noqa: E402


def _snippet():
    m = re.search(r'  if \[\[ "\$PROMPT_TEXT" == \*__BOARD_REPOS__\* \]\]; then.*?\n  fi\n',
                  RUN_SH.read_text(encoding="utf-8"), re.S)
    assert m, "run.sh's __BOARD_REPOS__ block moved or changed shape"
    return m.group(0)


def _render(prompt_file, home, repos_file):
    script = (f'set -u\nCAPSULE_CORP_DIR={ROOT}\nexport CAPSULE_HOME={home}\n'
              f'PROMPT_TEXT="$(cat {prompt_file})"\n{_snippet()}\nprintf %s "$PROMPT_TEXT"')
    env = {k: v for k, v in os.environ.items() if not k.startswith("CAPSULE_")}
    env["CAPSULE_HOME"] = str(home)
    env["AGENT_OPS_REPOS_JSON"] = str(repos_file)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                          env=env)


class PromptNames(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    @private_only
    def test_shipped_prompts_name_no_private_repo(self):
        for name in ("security.md", "sprint-plan.md"):
            text = (ROOT / "maintenance" / name).read_text(encoding="utf-8")
            self.assertIn("__BOARD_REPOS__", text)
            for word in ALWAYS:
                self.assertNotIn(word, text, f"{name} names {word}")
            if not PRIVATE:
                continue   # public checkout: only the denylist-driven part is skipped
            for word in PRIVATE:
                self.assertNotIn(word, text, f"{name} names {word}")

    @private_only
    def test_repos_json_board_fields_give_the_same_repos_in_both_prompts(self):
        reg = self.home / "repos.json"
        reg.write_text(json.dumps([{"name": n, "board": b} for n, b in (
            ("sample-mcp", "sample-mcp"), ("sample-app", "sample-app"), ("sample-site", None),
            ("capsule-corp", "capsule-corp"))]))
        sec = _render(ROOT / "maintenance" / "security.md", self.home, reg)
        spr = _render(ROOT / "maintenance" / "sprint-plan.md", self.home, reg)
        for out in (sec, spr):
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertNotIn("__BOARD_REPOS__", out.stdout)
        self.assertIn("`capsule-corp`, `sample-app`, `sample-mcp`.", sec.stdout)
        self.assertNotIn("sample-site", sec.stdout)
        self.assertIn("EXACTLY one of `capsule-corp`, `sample-app`, `sample-mcp` (lowercase)",
                      spr.stdout)

    @private_only
    def test_without_repos_json_config_boards_then_capsule_corp(self):
        absent = self.home / "absent.json"
        sec = _render(ROOT / "maintenance" / "security.md", self.home, absent).stdout
        self.assertIn("$HOME/CodeBase): `capsule-corp`.", sec)
        (self.home / "config.json").write_text(json.dumps({"unblocked": {"boards": ["w", "m"]}}))
        sec = _render(ROOT / "maintenance" / "security.md", self.home, absent).stdout
        self.assertIn("$HOME/CodeBase): `m`, `w`.", sec)
        for word in PRIVATE:
            self.assertNotIn(word, sec)


class RegistryFallbacks(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / "host.json").write_text(json.dumps({
            "repos": {"app": "/x/app"}, "repo_urls": {"app": "https://example.test/app.git"}}))
        (self.home / "config.json").write_text(json.dumps({"unblocked": {"boards": ["app"]}}))
        os.environ["AGENT_OPS_REPOS_JSON"] = str(self.home / "absent-repos.json")
        self.addCleanup(os.environ.pop, "AGENT_OPS_REPOS_JSON", None)
        self._old = os.environ.get("CAPSULE_HOME")
        os.environ["CAPSULE_HOME"] = str(self.home)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("CAPSULE_HOME", None)
        else:
            os.environ["CAPSULE_HOME"] = self._old

    def test_registry_rows_match_the_repos_json_shape(self):
        self.assertEqual(host_registry.registry(), [{"name": "app", "path": "/x/app",
                                            "url": "https://example.test/app.git", "board": "app"}])

    def test_deps_reads_a_registry_list(self):
        import deps
        d = Path(tempfile.mkdtemp())
        (d / "deps-allowlist.txt").write_text("pip:requests==2.32.3\n")
        got = list(deps.allowlists_for_repos([{"name": "app", "path": str(d)}]))
        self.assertEqual(got[0][0], "app")

    def test_brief_active_boards_fall_back_to_host_json(self):
        import brief
        old = brief.REPOS_JSON
        brief.REPOS_JSON = self.home / "absent-repos.json"
        self.addCleanup(setattr, brief, "REPOS_JSON", old)
        self.assertEqual(brief._active_boards(), {"app"})

    def test_brief_active_boards_include_served_boards_missing_from_host_repos(self):
        import brief
        (self.home / "config.json").write_text(
            json.dumps({"unblocked": {"boards": ["app", "extra"]}}))
        old = brief.REPOS_JSON
        brief.REPOS_JSON = self.home / "absent-repos.json"
        self.addCleanup(setattr, brief, "REPOS_JSON", old)
        self.assertEqual(brief._active_boards(), {"app", "extra"})

    def test_release_link_falls_back_to_host_json_url(self):
        import release_gate
        old = os.environ.pop("AGENT_OPS_REPOS_JSON", None)
        self.addCleanup(lambda: old and os.environ.__setitem__("AGENT_OPS_REPOS_JSON", old))
        os.environ["AGENT_OPS_REPOS_JSON"] = str(self.home / "absent.json")
        self.assertEqual(release_gate._release_link("app", "v1"),
                         "https://example.test/app/releases/tag/v1")


if __name__ == "__main__":
    unittest.main()
