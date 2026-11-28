# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   NoInboundLinks.test_nothing_outside_the_mirror_links_a_per_role_mirror_path
"""Zero inbound links to the per-role mirror paths (row 128's condition on the
deletion). If anything outside the mirror wikilinks a
`Architecture/skills/<retired system>/<role>/<skill>` path, the deletion breaks a link
and this test says so BEFORE the commit that cannot be un-broken cheaply.

The notes vault comes ONLY from its env var: the suite never reads the real
vault on its own. The controller runs this once by hand against the real vault
before the deletion commit (C4/Task 8). `Scan` proves the scan itself on a temp vault."""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests.private_names import retired_only, retired_word  # noqa: E402

_ENV = os.environ.get("OBSIDIAN_VAULT_PATH")
VAULT = Path(_ENV) if _ENV else None
# `[[Architecture/skills/<retired system>/<role>/<skill>]]`, with or without an alias,
# and the bare markdown-link form.
_LINK_RE = re.compile(rf"(?:\[\[|\]\(|\()\s*(?:\./)?"
                      rf"(Architecture/skills/{retired_word()}/[^/\]\)|#]+/[^\]\)|#]+)")
_SKIP = {".obsidian", ".git", ".trash", "_trash", ".claude", "copilot",
         ".smart-env", ".claude-runs", "node_modules", "__pycache__"}


class NoInboundLinks(unittest.TestCase):
    @retired_only   # reads the owner's real vault folder, named by the private denylist
    def test_nothing_outside_the_mirror_links_a_per_role_mirror_path(self):
        if VAULT is None:
            self.skipTest("OBSIDIAN_VAULT_PATH unset")
        mirror = VAULT / "Architecture" / "skills" / retired_word()
        if not mirror.is_dir():
            self.skipTest("mirror not present")
        roles = {p.name for p in mirror.iterdir() if p.is_dir()}
        if not roles:
            self.skipTest("no per-role directories left - deletion already done")
        offenders = []
        for md in VAULT.rglob("*.md"):
            rel = md.relative_to(VAULT).as_posix()
            if any(part in _SKIP for part in md.relative_to(VAULT).parts):
                continue
            if rel.startswith(f"Architecture/skills/{retired_word()}/"):
                continue                       # inside the mirror: deleted with it
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for target in _LINK_RE.findall(text):
                if target.split("/")[3] in roles:
                    offenders.append(f"{rel} -> {target}")
        self.assertEqual(offenders, [], "inbound links to per-role mirror paths: "
                                        + "; ".join(offenders[:20]))


if __name__ == "__main__":
    unittest.main()
