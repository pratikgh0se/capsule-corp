"""capsule/plists.py against its twin, deploy.sh's `render()` (lines 166-180).
Two oracles: the Python renderer, and the very sed expressions read out of deploy.sh's
render() and run by sed with stub values (source: deploy.sh lines 166-180). Output of both
must be identical for real templates; if someone edits one renderer and not the other, this
fails."""
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from capsule import paths, plists
from capsule.tests.public_tree import private_only

STUBS = {"AGENT_OPS_HOME": "/tmp/stub-home", "CAPSULE_LAUNCHD_PREFIX": "dev.stub",
         "LIVE": "/tmp/stub-repo", "OBSIDIAN_VAULT_PATH": "/tmp/stub-vault",
         "WORKER_IMAGE": "stub-image", "CAPSULE_JOBS_DIR": "/tmp/stub-jobs",
         "OSB_DIR": "/tmp/stub-skill"}
CAPSULE_HOME = "/tmp/stub-home/.local/share/capsule"       # deploy.sh's `__CAPSULE_HOME__` value
VALUES = {"__PREFIX__": "dev.stub", "__HOME__": "/tmp/stub-home", "__REPO__": "/tmp/stub-repo",
          "__CAPSULE_HOME__": CAPSULE_HOME, "__CONFIG__": "/tmp/stub-jobs"}   # CAPSULE_JOBS_DIR


def deploy_sed_expressions():
    """The `-e "s#...#...#g"` arguments of deploy.sh's render(), with its shell variables filled."""
    text = (paths.REPO / "deploy.sh").read_text()
    body = text[text.index("render() {"):]
    body = body[:body.index("\n}\n")]
    exprs = re.findall(r'-e "(s#[^"]*#g)"', body)
    for var, value in STUBS.items():
        exprs = [e.replace("${%s}" % var, value).replace("$" + var, value) for e in exprs]
    return exprs


class Parity(unittest.TestCase):
    def sed(self, template):
        args = [a for e in deploy_sed_expressions() for a in ("-e", e)]
        return subprocess.run(["sed", *args, str(template)], capture_output=True, text=True,
                              check=True).stdout

    @private_only
    def test_the_four_placeholders_are_the_ones_deploy_sh_fills(self):
        filled = {m for e in deploy_sed_expressions() for m in re.findall(r"__[A-Z_]+__", e)}
        self.assertTrue(set(plists.PLACEHOLDERS) <= filled, filled)
        self.assertTrue(all("$" not in e for e in deploy_sed_expressions()))   # all vars filled

    @private_only
    def test_real_templates_render_identically_through_both(self):
        names = ("__PREFIX__.capsule-corp-keeper-watch.plist",
                 "__PREFIX__.capsule-corp-heal.plist")
        for name in names:
            with self.subTest(name):
                template = paths.REPO / "launchd" / "templates" / name
                rendered = plists.render(template.read_text(), VALUES)
                self.assertEqual(self.sed(template), rendered)
                self.assertIn("dev.stub.capsule", rendered)          # the prefix really was filled


class Escaping(unittest.TestCase):
    """A path with `&`, `<` or a space must still give a valid plist (the sed twin cannot)."""
    ODD = {**VALUES, "__HOME__": "/home/a & b <c>", "__CAPSULE_HOME__": "/data/R&D home"}

    def rendered(self):
        name = "__PREFIX__.capsule-corp-keeper-watch.plist"
        template = paths.REPO / "launchd" / "templates" / name
        return plists.render(template.read_text(), self.ODD)

    def test_special_characters_are_escaped_and_the_plist_parses_back_to_the_value(self):
        text = self.rendered()
        self.assertIn("/home/a &amp; b &lt;c&gt;/.local/bin", text)
        self.assertNotIn("a & b", text)
        root = ET.fromstring(text)                                              # oracle 2
        strings = [s.text for s in root.iter("string")]
        self.assertIn("/data/R&D home/logs/keeper-watch.log", strings)
        self.assertIn("/data/R&D home", strings)                               # CAPSULE_HOME env

    def test_plutil_accepts_the_escaped_plist(self):
        if not shutil.which("plutil"):
            self.skipTest("plutil is macOS-only")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.plist"
            path.write_text(self.rendered())
            done = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class Refusals(unittest.TestCase):
    def test_a_placeholder_outside_the_four_is_an_error_naming_it(self):
        with self.assertRaisesRegex(ValueError, "__VAULT__"):
            plists.render("<string>__VAULT__</string>", VALUES)

    def test_a_missing_value_is_an_error_naming_it(self):
        with self.assertRaisesRegex(ValueError, "__REPO__"):
            plists.render("x", {k: v for k, v in VALUES.items() if k != "__REPO__"})

    def test_a_value_that_looks_like_a_placeholder_is_not_expanded_again(self):
        values = {**VALUES, "__HOME__": "/h/__REPO__"}
        self.assertEqual(plists.render("__HOME__ __REPO__", values), "/h/__REPO__ /tmp/stub-repo")


if __name__ == "__main__":
    unittest.main()
