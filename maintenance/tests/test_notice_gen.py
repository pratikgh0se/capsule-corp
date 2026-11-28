import contextlib, io, json, os, re, sys, tempfile, unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
import notice_gen as ng

DOCKERFILE = """\
FROM nikolaik/python-nodejs:python3.11-nodejs20
RUN python3 -m venv /opt/playwright && /opt/playwright/bin/pip install -q playwright \\
    && chmod -R a+rX /opt/playwright
RUN PW_VER="$(/opt/playwright/bin/pip show playwright | awk '/^Version:/{print $2}')" \\
    && pip install -q --no-cache-dir "coverage>=7" "diff-cover>=9" "playwright==${PW_VER}"
RUN pip install -q --no-cache-dir "ruff==0.6.9" "hypothesis==6.112.2"
"""

PIP_LICENSES = json.dumps([
    {"Name": "ruff", "Version": "0.6.9", "License": "MIT"},
    {"Name": "coverage", "Version": "7.6.1", "License": "Apache-2.0"},
    {"Name": "weird", "Version": "1.0", "License": "UNKNOWN"},
])

NPM_LOCK = json.dumps({
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "sample-app", "version": "0.1.0"},
        "node_modules/@esbuild/android-arm": {"version": "0.28.2", "license": "MIT", "dev": True},
        "node_modules/googleapis": {"version": "144.0.0", "license": "Apache-2.0"},
        "node_modules/mystery": {"version": "2.0.0"},
    },
})


class Parsers(unittest.TestCase):
    def test_dockerfile_pins_read_every_pip_install_line(self):
        got = {c.name: c.version for c in ng.parse_dockerfile_pins(DOCKERFILE)}
        self.assertEqual(got["ruff"], "0.6.9")
        self.assertEqual(got["hypothesis"], "6.112.2")
        self.assertEqual(got["coverage"], ">=7")
        self.assertEqual(got["diff-cover"], ">=9")
        self.assertEqual(got["playwright"], "${PW_VER}")

    def test_a_dockerfile_pin_has_no_declared_licence(self):
        """A Dockerfile says nothing about licences - so every component from
        it starts `unknown`, which classifies denied until declared.json says
        otherwise. The study's point in 5.4: unknown must be loud."""
        self.assertEqual({c.license for c in ng.parse_dockerfile_pins(DOCKERFILE)}, {"unknown"})

    def test_dockerfile_ignores_apt_and_venv_lines(self):
        self.assertEqual(ng.parse_dockerfile_pins("RUN apt-get install -y git sqlite3\n"), [])

    def test_pip_licenses_json(self):
        got = {c.name: c.license for c in ng.parse_pip_licenses(PIP_LICENSES)}
        self.assertEqual(got["ruff"], "MIT")
        self.assertEqual(got["weird"], "unknown", "UNKNOWN normalises to unknown")

    def test_npm_lock_skips_the_root_and_dev_entries(self):
        got = {c.name: c.license for c in ng.parse_npm_lock(NPM_LOCK)}
        self.assertNotIn("sample-app", got, "the root '' entry is the repo, not a component")
        self.assertNotIn("@esbuild/android-arm", got, "dev-only deps are not distributed")
        self.assertEqual(got["googleapis"], "Apache-2.0")
        self.assertEqual(got["mystery"], "unknown", "a package-lock entry with no license field")

    def test_a_malformed_source_is_empty_not_an_exception(self):
        self.assertEqual(ng.parse_pip_licenses("not json at all"), [])
        self.assertEqual(ng.parse_npm_lock("{"), [])


