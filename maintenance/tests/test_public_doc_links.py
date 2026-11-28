"""Shipped public docs link only to files release/public-paths.txt also ships.

Z1 intent: a markdown link from a shipped .md file to a repo file the allowlist does not ship
fails, naming doc and target; links to shipped files, folders with shipped files, web URLs and
in-page anchors pass. Z2 oracle: the fixed fixture trees below, plus the real allowlist at HEAD.
"""
import posixpath
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import public_lint  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)|^\s*\[[^\]]+\]:\s*(\S+)", re.M)
TICKED = re.compile(r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*)`")
FILE_LIKE = re.compile(r"/.|\.[A-Za-z]{1,5}$")   # a path or a file name; not a bare word
DOCS = re.compile(r"^(?!.*/tests/fixtures/).*\.md$")
# The renovate card names the target repo's own file, which this repo happens to track too.
EXEMPT = {("maintenance/templates/renovate-card.md", ".github/renovate.json")}
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
from capsule.tests.public_tree import private_only  # noqa: E402


def _exists(p, pool):
    return p in pool or any(f.startswith(p.rstrip("/") + "/") for f in pool)


def _refs(doc, text):
    """(target as written, candidate repo paths, is a markdown link) for each reference."""
    here = posixpath.dirname(doc)
    out = []
    for m in LINK.finditer(text):
        t = (m.group(1) or m.group(2)).split("#")[0].split("?")[0]
        if t and not re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I):
            out.append((t, [posixpath.normpath(posixpath.join(here, t))], True))
    for t in TICKED.findall(text):
        if FILE_LIKE.search(t):
            out.append((t, [posixpath.normpath(posixpath.join(b, t)) for b in ("", here)], False))
    return out


def broken_links(shipped, known, read):
    """`doc -> target` for each reference in a shipped .md that names a repo file (`known`:
    every tracked path) the allowlist does not ship: a markdown link, or a backticked path or
    file name that resolves to a tracked file or folder."""
    files, bad = set(shipped), set()
    for doc in sorted(n for n in files if DOCS.match(n)):
        for t, cands, is_link in _refs(doc, read(doc)):
            hit = [c for c in cands if _exists(c, known)]
            unshipped = not any(_exists(c, files) for c in (hit or cands))
            if (hit or is_link) and unshipped and (doc, t) not in EXEMPT:
                bad.add(f"{doc} -> {t}")
    return sorted(bad)


class Fixture(unittest.TestCase):
    TREE = {
        "README.md": "[a](docs/A.md) [dir](docs/) [web](https://x.test/y) [top](#top) `claude`",
        "docs/A.md": "[up](../README.md#x) [ref][r] `deploy.sh` `a/` `x.py`\n\n[r]: ../README.md",
        "docs/B.md": "[gone](../STATUS.md) [spec](specs/s.md \"t\") `deploy.sh` plain deploy.sh",
    }
    KNOWN = {"README.md", "docs/A.md", "docs/B.md", "deploy.sh", "STATUS.md"}

    def test_flags_unshipped_targets_only(self):
        read = self.TREE.get
        ok = ["README.md", "docs/A.md", "deploy.sh"]
        self.assertEqual(broken_links(ok, self.KNOWN, read), [])
        self.assertEqual(broken_links(["README.md", "docs/A.md"], self.KNOWN, read),
                         ["docs/A.md -> deploy.sh"])
        self.assertEqual(broken_links(ok + ["docs/B.md"], self.KNOWN, read),
                         ["docs/B.md -> ../STATUS.md", "docs/B.md -> specs/s.md"])


class RealAllowlist(unittest.TestCase):
    @private_only
    def test_every_shipped_doc_links_only_to_shipped_files(self):
        shipped = public_lint._public_files(REPO, "HEAD", REPO / "release/public-paths.txt")
        known = set(public_lint.git(REPO, "ls-tree", "-r", "--name-only", "-z",
                                    "HEAD").decode().split("\0"))
        bad = broken_links(shipped, known, lambda n: (REPO / n).read_text(encoding="utf-8"))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
