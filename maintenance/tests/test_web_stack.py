"""R11: the disk floor, the web stack files and the web_stack intent rows.

The image GC itself is tested in test_image_gc.py (fake docker, shared fakes in
web_stack_fakes.py). The floor tests run docker/build-base.sh against a fake `df` and a fake
`docker` that only logs. Z1: contracts/intent/web_stack.jsonl, replayed below once it exists
(skipped until stamped).
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_image_gc   # noqa: E402  module import only: its Gc tests must not be collected twice
from web_stack_fakes import N_IMAGES, ROOT, Fake, script  # noqa: E402

sys.path.insert(0, str(ROOT))
BUILD = ROOT / "docker" / "build-base.sh"
GIB_KB = 1000 * 1000        # build-base.sh counts decimal GB

FAKE_BUILD_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_CALLS"
case "$*" in
  "buildx version"*) exit 1 ;;   # the legacy builder: no named build context
  "image inspect --format {{.Id}}"*) echo "sha256:%s" ;;
  "image inspect --format {{.Size}}"*) echo 1234 ;;
esac
""" % ("a" * 64)

FAKE_DF = """#!/bin/sh
printf 'Filesystem 1K-blocks Used Available Capacity Mounted\\nfake 1 1 %s 1%% /\\n' "$FAKE_FREE_KB"
"""


class Floor(Fake):
    def build(self, free_gb, arg=()):
        script(self.bin / "docker", FAKE_BUILD_DOCKER)
        script(self.bin / "df", FAKE_DF)
        calls = self.tmp / "calls"
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_CALLS": str(calls),
               "FAKE_FREE_KB": str(free_gb * GIB_KB), "IMAGES_LOCK": str(self.lock)}
        self.lock.write_text("")
        r = subprocess.run(["bash", str(BUILD), *arg], env=env, capture_output=True, text=True)
        return r, (calls.read_text() if calls.exists() else "")

    def test_statusview_floor_is_25_gib_and_warns_below_it(self):
        from capsule import statusview
        self.assertEqual(statusview.DISK_MIN_BYTES, 25 * 2**30)
        for free, warned in ((20 * 2**30, True), (30 * 2**30, False)):
            usage = mock.Mock(free=free)
            with mock.patch.object(statusview.shutil, "disk_usage", return_value=usage):
                got = statusview._world_problems(self.tmp, 0)
            self.assertEqual(bool(got), warned, free)
            if warned:
                self.assertIn("under 25 GB", got[0]["what"])

    def test_a_build_under_the_floor_is_refused_and_runs_no_docker(self):
        for free in (5, 20, 30, 39):
            r, calls = self.build(free)
            self.assertEqual((r.returncode, calls), (2, ""), free)
            self.assertIn("not building", r.stderr)
            self.assertEqual(self.lock.read_text(), "")

    def test_the_web_stack_follows_the_same_floor_and_records_its_own_row(self):
        r, calls = self.build(20, ("web",))
        self.assertEqual((r.returncode, calls), (2, ""))
        r, calls = self.build(45, ("web",))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stacks/web/Dockerfile", calls)
        self.assertIn("capsule-web@sha256:" + "a" * 64, self.lock.read_text())
        self.assertEqual(self.build(45, ("rust",))[0].returncode, 64)

    def test_a_build_with_room_runs_and_records_its_digest(self):
        r, calls = self.build(45)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("build ", calls)
        self.assertIn("capsule-base@sha256:" + "a" * 64, self.lock.read_text())

    def test_the_build_turns_attestations_off_so_the_id_is_the_image_not_an_index(self):
        r, calls = self.build(45)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = [c for c in calls.splitlines() if c.startswith("build ")][0]
        self.assertIn("--provenance=false", line.split())
        self.assertIn("--sbom=false", line.split())

    def test_an_index_shaped_inspect_answer_is_refused_and_not_recorded(self):
        index = "sha256:%s\nsha256:%s" % ("a" * 64, "b" * 64)
        fake = FAKE_BUILD_DOCKER.replace('echo "sha256:%s"' % ("a" * 64), "echo '%s'" % index)
        script(self.bin / "docker", fake)
        script(self.bin / "df", FAKE_DF)
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_CALLS": str(self.tmp / "calls"),
               "FAKE_FREE_KB": str(45 * GIB_KB), "IMAGES_LOCK": str(self.lock)}
        self.lock.write_text("")
        r = subprocess.run(["bash", str(BUILD)], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("bad digest", r.stderr)
        self.assertEqual(self.lock.read_text(), "")


FAKE_RECORD_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_CALLS"
case "$*" in
  "buildx version"*) exit "${FAKE_BUILDX_RC:-1}" ;;   # 1: the legacy builder, no named context
  "build "*) while [ $# -gt 0 ]; do [ "$1" = -f ] && cp "$2" "$FAKE_DF_COPY"; shift; done ;;
  "image inspect --format {{.Id}} {{join"*)
    [ -n "$FAKE_BASE_ABSENT" ] && exit 1; echo "sha256:%s $FAKE_BASE_TAGS" ;;
  "image inspect --format {{.Id}}"*) echo "sha256:%s" ;;
  "image inspect --format {{.Size}}"*) echo 1234 ;;
  "image inspect capsule-base@"*) exit "${FAKE_PINNED_RC:-0}" ;;
