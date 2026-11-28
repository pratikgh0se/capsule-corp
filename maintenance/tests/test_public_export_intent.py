"""maintenance/public_export.py (P10a), Z1 intent cases. Each case below carries its built-in
expectation and runs always; the loader test replays the shipped rows of
contracts/intent/public_export.jsonl against the same cases and skips until that file exists."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capsule import intent  # noqa: E402
from public_export_fixture import Fixture, git  # noqa: E402

HISTORY = {"exit": 2, "written": False}
SYMLINK = {"exit": 1, "dest_touched": False}
DENYLIST = {"exit": 2, "dest_written": False, "identity": "export@capsule.invalid", "tz": "+0000"}


class Cases(Fixture):
    def snapshot(self, dest):
        """What DEST holds: its files and its head (None when it is not a repo or missing)."""
        files = sorted(str(p.relative_to(dest)) for p in Path(dest).rglob("*")
                       if p.is_file() and ".git" not in p.parts) if Path(dest).exists() else None
        head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip() if Path(dest, ".git").exists() else None
        return files, head

    def history_not_export_only(self):
        """A clone of the private repo, a sibling worktree of it, and a repo holding a foreign
        commit: each must be refused with exit 2 and leave DEST as it was."""
        git(self.root, "clone", "-q", str(self.repo), str(self.root / "clone"))
        git(self.repo, "worktree", "add", "-q", "-b", "sib", str(self.root / "sibling"))
        foreign = self.root / "foreign"
        foreign.mkdir()
        git(foreign, "init", "-q", "-b", "main")
        for key, val in (("user.email", "me@private.example"), ("user.name", "Me")):
            git(foreign, "config", key, val)
        (foreign / "x.txt").write_text("x")
        git(foreign, "add", "-A")
        git(foreign, "commit", "-q", "-m", "mine")
        codes, written = set(), False
        for name in ("clone", "sibling", "foreign"):
            dest = self.root / name
            before = self.snapshot(dest)
            codes.add(self.export(self.sha1, dest=dest)[0])
            written = written or self.snapshot(dest) != before
        return {"exit": codes.pop() if len(codes) == 1 else sorted(codes), "written": written}

    def symlink_outside_the_tree(self):
        """An allowlisted symlink that is absolute, escapes the tree, or goes through another
        link blocks the export (exit 1) and DEST is never created."""
        variants = ({"capsule/l": "/etc/hosts"}, {"capsule/l": "../../x"},
                    {"capsule/l1": "a.py", "capsule/l2": "l1"})
        codes, touched = set(), False
        for i, links in enumerate(variants):
            for old in ("capsule/l", "capsule/l1", "capsule/l2"):
                if (self.repo / old).is_symlink():
                    (self.repo / old).unlink()
            for name, target in links.items():
                os.symlink(target, self.repo / name)
            sha = self.commit(f"links {i}")
            dest = self.root / f"links-{i}"
            codes.add(self.export(sha, dest=dest)[0])
            touched = touched or dest.exists()
        return {"exit": codes.pop() if len(codes) == 1 else sorted(codes), "dest_touched": touched}

    def denylist_and_identity(self):
        """No private denylist: exit 2, fail closed, nothing written. With one, a commit made under
        a personal git config and environment carries only the generic identity at +0000."""
        rc, _, _ = self.export(self.sha1, deny=False)
        written = self.dest.exists()
        gconf = self.root / "personal-gitconfig"
        gconf.write_text("[user]\n\temail = real@private.example\n\tname = Real Person\n"
                         "[author]\n\temail = a@private.example\n")
        env = {"GIT_CONFIG_GLOBAL": str(gconf), "GIT_AUTHOR_EMAIL": "env@private.example",
               "GIT_COMMITTER_EMAIL": "env2@private.example"}
        with mock.patch.dict(os.environ, env):
            self.export(self.sha1)
        who = git(self.dest, "log", "-1", "--format=%ae|%ce|%an|%cn")
        when = git(self.dest, "log", "-1", "--format=%ai|%ci")
        generic = who == ("export@capsule.invalid|export@capsule.invalid|"
                          "capsule-export|capsule-export")
        utc = all(t.endswith("+0000") for t in when.split("|"))
        return {"exit": rc, "dest_written": written,
                "identity": "export@capsule.invalid" if generic else who,
                "tz": "+0000" if utc else when}

    def test_history_not_export_only_is_refused(self):
        self.assertEqual(self.history_not_export_only(), HISTORY)

    def test_symlink_outside_the_tree_blocks_the_export(self):
        self.assertEqual(self.symlink_outside_the_tree(), SYMLINK)

    def test_missing_denylist_refuses_and_identity_is_generic(self):
        self.assertEqual(self.denylist_and_identity(), DENYLIST)

    def test_shipped_rows_agree_with_the_built_in_cases(self):
        rows = intent.load("public_export")
        cases = {"history_not_export_only": (self.history_not_export_only, HISTORY),
                 "symlink_outside_the_tree": (self.symlink_outside_the_tree, SYMLINK),
                 "missing_denylist_and_identity": (self.denylist_and_identity, DENYLIST)}
        self.assertEqual(len(rows), 3)
        for ex in rows:
            run, builtin = cases[ex["given"]["document"]["case"]]
            self.assertEqual(ex["expect"], builtin, ex["id"])
            self.setUp()
            self.assertEqual(run(), ex["expect"], ex["id"])


if __name__ == "__main__":
    unittest.main()
