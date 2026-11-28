"""Row 638 e: the sandbox runner decides "wait" vs "red" when the container is down, and the Slack
listener (row 637) turns a Slack reply into an action. A worker that could edit either could turn a
red into a wait or make a forged reply act, so only the CoS or the owner may change them."""
import ast
import subprocess
import tempfile
import unittest
from pathlib import Path

from capsule import paths, workspace

CAPSULE = paths.REPO / "capsule"

# Modules a listener may import without being trust logic (the import walk below accepts them
# unprotected): each one needs a reason. Empty on purpose: capsule/paths.py is NOT exempt, because
# paths.REPO is the archive root and paths.py also fixes where contracts, intent examples and
# CAPSULE_HOME live; a listener that imports it must see paths.py protected (slice 11) first.
NOT_TRUST_LOGIC = ()

# What the sandbox runner (capsule/runners/sandbox.py) imports that stays writable: every other
# module in its import closure must be protected. An exempt module is listed with its reason and
# the walk does NOT follow its own imports (config.py alone pulls in ~41 modules). Residual risk,
# accepted: any module loaded in the same process (host.py, seatbelt.py, config.py's imports)
# could monkeypatch the runner at import time; protection covers direct edits to the decision
# code only, and a change to an exempt module lands through review.
SANDBOX_EXEMPT = {
    "capsule/paths.py": "core, edited by many cards; a changed paths.REPO only takes effect "
                        "after landing, which goes through review (row 639 a residual risk)",
    "capsule/config.py": "core, edited by many cards; same residual risk as paths.py",
    "capsule/runners/host.py": "imported only inside runner_for; lander and landing never pass a "
                               "runner, so the session always builds StrongRunner",
}

# What the Slack answer process (capsule/slack_answer.py) imports but never follows: both run only
# after (or instead of) the ask decision, so neither can change who or what an answer applies to.
ANSWER_EFFECTS = {
    "capsule/slack.py": "imported inside _handle after answer() returned, to post the reply",
    "capsule/brainstorm_intake.py": "imported inside the brainstorm branch, which is never an "
                                    "ask answer",
    "maintenance/messages.py": "imported inside _brainstorm_click only, to queue a brainstorm "
                               "for the morning brief; a brainstorm click is never an ask answer",
}
ANSWER_CLOSURE = ["capsule/__init__.py", "capsule/answer_base.py", "capsule/answer_scope.py",
                  "capsule/human.py", "capsule/jsonschema.py", "capsule/row_schemas.py",
                  *ANSWER_EFFECTS]


def listener_files():
    """Every `capsule/slack_listen*` module or package file (none exists today)."""
    out = []
    for entry in sorted(CAPSULE.glob("slack_listen*")):
        out += sorted(entry.rglob("*.py")) if entry.is_dir() else [entry]
    return [p for p in out if p.suffix == ".py"]


def capsule_imports(path):
    """The `capsule/...` and `maintenance/...` files `path` imports at any depth (module or function
    level, absolute or relative), relative to the repo root; a bare name resolves under
    maintenance/ too (docker_door puts it on `sys.path`); a name that is not such a file (stdlib,
    third party) is dropped."""
    names = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module
            if node.level:   # `from . import x` in capsule/models/y.py is capsule.models.x
                pkg = path.parent.relative_to(paths.REPO).parts
                pkg = pkg[:len(pkg) - (node.level - 1)]
                mod = ".".join([*pkg, *([node.module] if node.module else [])])
            names += [mod] + [f"{mod}.{a.name}" for a in node.names]
    found = []
    for name in names:
        for root in (paths.REPO, paths.REPO / "maintenance"):   # docker_door puts it on sys.path
            base = root / name.replace(".", "/")
            found += [str(c.relative_to(paths.REPO)) for c in (
                base.with_suffix(".py"), base / "__init__.py") if c.is_file()]
    return [f for f in found if f.startswith(("capsule/", "maintenance/"))]


def walk_imports(files, stop=()):
    """`capsule_imports` of each file in `files`, followed transitively; a module in `stop` is
    listed but its own imports are not followed."""
    seen, todo = set(), list(files)
    while todo:
        new = set(capsule_imports(todo.pop())) - seen
        seen |= new
        todo += [paths.REPO / f for f in sorted(new) if f not in stop]
    return sorted(seen)


