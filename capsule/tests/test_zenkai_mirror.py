"""docs/ZENKAI.md is a verbatim mirror of the vault's Zenkai Rules (rows 454, 455): its header's
body_sha256 must match the body below it, so an edit to the mirror alone fails here."""
import hashlib
import re
import unittest

from capsule import paths, workspace

MIRROR = paths.REPO / "docs" / "ZENKAI.md"
HEADER = re.compile(r"^Mirror of vault Knowledge/Capsule Corp - Zenkai Rules\.md, body_sha256 ([0-9a-f]{64})$")


class ZenkaiMirror(unittest.TestCase):
    def test_body_matches_header_sha(self):
        header, blank, body = MIRROR.read_text(encoding="utf-8").split("\n", 2)
        m = HEADER.match(header)
        self.assertIsNotNone(m, f"bad header line: {header!r}")
        self.assertEqual(blank, "")
        self.assertEqual(hashlib.sha256(body.encode("utf-8")).hexdigest(), m.group(1))
        self.assertTrue(body.rstrip().endswith("Zenkai Rules v2 (2026-10-20)"))

    def test_mirror_is_a_protected_path(self):
        self.assertEqual(workspace.touches_protected(["docs/ZENKAI.md", "docs/RUNBOOK.md"]), ["docs/ZENKAI.md"])


if __name__ == "__main__":
    unittest.main()
