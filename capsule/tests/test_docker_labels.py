"""Every Docker object Capsule code or a test creates carries the label scheme.

`capsule=1` plus `capsule.kind=test|run|image` on each `docker run/create/build/volume create`,
so the cleanup scripts (maintenance/storage_gc.py, image_gc.py) can remove by label alone instead
of guessing from names. Shell is scanned by text (comment lines skipped, `\\` continuations
joined); Python by AST, so docstrings that mention `docker run` are ignored. capsule/tests and
maintenance/tests only fake docker (never create anything real) and are not scanned.
"""
import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("capsule", "maintenance", "bin", "tests", "docker", ".github", "deploy.sh")
FAKES = (("capsule", "tests"), ("maintenance", "tests"))
SKIP = {"__pycache__", "fixtures", "node_modules"}
# Out of scope on purpose: the live observability compose stack (observability/ is not in ROOTS,
# and `docker compose` is not a creating verb here); a label change recreates live containers.
CREATES = re.compile(r"\bdocker\s+(?:(?:container\s+)?(?:run|create)"
                     r"|(?:image\s+|buildx\s+)?build|volume\s+create)\b")
# A label as its own token after `--label` (space, `=` or the quote/comma of a Python argv) or a
# Dockerfile-style `LABEL`; `volume-label=` never counts (that labels the volume, not the object).
_FLAG = r"(?:--label|\bLABEL)[\s=\"',]+"
ONE = re.compile(_FLAG + r"capsule=1(?![\w.])")
KIND = re.compile(_FLAG + r"capsule\.kind=(?:test|run|image)(?![\w.])")
PY_VERBS = {"run", "create", "build", "buildx"}


def labelled(segment):
    return bool(ONE.search(segment) and KIND.search(segment))


def shell_sites(text):
    """(line number, command text) of each docker creating command in shell/yaml text."""
    sites, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        start, logical = i + 1, lines[i]
        while logical.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            logical = logical.rstrip()[:-1] + " " + lines[i]
        i += 1
        if logical.lstrip().startswith("#"):
            continue
        hits = list(CREATES.finditer(logical))
        ends = [h.start() for h in hits[1:]] + [len(logical)]
        sites += [(start, logical[h.start():end]) for h, end in zip(hits, ends)]
    return sites


def py_sites(text):
    """(line number, source) of each `["docker", "run", ...]` list literal in Python source, and of
    each `docker_run(["run", ...])` first argument (docker_run's argv has no leading "docker")."""
    sites, tree = [], ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and len(node.elts) > 1:
            first, second = (e.value if isinstance(e, ast.Constant) else None
                             for e in node.elts[:2])
            if first == "docker" and second in PY_VERBS | {"volume"}:
                sites.append((node.lineno, ast.get_source_segment(text, node)))
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.List):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            verbs = [e.value for e in node.args[0].elts[:2] if isinstance(e, ast.Constant)]
            creates = verbs[:1] and (verbs[0] in PY_VERBS or verbs == ["volume", "create"])
            if name == "docker_run" and creates:
                sites.append((node.lineno, ast.get_source_segment(text, node.args[0])))
    return sites


def repo_sites():
    """{relative path: [(line, text)]} for every file in ROOTS that creates a Docker object."""
    found = {}
    for root in ROOTS:
        base = REPO / root
        for path in sorted([base] if base.is_file() else base.rglob("*")):
            rel = path.relative_to(REPO)
            if not path.is_file() or SKIP & set(rel.parts) or rel.parts[:2] in FAKES:
                continue
            if path.suffix == ".py":
                scan = py_sites
            elif path.suffix in (".sh", ".yml", ".yaml") or (root == "bin" and not path.suffix):
                scan = shell_sites
            else:
                continue
            sites = scan(path.read_text(errors="ignore"))
            if sites:
                found[rel.as_posix()] = sites
    return found


class DockerLabels(unittest.TestCase):
    def test_scanner_flags_a_bare_command_and_passes_a_labelled_one(self):
        bare = shell_sites("x\ndocker run --rm img\n# docker run in a comment\n")
        self.assertEqual([(2, "docker run --rm img")], bare)
        ok = "docker build --label capsule=1 \\\n  --label capsule.kind=image .\n"
        self.assertTrue(labelled(shell_sites(ok)[0][1]))
        self.assertFalse(labelled("docker run --label capsule=1 img"))
        both = "--label capsule.kind=test"
        self.assertFalse(labelled(f"docker run --label capsule=10 {both} img"))
        self.assertFalse(labelled(f"docker run --mount x,volume-label=capsule=1 {both} img"))
        self.assertTrue(labelled("docker run --label=capsule=1 --label=capsule.kind=run img"))
        argv = '["docker", "run", "--label", "capsule=1", "--label", "capsule.kind=run"]'
        self.assertTrue(labelled(argv))
        self.assertEqual(1, len(py_sites('x = ["docker", "run", "-d"]\n"""docker run"""\n')))
        self.assertEqual(1, len(py_sites('events.docker_run(["run", "--rm", "img"])\n')))
        self.assertEqual([], py_sites('docker_run(["ps", "-q"])\nother(["run"])\n'))
        for verb in ("docker container run", "docker create", "docker image build",
                     "docker buildx build", "docker volume create"):
            self.assertEqual(1, len(shell_sites(f"{verb} x\n")), verb)
        self.assertEqual([], shell_sites("docker builder prune -f\ndocker volume ls\n"))

    def test_known_sites_are_found(self):
        found = repo_sites()
        for rel in ("capsule/runners/docker.py", "maintenance/merge_gate.py",
                    "docker/build-base.sh", "tests/test_docker_image.sh",
                    "tests/test_migration_pg.sh"):
            self.assertIn(rel, found, f"scanner missed the docker sites in {rel}")

    def test_every_created_docker_object_is_labelled(self):
        bad = [f"{rel}:{line}: {text[:70]}" for rel, sites in repo_sites().items()
               for line, text in sites if not labelled(text)]
        self.assertEqual([], bad, "docker run/create/build without capsule=1 and capsule.kind=")


if __name__ == "__main__":
    unittest.main()