def tracked_python():
    """Every git-tracked `*.py` of the repo, relative to the repo root."""
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=paths.REPO, capture_output=True,
                         text=True, check=True).stdout
    return out.split()


def _socket_mode_names(node):
    """Every name or string in `node` that could name Socket Mode."""
    names = [getattr(node, "id", ""), getattr(node, "attr", "")]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        names += [a.name for a in node.names] + [getattr(node, "module", None) or ""]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        names.append(node.value)   # importlib.import_module("slack_sdk.socket_mode"), a URL
    return names


def socket_mode_users(files, root=paths.REPO):
    """Files in `files` (relative to `root`) outside the listener and the `test_*.py` files of
    test dirs that import, name or call Socket Mode: any spelling containing "socketmode" or the
    `apps.connections.open` call that starts a session, ignoring case, `_`, `.` and `-` (so
    `apps_connections_open` counts). Only `*.py` files are scanned: an extensionless script or
    shell is not (accepted)."""
    users = []
    for rel in files:
        parts = Path(rel).parts
        if rel.startswith("capsule/slack_listen") or (
                "tests" in parts[:-1] and parts[-1].startswith("test_")):
            continue
        try:
            tree = ast.parse((Path(root) / rel).read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if any(_names_socket_mode(n) for n in _socket_mode_names(node)):
                users.append(rel)
                break
    return users


def _names_socket_mode(name):
    k = name.casefold().replace("_", "").replace(".", "").replace("-", "")
    return "socketmode" in k or "appsconnectionsopen" in k


class SandboxAndSlackProtected(unittest.TestCase):
    def test_a_worker_write_to_the_sandbox_runner_is_refused(self):
        path = "capsule/runners/sandbox.py"
        self.assertEqual(workspace.touches_protected([path, "capsule/tick.py"]), [path])

    def test_a_worker_write_to_the_slack_listener_is_refused(self):
        paths = ["capsule/slack_listen.py", "capsule/slack_listener.py",
                 "capsule/slack_listen_answers.py", "capsule/slack_listen/handlers.py"]
        self.assertEqual(workspace.touches_protected(paths + ["capsule/tick.py"]), paths)

    def test_a_shadowing_or_trust_data_path_is_refused(self):
        # a package dir or an extension module shadows sandbox.py; the allowlist is trust data;
        # bare capsule/slack_listen is the package dir or the module stem itself
        paths = ["capsule/runners/sandbox/__init__.py",
                 "capsule/runners/sandbox.cpython-313-darwin.so", "capsule/runners",
                 "capsule/slack_listen", "capsule/slack_listen/allowlist.json",
                 "capsule/slack_listen.abi3.so"]
        self.assertEqual(workspace.touches_protected(paths + ["capsule/tick.py"]), paths)

    def test_the_runner_by_function_is_refused(self):
        # docker.py's available/alive/exec rc and run flags decide the same outcome as sandbox.py;
        # a sys.modules shim in either package __init__ would shadow the runner; every docker call
        # goes StrongRunner._d -> docker_door.docker_run -> maintenance/events.py docker_run
        paths = ["capsule/runners/docker.py", "capsule/runners/__init__.py",
                 "capsule/__init__.py", "capsule/docker_door.py", "maintenance/events.py"]
        # a package dir or an extension module would shadow each module, as for sandbox.py
        for stem in ("capsule/runners/docker", "capsule/docker_door", "maintenance/events",
                     "maintenance/scrub"):
            paths += [f"{stem}/__init__.py", f"{stem}.cpython-313-darwin.so"]
        paths += ["capsule/runners/__init__.cpython-313-darwin.so",
                  "capsule/__init__.cpython-313-darwin.so", "maintenance/scrub.py"]
        self.assertEqual(workspace.touches_protected(paths + ["capsule/tick.py"]), paths)

    def test_scrub_is_refused(self):
        # events.py imports scrub at load; scrub's top-level code could rebind subprocess.run,
        # which events.docker_run looks up per call, so scrub decides every docker rc
        paths = ["maintenance/scrub.py", "maintenance/scrub/__init__.py",
                 "maintenance/scrub.cpython-313-darwin.so", "maintenance/scrub"]
        self.assertEqual(workspace.touches_protected(paths + ["maintenance/ledger.py"]), paths)

    def test_a_maintenance_file_named_after_a_stdlib_module_is_refused(self):
        # docker_door puts maintenance/ on sys.path, so such a file would import ahead of the stdlib
        shadows = ["maintenance/json.py", "maintenance/argparse/__init__.py",
                   "maintenance/re.cpython-313.pyc", "Maintenance/csv.py"]
        near = ["maintenance/ledger.py", "maintenance/heal.sh", "maintenance/jsonl_tools.py"]
        self.assertEqual(workspace.touches_protected(shadows + near), shadows)

    def test_a_rename_out_of_a_protected_path_lists_the_old_path(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"   # the clone layout: the git dir sits beside the worktree
            workspace.gitdir(ws).parent.mkdir(parents=True)

            def git(*a):
                return subprocess.run(["git", "-C", str(ws), *a], check=True,
                                      capture_output=True, text=True)

            subprocess.run(["git", "init", "-q", "-b", "main", str(ws),
                            f"--separate-git-dir={workspace.gitdir(ws)}"], check=True,
                           capture_output=True, text=True)
            git("config", "user.email", "t@t.test")
            git("config", "user.name", "t")
            (ws / "capsule/runners").mkdir(parents=True)
            (ws / "capsule/runners/sandbox.py").write_text("# the runner\n" * 20)
            git("add", "-A")
            git("commit", "-q", "-m", "base")
            base = git("rev-parse", "HEAD").stdout.strip()
            git("mv", "capsule/runners/sandbox.py", "notes.md")
            touched = workspace.changed(ws, base)
            self.assertIn("capsule/runners/sandbox.py", touched)
            self.assertEqual(workspace.touches_protected(touched), ["capsule/runners/sandbox.py"])

    def test_case_variants_are_refused(self):
        paths = ["Capsule/Runners/Sandbox.py", "capsule/SLACK_LISTEN.py",
                 "Capsule/Runners/Docker.py", "capsule/__INIT__.py"]
        self.assertEqual(workspace.touches_protected(paths), paths)

    def test_the_trust_path_modules_are_protected_with_their_shadows(self):
        """D125: a package dir or an extension module would shadow a protected module."""
        stems = ("slack_answer", "cos_ask", "human", "human_preflight", "answer_scope",
                 "slack_secret", "answer_base", "row_schemas", "jsonschema")
        shadows = [f"capsule/{s}{tail}" for s in stems
                   for tail in (".py", "/__init__.py", ".abi3.so")]
        self.assertEqual(workspace.touches_protected(shadows), shadows)

    def test_nearby_paths_stay_writable(self):
        neighbours = ["capsule/runners/host.py", "capsule/runners/sandbox_notes.md",
                      "capsule/slack.py", "capsule/slack_digest.py",
                      "capsule/tests/test_slack_listen.py"]
        self.assertEqual(workspace.touches_protected(neighbours), [])

    def test_a_relative_import_is_followed(self):
        """Non-vacuous: `from . import Result` in capsule/models/claude.py is capsule.models."""
        found = capsule_imports(paths.REPO / "capsule/models/claude.py")
        self.assertIn("capsule/models/__init__.py", found)

    def test_the_import_walk_is_transitive(self):
        """Non-vacuous: cos_ask imports cos_brief, which imports statusview inside a function."""
        found = walk_imports([paths.REPO / "capsule/cos_ask.py"])
        self.assertIn("capsule/cos_brief.py", found)
        self.assertIn("capsule/statusview.py", found)

    def test_every_capsule_import_of_the_listener_is_protected(self):
        """Vacuous while no capsule/slack_listen* file exists: the loop then runs zero times and
        the test passes. Once the listener lands, every capsule module it imports (transitively)
        must be in PROTECTED or on NOT_TRUST_LOGIC, so slice 11 cannot be forgotten."""
        closure = walk_imports(listener_files())
        self.assertIn("capsule/slack_secret.py", closure)    # not vacuous: the walk is real
        for module in closure:
            if module in NOT_TRUST_LOGIC:
                continue
            self.assertEqual(workspace.touches_protected([module]), [module],
                             f"the listener imports {module}, which a worker could rewrite")

    def test_every_module_the_sandbox_runner_imports_is_protected(self):
        """The runner's import closure (capsule/ plus the maintenance/ modules docker_door makes
        importable) is all protected except SANDBOX_EXEMPT, so a worker cannot change what the
        runner runs by editing a module it imports."""
        closure = walk_imports([paths.REPO / "capsule/runners/sandbox.py"], stop=SANDBOX_EXEMPT)
        for module in ("capsule/docker_door.py", "maintenance/events.py",
                       "maintenance/scrub.py"):   # non-vacuous
            self.assertIn(module, closure)
        self.assertIn("capsule/jobs_dir.py", closure)   # the config-dir rule, protected
        self.assertNotIn("capsule/host.py", closure)    # host.py (often edited) is out
        open_ = [m for m in closure
                 if m not in SANDBOX_EXEMPT and workspace.touches_protected([m]) != [m]]
        self.assertEqual(open_, [], "the sandbox runner imports these, a worker could rewrite them")
        self.assertEqual(sorted(set(SANDBOX_EXEMPT) - set(closure)), [],
                         "SANDBOX_EXEMPT lists a module the runner no longer imports")

    def test_the_answer_closure_is_pinned(self):
        """The decision path of the Slack answer process is exactly these modules, all protected;
        the two ANSWER_EFFECTS are listed but not followed. A new import fails here."""
        closure = walk_imports([paths.REPO / "capsule/slack_answer.py"], stop=ANSWER_EFFECTS)
        self.assertEqual(closure, sorted(ANSWER_CLOSURE))
        open_ = [m for m in closure if m not in ANSWER_EFFECTS
                 and workspace.touches_protected([m]) != [m]]
        self.assertEqual(open_, [], "the answer path imports these, a worker could rewrite them")
        self.assertEqual(sorted(set(ANSWER_EFFECTS) - set(closure)), [],
                         "ANSWER_EFFECTS lists a module the answer path no longer imports")

    def test_the_human_verb_closure_is_protected(self):
        """`capsule human` writes the same decision file: its imports are protected too."""
        closure = walk_imports([paths.REPO / "capsule/human.py"])
        self.assertIn("capsule/answer_base.py", closure)
        open_ = [m for m in closure if workspace.touches_protected([m]) != [m]]
        self.assertEqual(open_, [], "capsule human imports these, a worker could rewrite them")

    def test_only_the_listener_uses_socket_mode(self):
        self.assertEqual(socket_mode_users(tracked_python()), [],
                         "Socket Mode belongs in capsule/slack_listen* only")

    def test_socket_mode_in_any_spelling_is_found(self):
        """A fake repo with one probe per spelling; the listener and test dirs stay exempt."""
        probes = {
            "bin/a.py": "from slack_bolt.adapter.socket_mode import SocketModeHandler\n",
            "maintenance/b.py": ("import importlib\n"
                                 "importlib.import_module('slack_sdk.socket_mode')\n"),
            "capsule/c.py": "URL = 'https://slack.com/api/apps.connections.open'\n",
            "observability/d.py": ("import slack_sdk\n"
                                   "client = slack_sdk.socket_mode.SocketModeClient\n"),
            "capsule/slack_listen/ok.py": "from slack_sdk.socket_mode import SocketModeClient\n",
            "capsule/tests/test_ok.py": "from slack_sdk.socket_mode import SocketModeClient\n",
            "capsule/plain.py": "import slack_sdk\nweb = slack_sdk.WebClient\n",
            "capsule/e.py": ("import slack_sdk\n"
                             "slack_sdk.WebClient().apps_connections_open(app_token='x')\n"),
            "tests/helper.py": "from slack_sdk.socket_mode import SocketModeClient\n",
        }
        with tempfile.TemporaryDirectory() as td:
            for rel, text in probes.items():
                (Path(td) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(td) / rel).write_text(text)
            self.assertEqual(socket_mode_users(sorted(probes), root=td),
                             ["bin/a.py", "capsule/c.py", "capsule/e.py", "maintenance/b.py",
                              "observability/d.py", "tests/helper.py"])


if __name__ == "__main__":
    unittest.main()