class Classify(unittest.TestCase):
    def _c(self, lic):
        return ng.Component("x", "1", lic, "test")

    def test_the_truth_table(self):
        self.assertEqual(ng.classify(self._c("MIT")), "allowed")
        self.assertEqual(ng.classify(self._c("Apache-2.0")), "allowed")
        self.assertEqual(ng.classify(self._c("MPL-2.0")), "allowed")
        self.assertEqual(ng.classify(self._c("LGPL-3.0")), "allowed")
        self.assertEqual(ng.classify(self._c("EPL-2.0")), "review")
        self.assertEqual(ng.classify(self._c("GPL-3.0")), "denied")
        self.assertEqual(ng.classify(self._c("AGPL-3.0")), "denied")
        self.assertEqual(ng.classify(self._c("unknown")), "denied")
        self.assertEqual(ng.classify(self._c("Weird-1.0")), "denied",
                         "a licence nobody has classified is denied, not allowed")

    def test_declared_json_fills_an_unknown_and_nothing_else(self):
        declared = ng.parse_declared('{"ruff": "MIT"}')
        comps = [ng.Component("ruff", "0.6.9", "unknown", "Dockerfile"),
                 ng.Component("other", "1.0", "GPL-3.0", "pip-licenses")]
        got = {c.name: c.license for c in ng.apply_declared(comps, declared)}
        self.assertEqual(got["ruff"], "MIT")
        self.assertEqual(got["other"], "GPL-3.0", "declared.json never overrides a real declaration")

    def test_a_component_absent_from_declared_stays_unknown(self):
        comps = [ng.Component("brandnew", "1.0", "unknown", "Dockerfile")]
        self.assertEqual(ng.apply_declared(comps, {})[0].license, "unknown")


class InventoryAndRender(unittest.TestCase):
    def setUp(self):
        self.comps = ng.inventory(sources=[("Dockerfile", DOCKERFILE, ng.parse_dockerfile_pins),
                                           ("pip-licenses", PIP_LICENSES, ng.parse_pip_licenses)],
                                  declared={"hypothesis": "MPL-2.0", "diff-cover": "Apache-2.0",
                                            "playwright": "Apache-2.0"})

    def test_inventory_dedupes_and_prefers_a_real_licence(self):
        got = {c.name: c.license for c in self.comps}
        self.assertEqual(got["ruff"], "MIT", "pip-licenses wins over the Dockerfile's unknown")
        self.assertEqual(len([c for c in self.comps if c.name == "ruff"]), 1)

    def test_inventory_is_sorted_by_name(self):
        names = [c.name for c in self.comps]
        self.assertEqual(names, sorted(names))

    def test_the_more_specific_version_wins(self):
        """`playwright` really is in the Dockerfile twice - bare on the venv
        line, pinned on the next - and `unpinned` must not win."""
        got = {c.name: c.version for c in self.comps}
        self.assertEqual(got["playwright"], "${PW_VER}")

    def test_scan_lines_end_with_a_summary(self):
        lines, counts = ng.scan_lines(self.comps)
        self.assertTrue(lines[-1].startswith("licences: "))
        self.assertIn(f"{counts['allowed']} allowed", lines[-1])
        self.assertIn(f"{counts['review']} review", lines[-1])
        self.assertIn(f"{counts['denied']} denied", lines[-1])
        self.assertTrue(any(l.startswith("  ruff 0.6.9 MIT allowed") for l in lines), lines)

    def test_render_notice_names_every_component_and_the_url_warning(self):
        out = ng.render_notice(self.comps, generated=date(2026, 9, 10), texts_dir=None)
        self.assertIn("generated: 2026-09-10", out)
        for c in self.comps:
            self.assertIn(c.name, out)
        self.assertIn("a bare URL is not compliant", out)
        self.assertIn("| ruff | 0.6.9 | MIT |", out)

    def test_render_notice_flags_a_licence_with_no_text_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "MIT.txt").write_text("MIT text")
            out = ng.render_notice(self.comps, generated=date(2026, 9, 10), texts_dir=Path(d))
            self.assertIn("MIT - full text present", out)
            self.assertIn("Apache-2.0 - TEXT MISSING", out)