esac
""" % ("b" * 64, "c" * 64)


class RecordRoute(Fake):
    """A tester's build writes $CAPSULE_HOME/images.lock (outside any clone), never the repo's
    tracked docker/IMAGES.lock, and chains a stack from the base built here."""
    PINNED = (ROOT / "docker" / "stacks" / "python" / "Dockerfile").read_text()
    BASE = "capsule-base@sha256:" + "b" * 64

    def build(self, *arg, record=None, pinned_rc=0, extra=None, repo=None, cwd=None):
        """`repo`: a copy of the script's repo (see `fake_repo`) to run instead of the real one."""
        script(self.bin / "docker", FAKE_RECORD_DOCKER)
        script(self.bin / "df", FAKE_DF)
        self.home = self.tmp / "home"
        self.home.mkdir(exist_ok=True)
        if record is not None:
            (self.home / "images.lock").write_text(record)
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_CALLS": str(self.tmp / "calls"),
               "FAKE_FREE_KB": str(45 * GIB_KB), "CAPSULE_HOME": str(self.home),
               "FAKE_DF_COPY": str(self.tmp / "dockerfile"), "TMPDIR": str(self.tmp),
               "FAKE_PINNED_RC": str(pinned_rc), "HOME": str(self.tmp),
               "AGENT_OPS_DOCKER_BIN": str(self.bin), **(extra or {})}
        run = repo / "docker" / "build-base.sh" if repo else BUILD
        r = subprocess.run(["bash", str(run), *arg], env=env, capture_output=True, text=True,
                           cwd=cwd)
        calls = self.tmp / "calls"
        return r, (calls.read_text() if calls.exists() else "")

    def fake_repo(self, stack_dockerfile=None):
        """A copy of docker/build-base.sh in a throwaway repo, so a test can write its lock and
        change its Dockerfiles without touching this one."""
        repo = self.tmp / "repo"
        for sub in ("base", "stacks/python"):
            (repo / "docker" / sub).mkdir(parents=True, exist_ok=True)
        shutil.copy(BUILD, repo / "docker" / "build-base.sh")
        shutil.copy(BUILD.parent / "BUILD_MIN_FREE_BYTES", repo / "docker" / "BUILD_MIN_FREE_BYTES")
        (repo / "docker" / "base" / "Dockerfile").write_text("FROM scratch\n")
        (repo / "docker" / "stacks" / "python" / "Dockerfile").write_text(
            stack_dockerfile or self.PINNED)
        return repo

    def froms(self):
        """The FROM lines of the Dockerfile the fake docker was handed with `-f`."""
        lines = (self.tmp / "dockerfile").read_text().splitlines()
        return [ln for ln in lines if ln.startswith("FROM ")]

    def test_the_default_target_is_the_record_under_capsule_home_not_the_repo_lock(self):
        tracked = (ROOT / "docker" / "IMAGES.lock").read_bytes()
        r, _ = self.build()
        self.assertEqual(r.returncode, 0, r.stderr)
        row = "capsule-base@sha256:" + "c" * 64
        self.assertEqual((self.home / "images.lock").read_text(), row + "\n")
        self.assertIn(f"recorded in {self.home}/images.lock", r.stdout)
        self.assertEqual((ROOT / "docker" / "IMAGES.lock").read_bytes(), tracked)

    def test_a_missing_home_folder_is_made_for_the_record(self):
        fresh = self.tmp / "not" / "yet"
        r, _ = self.build(extra={"CAPSULE_HOME": str(fresh)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((fresh / "images.lock").is_file())

    def test_images_lock_still_names_the_maintainers_tracked_file(self):
        r, _ = self.build(extra={"IMAGES_LOCK": str(self.lock)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("capsule-base@sha256:", self.lock.read_text())
        self.assertFalse((self.home / "images.lock").exists())

    def test_a_relative_capsule_home_is_the_folder_it_runs_from_not_the_repo(self):
        repo, elsewhere = self.fake_repo(), self.tmp / "elsewhere"
        elsewhere.mkdir()
        r, _ = self.build(extra={"CAPSULE_HOME": "rel"}, repo=repo, cwd=elsewhere)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((elsewhere / "rel" / "images.lock").is_file())
        self.assertFalse((repo / "rel").exists())

    def test_a_relative_images_lock_is_the_repos_whatever_the_folder_it_runs_from(self):
        repo, elsewhere = self.fake_repo(), self.tmp / "elsewhere"
        elsewhere.mkdir()
        r, calls = self.build(extra={"IMAGES_LOCK": "docker/IMAGES.lock"}, repo=repo, cwd=elsewhere)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("build ", calls)                       # the fake docker is the one that ran
        self.assertIn("capsule-base@sha256:", (repo / "docker" / "IMAGES.lock").read_text())
        self.assertEqual(list(elsewhere.iterdir()), [])      # nothing made where it was run from
        self.assertFalse((self.home / "images.lock").exists())

    def test_a_from_that_cannot_be_swapped_is_refused_never_built_unpinned(self):
        pinned = [ln for ln in self.PINNED.splitlines() if ln.startswith("FROM capsule-base@")][0]
        lower = self.PINNED.replace(pinned, pinned.replace("FROM", "from"))
        for name, text in (("lowercase", lower),
                           ("a tag", self.PINNED.replace(pinned, "FROM capsule-base:latest")),
                           ("one of two", self.PINNED + "\nFROM capsule-base:latest AS two\n")):
            with self.subTest(name):
                (self.tmp / "calls").unlink(missing_ok=True)
                r, calls = self.build("python", record=f"{self.BASE}\n", repo=self.fake_repo(text))
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("not building", r.stderr)
                self.assertNotIn("build ", calls)
                self.assertNotIn("capsule-python", (self.home / "images.lock").read_text())

    def test_a_malformed_base_row_with_the_pinned_image_present_warns_and_builds_pinned(self):
        r, calls = self.build("python", record="capsule-base@sha256:" + "b" * 10 + "\n",
                              repo=self.fake_repo())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("warning: ignoring a malformed capsule-base row", r.stderr)
        self.assertIn("docker/stacks/python/Dockerfile", calls)    # the repo's own, unswapped
        r, _ = self.build("python", record="capsule-base@sha256:" + "b" * 10 + "\n",
                          pinned_rc=1, repo=self.fake_repo())
        self.assertEqual(r.returncode, 1)                          # pinned base absent: refused
        self.assertIn("run docker/build-base.sh base first", r.stderr)

    def test_a_stack_builds_from_the_base_row_of_the_record_and_keeps_the_other_rows(self):
        r, _ = self.build("python", record=f"{self.BASE}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.froms(), [f"FROM {self.BASE}"])
        self.assertEqual((self.home / "images.lock").read_text().splitlines(),
                         [self.BASE, "capsule-python@sha256:" + "c" * 64])
        python_df = ROOT / "docker" / "stacks" / "python" / "Dockerfile"
        self.assertEqual(python_df.read_text(), self.PINNED)       # the repo's file is not edited

    def test_the_web_stack_swaps_only_its_capsule_base_edge(self):
        r, _ = self.build("web", record=f"{self.BASE}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        froms = self.froms()
        self.assertTrue(froms[0].startswith("FROM node:26"), froms)
        self.assertEqual(froms[1], f"FROM {self.BASE}")

    def buildx_build(self, **extra):
        r, calls = self.build("python", record=f"{self.BASE}\n",
                              extra={"FAKE_BUILDX_RC": "0", **extra})
        return r, [c for c in calls.splitlines() if c.startswith("build ")]

    def test_with_buildx_the_base_row_becomes_a_named_context_by_any_local_tag(self):
        ctx = f"--build-context {self.BASE}=docker-image://"
        for tags, tag in (("capsule-base:old,capsule-base:local", "capsule-base:local"),
                          ("capsule-base:old", "capsule-base:old")):
            with self.subTest(tags=tags):
                r, builds = self.buildx_build(FAKE_BASE_TAGS=tags)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(ctx + tag, builds[-1])   # the calls log accumulates

    def test_with_buildx_an_untagged_base_gets_no_context_and_a_note(self):
        r, builds = self.buildx_build()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("has no local tag", r.stderr)
        self.assertNotIn("--build-context", builds[0])

    def test_with_buildx_a_base_digest_that_is_not_local_is_refused(self):
        r, builds = self.buildx_build(FAKE_BASE_ABSENT="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("run docker/build-base.sh base", r.stderr)
        self.assertEqual(builds, [])

    def test_a_stack_with_no_base_anywhere_says_to_build_the_base_first(self):
        r, calls = self.build("python", pinned_rc=1)
        self.assertEqual(r.returncode, 1)
        self.assertIn("run docker/build-base.sh base first", r.stderr)
        self.assertNotIn("build ", calls.replace("image inspect", ""))
        self.assertFalse((self.home / "images.lock").exists())

    def test_the_40_gb_floor_still_refuses_before_any_docker_call(self):
        r, calls = self.build("python", extra={"FAKE_FREE_KB": str(39 * GIB_KB)})
        self.assertEqual((r.returncode, calls), (2, ""))
        self.assertFalse((self.home / "images.lock").exists())


class WebStackFiles(unittest.TestCase):
    """docker/stacks/web as committed: pinned by digest, lockfile-only, host list respected."""
    DOCKERFILE = ROOT / "docker" / "stacks" / "web" / "Dockerfile"
    LOCKROWS = (ROOT / "docker" / "IMAGES.lock").read_text().splitlines()

    def code(self):
        return [ln for ln in self.DOCKERFILE.read_text().splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]

    def test_both_froms_are_digest_pinned_and_the_base_edge_matches_the_lock(self):
        froms = [ln.split()[1] for ln in self.code() if ln.startswith("FROM ")]
        self.assertEqual(len(froms), 2)
        self.assertRegex(froms[0], r"^node:26\.12\.1-trixie-slim@sha256:[0-9a-f]{64}$")
        base = [ln for ln in self.LOCKROWS if ln.startswith("capsule-base@")]
        self.assertEqual(froms[1], base[0])

    def test_the_web_row_is_in_the_lock_as_a_digest(self):
        rows = [ln for ln in self.LOCKROWS if ln.startswith("capsule-web@")]
        self.assertEqual(len(rows), 1)
        self.assertRegex(rows[0], r"^capsule-web@sha256:[0-9a-f]{64}$")

    def test_npm_installs_only_from_the_lock_without_scripts_and_nothing_fetches_by_url(self):
        text = "\n".join(self.code())
        self.assertIn("npm ci --ignore-scripts", text)
        for bad in ("npm install", "npm i ", "curl", "wget", "://", "playwright install"):
            self.assertNotIn(bad, text)
        self.assertIn("LABEL capsule-layer=stack-web", text)

    def test_node_bins_follow_the_system_dirs_and_the_root_run_uses_absolute_tools(self):
        """R11 review blocker: a package bin named rm or chmod must never shadow a system tool."""
        text = self.DOCKERFILE.read_text()
        env = [ln for ln in self.code() if ln.startswith("ENV PATH=")]
        web_sh = [ln for ln in self.code() if "/etc/profile.d/web.sh" in ln]
        self.assertEqual((len(env), len(web_sh)), (1, 1))
        for value in (env[0].split("=", 1)[1], web_sh[0].split("'")[1].split("\\n")[0][5:]):
            parts = value.split(":")
            self.assertTrue(value.startswith("/opt/playwright/bin:"), value)
            self.assertIn("$PATH", parts)
            self.assertEqual([p for p in parts if "node_modules" in p and
                              parts.index(p) < parts.index("$PATH")], [], value)
            self.assertEqual(len([p for p in parts if "node_modules" in p]), 2, value)
        run = text.split("RUN cd /opt/capsule-web", 1)[1].split("ENV NODE_PATH", 1)[0]
        self.assertIn("/usr/bin/env PATH=/usr/local/bin:/usr/bin:/bin npm ci", run)
        for tool in ("rm", "chmod"):
            self.assertIn(f"&& /bin/{tool} ", run)
            self.assertNotIn(f"&& {tool} ", run)

    def test_the_python_tool_pins_equal_the_python_stacks_pins(self):
        """The web stack is a superset of the python stack's tooling: no drift between them."""
        import re

        def pins(path):
            return dict(re.findall(r'"([A-Za-z0-9_.-]+)==([0-9][^"]*)"', path.read_text()))
        want = {"coverage", "diff-cover", "hypothesis", "mutmut", "pytest", "ruff"}
        py = pins(ROOT / "docker" / "stacks" / "python" / "Dockerfile")
        web = pins(self.DOCKERFILE)
        self.assertEqual(set(py), want)
        self.assertEqual({k: web.get(k) for k in want}, py)
        self.assertIn("python3 python3-venv perl", self.DOCKERFILE.read_text())

    def test_the_venv_leads_the_profile_path_and_ui_guard_is_in_its_site_packages(self):
        text = self.DOCKERFILE.read_text()
        web_sh = [ln for ln in self.code() if "/etc/profile.d/web.sh" in ln]
        self.assertEqual(len(web_sh), 1)
        self.assertTrue(web_sh[0].split("'")[1].startswith("PATH=/opt/playwright/bin:"), web_sh[0])
        cp = [ln for ln in self.code() if "ui_guard.py" in ln and ln.lstrip().startswith("RUN cp")]
        self.assertEqual(len(cp), 1)
        self.assertIn("/opt/playwright/bin/python -c 'import site", cp[0])
        self.assertIn("site.getsitepackages()[0])')/ui_guard.py", cp[0])
        self.assertLess(text.index("COPY base/ui-check base/ui_guard.py"), text.index(cp[0]))

    def test_package_json_is_exact_and_every_lock_entry_is_a_registry_tarball(self):
        base = ROOT / "docker" / "stacks" / "web"
        pkg = json.loads((base / "package.json").read_text())
        lock = json.loads((base / "package-lock.json").read_text())
        self.assertEqual(pkg["dependencies"], {"@playwright/test": "1.64.1"})
        self.assertEqual(lock["lockfileVersion"], 3)
        for key, entry in lock["packages"].items():
            if key:
                self.assertTrue(entry["resolved"].startswith("https://registry.npmjs.org/"), key)
                self.assertTrue(entry["integrity"].startswith("sha512-"), key)
        self.assertEqual(lock["packages"]["node_modules/@playwright/test"]["version"], "1.64.1")

    def test_the_renderer_builds_a_project_layer_on_the_real_web_row(self):
        from argparse import Namespace
        from capsule import image_render
        project = Path(tempfile.mkdtemp(prefix="web-render-"))
        self.addCleanup(shutil.rmtree, project, True)
        base = ROOT / "docker" / "stacks" / "web"
        (project / "capsule.toml").write_text('[image]\nstacks = ["web"]\n'
                                              'lockfiles = ["package-lock.json"]\n')
        for name in ("package.json", "package-lock.json"):
            shutil.copy(base / name, project / name)
        out = project / "Dockerfile"
        rc = image_render.main(Namespace(project=str(project), lock=None, output=str(out)))
        row = [ln for ln in self.LOCKROWS if ln.startswith("capsule-web@")][0]
        self.assertEqual(rc, 0)
        self.assertIn(f"FROM {row}\n", out.read_text())


def replay(case):
    """What each stamped web_stack row does, as an observed result the row's expect is read from."""
    if case == "floor":
        t = Floor("test_a_build_under_the_floor_is_refused_and_runs_no_docker")
    else:
        t = test_image_gc.Gc("test_dry_run_is_the_default_and_removes_nothing")
    t.setUp()
    try:
        if case == "floor":
            r, calls = t.build(20)
            return {"exit": r.returncode, "docker_build_runs": calls.count("build ")}
        r = t.gc("1" if case == "gc-keeps-lock" else None)
        if case == "gc-keeps-lock":
            return {"exit": r.returncode, "lock_digests_removed": len({"a", "1", "5"} - t.left()),
                    "unlabelled_removed": int("u" not in t.left())}
        return {"exit": r.returncode, "images_removed": N_IMAGES - len(t.left())}
    finally:
        shutil.rmtree(t.tmp, True)


class Rows(unittest.TestCase):
    def test_the_intent_rows_hold_when_the_file_exists(self):
        from capsule import intent
        rows = intent.load("web_stack")
        case_of = {"gc-keeps-lock": "gc-keeps-lock", "under-floor-no-build": "floor",
                   "gc-dry-run-removes-nothing": "gc-dry-run-removes-nothing"}
        self.assertEqual({r["id"] for r in rows}, set(case_of))
        for row in rows:
            got = replay(case_of[row["id"]])
            for key, want in row["expect"].items():
                if isinstance(want, int):
                    self.assertEqual(got[key], want, row["id"])

    def test_the_built_in_cases_hold_without_the_file(self):
        self.assertEqual(replay("gc-keeps-lock"),
                         {"exit": 0, "lock_digests_removed": 0, "unlabelled_removed": 0})
        self.assertEqual(replay("gc-dry-run-removes-nothing"), {"exit": 0, "images_removed": 0})
        self.assertEqual(replay("floor"), {"exit": 2, "docker_build_runs": 0})


if __name__ == "__main__":
    unittest.main()