class NoticeSections(unittest.TestCase):
    """The data-driven sections: services, CI-only tools, host tools, worker layers."""
    ROOT = Path(__file__).resolve().parents[2]

    def _sections(self):
        text = (self.ROOT / "docs" / "licenses" / ng.SECTIONS_FILE).read_text(encoding="utf-8")
        return {s["title"]: s for s in json.loads(text)}

    def _rendered(self):
        return ng.render_notice([], generated=date(2026, 11, 14),
                                sections=list(self._sections().values()))

    def test_downloaded_services_are_listed_with_licence_and_upstream_and_not_redistributed(self):
        sec = self._sections()["Services you download separately"]
        by = {r[0]: r for r in sec["rows"]}
        self.assertEqual(by["grafana/grafana"][1:3], ["13.2.4", "AGPL-3.0"])
        self.assertEqual(by["valkey/valkey"][2][:12], "BSD-3-Clause")
        self.assertNotIn("redis", by)
        self.assertTrue(all(r[3].startswith("https://") for r in sec["rows"]))
        self.assertIn("does not redistribute", " ".join(sec["intro"].split()))
        self.assertIn("## Services you download separately", self._rendered())

    def test_ci_only_tools_are_listed_under_not_shipped(self):
        sec = self._sections()["CI-only tools (not shipped)"]
        by = {r[0]: r for r in sec["rows"]}
        self.assertNotIn("gitleaks/gitleaks-action", by)
        semgrep = by["Semgrep rules (registry p/owasp-top-ten, p/python)"]
        self.assertIn("Semgrep Rules License", semgrep[2])
        self.assertEqual(by["actions/checkout"][2], "MIT")
        self.assertIn("## CI-only tools (not shipped)", self._rendered())

    def test_host_tools_section_says_they_are_not_redistributed(self):
        sec = self._sections()["Host tools you install yourself"]
        self.assertIn("does not redistribute", " ".join(sec["intro"].split()))
        self.assertIn("## Host tools you install yourself", self._rendered())

    def test_worker_image_indirect_dependencies_are_data_and_direct_pins_are_not(self):
        secs = self._sections()
        # R11: the packages come from the committed pip-licenses JSON, so the sections hold none.
        self.assertEqual(secs["Worker image: Python layer (indirect dependencies)"]["rows"], [])
        web = {r[0]: r for r in secs["Worker image: web layer (indirect dependencies)"]["rows"]}
        self.assertEqual(set(web), {"Node.js (node image, with npm)"})
        rows = {c.name: c for f in ("python-layer", "web-layer") for c in ng.parse_pip_licenses(
            (self.ROOT / "docs" / "licenses" / f"{f}.pip-licenses.json").read_text("utf-8"))}
        self.assertEqual((rows["jinja2"].version, rows["jinja2"].license),
                         ("3.1.6", "BSD-3-Clause"))
        self.assertEqual(rows["pyee"].license, "MIT")

    def test_scan_reads_the_stack_dockerfiles_and_the_lockfile(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ng.main(["--scan", "--root", str(self.ROOT)])
        out = buf.getvalue()
        m = re.search(r"licences: (\d+) allowed, (\d+) review, (\d+) denied", out)
        self.assertEqual(rc, 0, out)
        self.assertGreaterEqual(int(m.group(1)), 7, out)
        self.assertEqual(m.group(3), "0", out)
        for name in ("hypothesis 6.170.9 MPL-2.0 allowed", "pytest 9.1.1",
                     "@playwright/test 1.64.1"):
            self.assertIn(name, out)

    def test_continuation_lines_are_joined_before_pins_are_read(self):
        text = 'RUN pip install -q \\\n    "a==1.0" "b==2.0" \\\n    && chmod -R a+rX /opt\n'
        self.assertEqual([(c.name, c.version) for c in ng.parse_dockerfile_pins(text)],
                         [("a", "1.0"), ("b", "2.0")])

    def test_service_rows_equal_the_images_pinned_in_the_compose_file(self):
        compose = (self.ROOT / "observability" / "docker-compose.yml").read_text(encoding="utf-8")
        pinned = set()
        image_re = r"(?m)^\s*image:\s*(\S+?)(?::(\S+)|@sha256:\S+\s+#\s*(v[\d.]+))(?:\s|$)"
        for m in re.finditer(image_re, compose):
            pinned.add((m.group(1), m.group(2) or m.group(3)))
        rows = {(r[0], r[1]) for r in self._sections()["Services you download separately"]["rows"]}
        self.assertEqual(rows, pinned)

    def test_ci_rows_equal_the_uses_pins_in_the_workflows(self):
        pinned = set()
        for wf in (self.ROOT / ".github" / "workflows").glob("*.yml"):
            text = wf.read_text(encoding="utf-8")
            for m in re.finditer(r"uses:\s*(\S+?)@\w+\s+#\s*(v[\d.]+)", text):
                pinned.add((m.group(1), m.group(2)))
        rows = set()
        for r in self._sections()["CI-only tools (not shipped)"]["rows"]:
            if re.fullmatch(r"[\w.-]+/[\w.-]+", r[0]) and r[0] != "gitleaks/gitleaks":
                rows |= {(r[0], v) for v in r[1].split(", ")}
        self.assertEqual(rows, pinned)

    def test_ci_pip_and_download_rows_match_the_verify_workflow(self):
        verify = (self.ROOT / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
        for r in self._sections()["CI-only tools (not shipped)"]["rows"]:
            if r[0] in ("semgrep", "pip-audit", "ruff"):
                self.assertIn(f"{r[0]}=={r[1]}", verify)
            if r[0].startswith("gitleaks (CLI"):
                self.assertIn(f"/{r[1]}", verify)

    def test_the_committed_notice_equals_the_generated_output(self):
        lic = self.ROOT / "docs" / "licenses"
        sources, _p, _s, meta = ng._default_sources(self.ROOT)
        declared = ng.parse_declared(ng._read(lic / "declared.json"))
        comps = ng.inventory(sources=sources, declared=declared)
        text = ng.render_notice(comps, generated=date(2026, 11, 14), texts_dir=lic / "texts",
                                sources=meta, sections=list(self._sections().values()))
        committed = (lic / "NOTICE.md").read_text(encoding="utf-8")
        self.assertEqual(re.sub(r"(?m)^generated: .*$", "", committed),
                         re.sub(r"(?m)^generated: .*$", "", text))


class Freshness(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="notice-test-"))
        self.src = self.d / "Dockerfile"
        self.src.write_text(DOCKERFILE)
        self.notice = self.d / "NOTICE.md"

    def test_a_missing_notice_is_not_fresh(self):
        ok, line = ng.check_freshness(self.notice, [self.src], today=date(2026, 9, 10))
        self.assertFalse(ok)
        self.assertIn("missing", line)

    def test_a_notice_generated_today_is_fresh(self):
        self.notice.write_text("generated: 2026-09-10\n")
        ok, line = ng.check_freshness(self.notice, [self.src], today=date(2026, 9, 10))
        self.assertTrue(ok, line)
        self.assertIn("fresh", line)

    def test_a_notice_older_than_its_sources_is_stale(self):
        self.notice.write_text("generated: 2026-01-01\n")
        ok, line = ng.check_freshness(self.notice, [self.src], today=date(2026, 9, 10))
        self.assertFalse(ok)
        self.assertIn("stale", line)

    def test_no_generated_date_is_stale_not_a_crash(self):
        self.notice.write_text("# Notice\n")
        ok, line = ng.check_freshness(self.notice, [self.src], today=date(2026, 9, 10))
        self.assertFalse(ok)


class Gate(unittest.TestCase):
    def test_the_flag_truth_table(self):
        self.assertFalse(ng.gate_blocking({}))
        self.assertFalse(ng.gate_blocking({"OPS_LICENSE_GATE": "0"}))
        self.assertFalse(ng.gate_blocking({"OPS_LICENSE_GATE": "off"}))
        self.assertTrue(ng.gate_blocking({"OPS_LICENSE_GATE": "1"}))

    def _main(self, env):
        """Hermetic: an empty scratch root (no declared.json, so every
        Dockerfile component stays `unknown` and therefore denied) and no node
        lockfile, so the result does not depend on which sibling checkouts
        this machine happens to have. Output is captured, not let through -
        `main` prints scan/skip lines that would otherwise spam the test
        run's own stdout."""
        with tempfile.TemporaryDirectory() as d:
            args = ["--scan", "--root", d, "--node-lock", "/nonexistent",
                    "--inline-dockerfile", DOCKERFILE]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = ng.main(args, env=env)
            return rc, buf.getvalue()

    def test_scan_exits_zero_while_advisory_even_with_a_denied_component(self):
        rc, _out = self._main({})
        self.assertEqual(rc, 0)

    def test_scan_exits_one_when_the_flag_is_on_and_something_is_outside_the_allowlist(self):
        rc, _out = self._main({"OPS_LICENSE_GATE": "1"})
        self.assertEqual(rc, 1)

    def test_a_flag_value_is_consumed_not_rescanned_as_another_flag(self):
        """`--node-lock --check` must not make `--check` re-parse as the mode:
        the value of `--node-lock` is consumed, not scanned a second time."""
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = ng.main(["--write", "--node-lock", "--check", "--root", d,
                              "--inline-dockerfile", DOCKERFILE], env={})
            self.assertEqual(rc, 0)
            self.assertTrue((Path(d) / "docs" / "licenses" / "NOTICE.md").exists(),
                             "mode must have stayed --write, not been overridden by the flag's value")


class PolicySets(unittest.TestCase):
    def test_the_three_classes_are_pairwise_disjoint(self):
        """A future edit that adds the same id to two classes must not
        silently decide the tie in `classify`'s favour of whichever branch
        runs first."""
        self.assertEqual(ng.ALLOWED & ng.REVIEW, set())
        self.assertEqual(ng.ALLOWED & ng.DENIED, set())
        self.assertEqual(ng.REVIEW & ng.DENIED, set())


class DefaultSources(unittest.TestCase):
    def _root(self, d):
        root = Path(d)
        (root / "docker" / "base").mkdir(parents=True)
        (root / "docker" / "base" / "Dockerfile").write_text(DOCKERFILE)
        (root / "docs" / "licenses").mkdir(parents=True)
        return root

    def test_an_absent_pip_licenses_glob_and_node_lock_are_each_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            _sources, _paths, skips, meta = ng._default_sources(root, node_lock="/nonexistent")
        self.assertTrue(any(s.startswith("pip-licenses: skipped") for s in skips), skips)
        self.assertTrue(any(s.startswith("node-lock: skipped") for s in skips), skips)
        self.assertFalse(any(m["external"] for m in meta))

    def test_a_present_pip_licenses_file_and_node_lock_are_not_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            (root / "docs" / "licenses" / "worker.pip-licenses.json").write_text(PIP_LICENSES)
            lockfile = root / "package-lock.json"
            lockfile.write_text(NPM_LOCK)
            _sources, _paths, skips, meta = ng._default_sources(root, node_lock=str(lockfile))
        self.assertEqual(skips, [])
        self.assertTrue(any(m["external"] for m in meta), meta)
        self.assertIn(f"{root.name}/package-lock.json", [m["label"] for m in meta])

    def test_the_node_lock_label_comes_from_the_lock_projects_dir_not_a_fixed_name(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            proj = Path(d) / "other" / "my-app"
            proj.mkdir(parents=True)
            (proj / "package-lock.json").write_text(NPM_LOCK)
            _s, _p, _skips, meta = ng._default_sources(
                root, node_lock=str(proj / "package-lock.json"))
        self.assertIn("my-app/package-lock.json", [m["label"] for m in meta])

    def test_an_empty_node_lock_flag_is_treated_as_absent_not_as_the_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            _sources, _paths, skips, _meta = ng._default_sources(root, node_lock="")
        self.assertTrue(any(s.startswith("node-lock: skipped") for s in skips), skips)


class RenderNoticeProvenanceAndSafety(unittest.TestCase):
    def test_provenance_names_each_source_and_flags_the_lockfile_as_external(self):
        comps = [ng.Component("ruff", "0.6.9", "MIT", "Dockerfile")]
        sources = [{"label": "Dockerfile", "external": False},
                   {"label": "sample-app/package-lock.json", "external": True}]
        out = ng.render_notice(comps, generated=date(2026, 9, 10), sources=sources)
        self.assertIn("Dockerfile", out)
        self.assertIn("outside this repository", out)

    def test_a_licence_id_that_looks_like_a_path_cannot_escape_texts_dir(self):
        """The raw (untrusted) licence id may still be shown as text - that's
        just what the source claimed - but the `docs/licenses/texts/...`
        path built from it must never contain a `..` segment or the
        component's `/etc/passwd`-shaped payload verbatim."""
        comps = [ng.Component("weird", "1.0", "../../etc/passwd", "test")]
        with tempfile.TemporaryDirectory() as d:
            out = ng.render_notice(comps, generated=date(2026, 9, 10), texts_dir=Path(d))
        path_line = next(l for l in out.splitlines() if l.startswith("- ../../etc/passwd"))
        quoted_path = path_line.split("(`", 1)[1].rstrip("`)")
        self.assertNotIn("..", quoted_path)
        self.assertNotIn("/etc/passwd", quoted_path)


class DeepReview(unittest.TestCase):
    @staticmethod
    def _report(*lics):
        return json.dumps({"Results": [{"Licenses": [
            {"FilePath": p, "Name": n} for p, n in lics]}]})

    def _run(self, *texts, env=None):
        with tempfile.TemporaryDirectory() as d:
            files = []
            for i, t in enumerate(texts):
                f = Path(d) / f"r{i}.json"
                f.write_text(t, encoding="utf-8")
                files.append(str(f))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = ng.main(["--deep-review", *files], env=env or {})
        return rc, buf.getvalue()

    def test_allowed_silent(self):
        rc, out = self._run(self._report(("/x/coverage/LICENSE", "Apache-2.0")))
        self.assertEqual(rc, 0)
        self.assertNotIn("new finding", out)

    def test_mpl_listed(self):
        rc, out = self._run(self._report(("/s/site-packages/zzz_unlisted/LICENSE", "EPL-2.0")))
        self.assertEqual(rc, 1)
        self.assertIn("deep-scan: 1 new finding(s)", out)
        self.assertIn("EPL-2.0", out)

    def test_d240_gpl_family_denied_in_shipped_paths(self):
        for lic in ("GPL-3.0-or-later", "AGPL-3.0-only", "GPL-2.0-only", "AGPL-3.0"):
            rc, out = self._run(self._report(("/app/vendor/zzz/LICENSE", lic)))
            self.assertEqual(rc, 1, lic)
            self.assertIn(lic, out)

    def test_d240_lgpl_and_mpl_allowed(self):
        for lic in ("LGPL-2.1", "LGPL-3.0-or-later", "MPL-2.0"):
            rc, out = self._run(self._report(("/app/vendor/zzz/LICENSE", lic)))
            self.assertEqual(rc, 0, lic)

    def test_d240_expressions_and_junk_ids_denied(self):
        for lic in ("LGPL-2.1 AND GPL-2.0", "MPL-2.0 AND GPL-3.0", "MPL-2.0;GPL-3.0",
                    "LGPL-2.1, GPL-3.0", "MPL-", "LGPL-foo"):
            rc, _out = self._run(self._report(("/app/vendor/zzz/LICENSE", lic)))
            self.assertEqual(rc, 1, lic)

    def test_d240_spdx_expression_semantics(self):
        shipped = "/app/vendor/zzz/LICENSE"
        for lic in ("MIT OR Apache-2.0", "GPL-2.0 OR MIT", "(MIT AND GPL-3.0) OR Apache-2.0",
                    "GPL-2.0-only WITH Classpath-exception-2.0 OR MIT", "MIT or ISC"):
            rc, _out = self._run(self._report((shipped, lic)))
            self.assertEqual(rc, 0, lic)
        for lic in ("LGPL-2.1 AND GPL-2.0", "GPL-2.0 OR", "(MIT", "MIT)", "MIT Apache-2.0",
                    "OR MIT", "MIT WITH", "AND", "()", "MIT OR ??", "MIT AND GPL-3.0 OR GPL-2.0"):
            rc, _out = self._run(self._report((shipped, lic)))
            self.assertEqual(rc, 1, lic)

    def test_d240_with_operand_must_be_a_known_exception(self):
        shipped = "/app/vendor/zzz/LICENSE"
        for lic, rc_want in (("MIT WITH GPL-3.0", 1), ("MIT WITH AGPL-3.0", 1),
                             ("MIT WITH Made-up-exception", 1),
                             ("Apache-2.0 WITH LLVM-exception", 0),
                             ("LGPL-2.1-only WITH OCaml-LGPL-linking-exception", 0),
                             ("Apache-2.0 with LLVM-exception", 0),
                             ("(MIT OR Apache-2.0) WITH LLVM-exception", 1),
                             ("GPL-2.0-only WITH Classpath-exception-2.0", 1),
                             ("(MIT WITH GPL-3.0) OR Apache-2.0", 0),
                             ("(MIT WITH GPL-3.0) AND Apache-2.0", 1)):
            rc, _out = self._run(self._report((shipped, lic)))
            self.assertEqual(rc, rc_want, lic)

    def test_d240_accept_line_never_silences_gpl_in_shipped_path(self):
        path = "/app/vendor/zzz/LICENSE"
        acc = [("GPL-3.0", "/app/vendor/*", "r"), ("LGPL-2.1 AND GPL-2.0", "/app/vendor/*", "r")]
        out = ng._deep_review([(path, "GPL-3.0")], acc)
        self.assertEqual(len(out), 1)
        self.assertIn("accept line ignored", out[0])
        self.assertIn("D240", out[0])
        out = ng._deep_review([(path, "LGPL-2.1 AND GPL-2.0")], acc)
        self.assertIn("accept line ignored", out[0])
        self.assertEqual(ng._deep_review([("/s/a/L", "GPL-3.0")], acc, ["/s/a/*"]), [])
        plain = ng._deep_review([(path, "GPL-3.0")], [])
        self.assertNotIn("ignored", plain[0])

    def test_d240_gpl_guard_reads_raw_text(self):
        path = "/app/vendor/zzz/LICENSE"
        for lic in ("MIT;GPL-3.0", "MIT, GPL-3.0", "MIT/GPL-3.0", "MIT+GPL-3.0",
                    "LGPL-2.1;GPL-3.0", "GPLv3", "GPL3", "AGPLv3", "MIT AND GPLv2+",
                    "GNU General Public License v3", "GNU Affero General Public License v3",
                    "GNU General Public Licence v3", "GNU Affero General Public Licence v3",
                    "GNU Public License v2", "GNU General  Public License",
                    "GNU General Public\nLicense v3"):
            out = ng._deep_review([(path, lic)], [(lic, "/app/*", "r")])
            self.assertEqual(len(out), 1, lic)
            self.assertIn("accept line ignored", out[0], lic)
        for lic in ("LGPL-2.1;junk", "LGPLv3 junk", "GNU Lesser General Public License v3 junk",
                    "GNU Library General Public License junk",
                    "GNU Lesser General Public Licence junk", "Lesser GPL junk",
                    "Library GPL junk"):
            self.assertEqual(ng._deep_review([(path, lic)], [(lic, "/app/*", "r")]), [], lic)

    def test_d240_deep_nesting_is_denied_not_a_crash(self):
        lic = "(" * 400 + "MIT" + ")" * 400
        self.assertEqual(ng.classify(ng.Component("x", "1", lic, "t")), "denied")

    def test_d240_accept_line_still_silences_epl(self):
        path = "/app/vendor/zzz/LICENSE"
        acc = [("EPL-2.0", "/app/vendor/*", "r")]
        self.assertEqual(ng._deep_review([(path, "EPL-2.0")], acc), [])

    def test_d240_gpl_in_dev_only_path_allowed(self):
        rc, out = self._run(self._report(("/s/site-packages/pytest/LICENSE", "GPL-3.0-or-later")))
        self.assertEqual(rc, 0)
        self.assertEqual(ng._parse_dev_only("# c\n*/a/*\n\n"), ["*/a/*"])
        self.assertEqual(ng._deep_review([("/x/a/L", "GPL-3.0")], [], ["*/a/*"]), [])

    def test_accepted_glob_suppresses(self):
        glob = "*/site-packages/hypothesis/*"
        acc = ng._parse_accepted(f"# c\nEPL-2.0\t{glob}\tweak copyleft, tests only\n")
        self.assertEqual(acc, [("EPL-2.0", glob, "weak copyleft, tests only")])
        f = [("/a/site-packages/hypothesis/LICENSE", "EPL-2.0"),
             ("/a/site-packages/other/LICENSE", "EPL-2.0")]
        self.assertEqual(len(ng._deep_review(f, acc)), 1)
        self.assertEqual(ng._deep_review(f[:1], acc), [])

    def test_empty_name_denied(self):
        rc, out = self._run(self._report(("/x/mystery/LICENSE", "")))
        self.assertEqual(rc, 1)
        self.assertIn("unknown", out)

    def test_bad_reports_exit_2(self):
        self.assertEqual(self._run("{not json")[0], 2)
        self.assertEqual(self._run('{"x": 1}')[0], 2)
        self.assertEqual(ng.main(["--deep-review", "/nonexistent/trivy.json"], env={}), 2)

    def test_no_report_given_exits_2(self):
        rc, out = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("no report given", out)

    def test_empty_results_clean(self):
        self.assertEqual(self._run('{"Results": []}')[0], 0)

    def test_summary_file_written(self):
        with tempfile.TemporaryDirectory() as d:
            s = Path(d) / "summary.md"
            rpt = self._report(("/p/GPL/LICENSE", "GPL-3.0"))
            rc, _ = self._run(rpt, env={"GITHUB_STEP_SUMMARY": str(s)})
            self.assertEqual(rc, 1)
            self.assertIn("GPL-3.0", s.read_text())

    def test_workflow_wiring(self):
        wf = Path(__file__).resolve().parents[2] / ".github" / "workflows"
        img = (wf / "image.yml").read_text()
        self.assertIn("--deep-review", img)
        pin = re.search(r"aquasecurity/trivy-action@\S+", (wf / "verify.yml").read_text()).group(0)
        self.assertIn(pin, img)


if __name__ == "__main__":
    unittest.main()
