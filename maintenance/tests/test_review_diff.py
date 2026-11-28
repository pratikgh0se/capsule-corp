"""The reviewer's diff packet (decision row 125, C6).

The reviewer profile lost `terminal` and `code_execution` on 2026-09-09 (C0
item 6 / decision row 120), so `legacy/profiles/reviewer/SOUL.md`'s old step 2
- run `git diff origin/<base>...HEAD` in the worktree - names a shell that
does not exist any more. The host produces the diff instead:
`board_bridge.write_review_diff()` writes it into the repo mount the reviewer
already gets read-only, and the packet (a `diff: ` comment on the impl card,
posted by the quality hook) names the file by path.

These are contract tests over that seam: the packet names a file that exists
inside the mount, the diff is scrubbed, secret-shaped paths never enter it at
all, nothing is ever truncated (fix round 1 removed the old 128 KB cap), and
a repo with no branch yields a packet that SAYS so instead of failing the
handoff.

Real git repositories in a temp dir - no network, no origin, no docker.
"""
import os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path[:0] = [str(Path(__file__).resolve().parents[n]) for n in (1, 2)]
import capsule.tests  # noqa: E402,F401 - its __init__ installs the swept TMPDIR (tmpclean)
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")

import board_bridge as bb
import contracts as ct
import scrub
import vault_git

# A token shaped exactly like scrub.py's telegram-token pattern, planted in a
# fixture branch so the assertion is "the packet masked a real-shaped secret",
# not "the packet masked a word".
FAKE_TOKEN = "8123456789:AAF" + ("x" * 32)
FAKE_KV = "TELEGRAM_BOT_TOKEN=" + FAKE_TOKEN


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                        "-C", str(cwd)] + list(args),
                       capture_output=True, text=True, timeout=60)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args} failed in {cwd}: {r.stderr or r.stdout}")
    return r


class _Repo:
    """A repo on `main` with a `sprint/<slug>` branch checked out."""

    def __init__(self, files, base_files=None, slug="timer"):
        self.root = Path(tempfile.mkdtemp(prefix="c6-diff-repo-"))
        self.slug = slug
        _git(self.root, "init", "-q", "-b", "main")
        for name, text in (base_files or {"README.md": "hello\n"}).items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "base")
        _git(self.root, "checkout", "-q", "-b", f"sprint/{slug}")
        for name, text in files.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "--allow-empty", "-m", "the change")

    def write(self, **kw):
        kw.setdefault("repo_root", str(self.root))
        kw.setdefault("worktree", str(self.root))
        kw.setdefault("slug", self.slug)
        kw.setdefault("base_ref", "main")
        return bb.write_review_diff(**kw)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class PacketNamesAFileThatExists(unittest.TestCase):
    def setUp(self):
        self.repo = _Repo({"server.py": "def f():\n    return 1\n"})
        self.addCleanup(self.repo.cleanup)

    def test_the_line_carries_the_packet_prefix_and_the_manifest_path(self):
        """The comment now names MANIFEST.md, not branch.diff (task 3 / card
        23): read it first, then every per-file diff."""
        path, line = self.repo.write()
        self.assertTrue(line.startswith(ct.DIFF_PACKET), line)
        self.assertIn(str(path), line)
        self.assertEqual(Path(path).name, bb.REVIEW_MANIFEST_FILENAME)
        self.assertIn("per-file diffs in files/", line)

    def test_branch_diff_exists_and_holds_the_full_diff(self):
        path, _ = self.repo.write()
        diff_path = bb.review_packet_path(self.repo.root, self.repo.slug)
        self.assertTrue(diff_path.is_file(), f"{diff_path} was named but not written")
        text = diff_path.read_text(encoding="utf-8")
        self.assertIn("server.py", text)
        self.assertIn("+    return 1", text)

    def test_the_manifest_lists_the_file_with_its_size_and_files_path(self):
        path, _ = self.repo.write()
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("server.py", text)
        self.assertIn("files/000-server.py.diff", text)
        self.assertIn("nothing in this packet is truncated", text)

    def test_the_per_file_diff_exists_and_matches_the_files_content(self):
        path, _ = self.repo.write()
        files_dir = bb.review_files_dir(self.repo.root, self.repo.slug)
        entries = sorted(files_dir.glob("*.diff"))
        self.assertEqual(len(entries), 1, entries)
        text = entries[0].read_text(encoding="utf-8")
        self.assertIn("server.py", text)
        self.assertIn("+    return 1", text)

    def test_both_the_stat_and_the_context_diff_are_in_branch_diff(self):
        self.repo.write()
        text = bb.review_packet_path(self.repo.root, self.repo.slug).read_text(encoding="utf-8")
        self.assertIn("git diff --stat", text)
        self.assertIn(f"git diff -U{bb.REVIEW_DIFF_CONTEXT}", text)
        self.assertIn("1 file changed", text)
        self.assertIn("@@", text)

    def test_the_header_names_the_base_and_the_head_branch(self):
        self.repo.write()
        text = bb.review_packet_path(self.repo.root, self.repo.slug).read_text(encoding="utf-8")
        self.assertIn("base: main", text)
        self.assertIn("sprint/timer", text)

    def test_the_files_land_inside_the_repo_and_outside_every_worktree(self):
        """The reviewer's mount is the repo root read-only
        (legacy/profiles/reviewer/config.yaml docker_volumes), so the packet
        must be under it. It must NOT be inside a sprint worktree: qa's SOUL
        runs `git add review/<slug>`, which would commit the diff onto the
        branch and put round n's diff inside round n+1's diff."""
        path, _ = self.repo.write()
        for p in (Path(path), bb.review_packet_path(self.repo.root, self.repo.slug)):
            rel = p.relative_to(self.repo.root)          # raises if outside the mount
            self.assertEqual(rel.parts[0], ".worktrees")
            self.assertEqual(rel.parts[1], bb.REVIEW_PACKET_DIRNAME)
            self.assertNotIn("sprint-timer", rel.parts)

    def test_a_second_run_overwrites_rather_than_appends(self):
        p1, _ = self.repo.write()
        first = Path(p1).read_text(encoding="utf-8")
        p2, _ = self.repo.write()
        self.assertEqual(p1, p2)
        self.assertEqual(first, Path(p2).read_text(encoding="utf-8"))
        files_dir = bb.review_files_dir(self.repo.root, self.repo.slug)
        self.assertEqual(len(list(files_dir.glob("*.diff"))), 1)


class Scrubbed(unittest.TestCase):
    def test_a_planted_token_is_masked_in_the_packet(self):
        repo = _Repo({"conf.py": f'BOT = "{FAKE_KV}"\n'})
        self.addCleanup(repo.cleanup)
        path, _ = repo.write()
        text = Path(path).read_text(encoding="utf-8")
        diff_text = bb.review_packet_path(repo.root, repo.slug).read_text(encoding="utf-8")
        per_file = "".join(p.read_text(encoding="utf-8")
                           for p in bb.review_files_dir(repo.root, repo.slug).glob("*.diff"))
        for blob in (text, diff_text, per_file):
            self.assertNotIn(FAKE_TOKEN, blob)
            # scrub() is not asserted to be idempotent-clean here: `KEY=<redacted:...>`
            # still matches its own kv-secret pattern (the replacement is a \S+ value).
            # What matters is that the planted value is gone.
            self.assertNotIn("AAF", blob)
        self.assertIn("<redacted:", diff_text)
        self.assertIn("<redacted:", per_file)

    def test_secret_shaped_paths_are_withheld_whole(self):
        repo = _Repo({"ops.env": f"{FAKE_KV}\n", "server.py": "x = 1\n"})
        self.addCleanup(repo.cleanup)
        path, line = repo.write()
        text = Path(path).read_text(encoding="utf-8")
        diff_text = bb.review_packet_path(repo.root, repo.slug).read_text(encoding="utf-8")
        self.assertIn("ops.env", text)          # named as withheld in the manifest
        self.assertIn("WITHHELD", text)
        self.assertNotIn(FAKE_TOKEN, diff_text)
        self.assertNotIn("+TELEGRAM_BOT_TOKEN", diff_text)
        self.assertIn("withheld", line)
        self.assertIn("server.py", diff_text)   # the rest of the diff is still there
        for p in bb.review_files_dir(repo.root, repo.slug).glob("*.diff"):
            self.assertNotIn("ops.env", p.name)  # no per-file diff for a withheld path

    def test_the_deny_list_covers_vault_gits_secret_file_suffixes(self):
        """One denylist for secret-shaped paths: vault_git.PUSH_DENY is the
        existing one (row 46's GitHub boundary). Every file entry in it must
        also keep a file out of the reviewer's packet."""
        for entry in vault_git.PUSH_DENY:
            if entry.endswith("/"):
                continue                        # a vault directory, not a file shape
            self.assertIn(entry, bb.REVIEW_DIFF_DENY, entry)


class NoTruncationEver(unittest.TestCase):
    """U1 incident 23 / card t_f144d2fe: the old 128 KB cap silently dropped
    ~105-207 KB of diff across 65 and 43 files (four whole test files) on the
    sample-app epics. `write_review_diff` no longer caps anything; the packet
    reaches the whole-branch packet's no-content-cap-plus-manifest standard."""

    def setUp(self):
        # 3 files of ~80 KB each, well over the old 128 KB cap combined.
        self.big_text = {
            f"pkg/big_{n}.py": "".join(f"line_{n}_{i} = {i}\n" for i in range(4200))
            for n in range(3)
        }
        self.repo = _Repo(self.big_text)
        self.addCleanup(self.repo.cleanup)

    def test_the_old_cap_symbol_is_gone(self):
        self.assertFalse(hasattr(bb, "REVIEW_DIFF_CAP_BYTES"))
        self.assertFalse(hasattr(bb, "REVIEW_DIFF_TRUNCATED"))

    def test_branch_diff_is_well_over_the_old_cap_and_says_nothing_truncated(self):
        """The header's own "nothing in this packet is truncated" reassurance
        legitimately uses the word - what must never appear is the old marker
        shape or a claim that something WAS cut."""
        self.repo.write()
        text = bb.review_packet_path(self.repo.root, self.repo.slug).read_text(encoding="utf-8")
        self.assertGreater(len(text.encode("utf-8")), 128 * 1024)
        self.assertNotIn("TRUNCATED", text)
        self.assertNotIn("DIFF TRUNCATED", text)
        self.assertNotIn("bytes of\ndiff are NOT", text)

    def test_branch_diff_holds_every_files_full_body_byte_for_byte_after_scrub(self):
        """Recompute each file's own scrubbed diff directly with git, then
        confirm it appears WHOLE inside branch.diff - not a fragment of it."""
        expected = {}
        for name in self.big_text:
            rc, out = bb._diff_sh(["git", "diff", f"-U{bb.REVIEW_DIFF_CONTEXT}", "main...HEAD",
                                   "--", name], self.repo.root)
            self.assertEqual(rc, 0)
            expected[name] = scrub.scrub(out)
        self.repo.write()
        branch_text = bb.review_packet_path(self.repo.root, self.repo.slug).read_text(encoding="utf-8")
        for name, body in expected.items():
            self.assertIn(body, branch_text, name)

    def test_files_dir_has_exactly_one_diff_per_kept_file_with_matching_content(self):
        """Brief item 5 / review Important #3: each files/ entry's CONTENT
        must equal that file's own (scrubbed) diff body - not just its name
        appearing somewhere in the concatenation of all entries, which would
        pass even if two files' bodies were swapped or one was truncated."""
        path, _ = self.repo.write()
        files_dir = bb.review_files_dir(self.repo.root, self.repo.slug)
        entries = sorted(files_dir.glob("*.diff"))
        self.assertEqual(len(entries), len(self.big_text), entries)
        manifest = Path(path).read_text(encoding="utf-8")

        expected = {}
        for name in self.big_text:
            rc, out = bb._diff_sh(["git", "-c", "core.quotePath=false", "diff",
                                   f"-U{bb.REVIEW_DIFF_CONTEXT}", "--no-renames",
                                   "main...HEAD", "--", name], self.repo.root)
            self.assertEqual(rc, 0)
            expected[name] = scrub.scrub(out) or ""

        # Map each manifest row's "files/<n>-name.diff" back to the source
        # path via the manifest table itself, so this does not assume any
        # particular index ordering.
        for name, body in expected.items():
            row = next((l for l in manifest.splitlines() if l.startswith("| ") and name in l), None)
            self.assertIsNotNone(row, f"no manifest row for {name}: {manifest}")
            rel = row.split("|")[-2].strip()
            self.assertTrue(rel.startswith(f"{bb.REVIEW_FILES_DIRNAME}/"), rel)
            entry_path = files_dir.parent / rel
            self.assertEqual(entry_path.read_text(encoding="utf-8"), body, name)

    def test_manifest_lists_every_file_with_its_full_table_row(self):
        """Review Minor #8: a size number alone can match anywhere (including
        branch.diff's own byte count in the header) - assert the whole row."""
        path, _ = self.repo.write()
        manifest = Path(path).read_text(encoding="utf-8")
        files_dir = bb.review_files_dir(self.repo.root, self.repo.slug)
        for entry in sorted(files_dir.glob("*.diff")):
            rel = f"{bb.REVIEW_FILES_DIRNAME}/{entry.name}"
            size = entry.stat().st_size
            name = next(n for n in self.big_text if n in entry.read_text(encoding="utf-8"))
            expected_row = f"| {name} | {size} | 4200 | 0 | {rel} |"
            self.assertIn(expected_row, manifest, manifest)

    def test_manifest_is_written_last(self):
        order = []
        real = bb._atomic_write_text

        def spy(p, text, encoding="utf-8", root=None):
            order.append(Path(p).name)
            return real(p, text, encoding=encoding, root=root)

        with mock.patch.object(bb, "_atomic_write_text", side_effect=spy):
            self.repo.write()
        self.assertEqual(order[-1], bb.REVIEW_MANIFEST_FILENAME, order)
        self.assertGreater(len(order), 1)

    def test_stale_files_from_an_earlier_round_are_removed(self):
        files_dir = bb.review_files_dir(self.repo.root, self.repo.slug)
        files_dir.mkdir(parents=True, exist_ok=True)
        stale = files_dir / "999-stale-from-round-1.diff"
        stale.write_text("stale\n", encoding="utf-8")
        keep_me = files_dir / "not-a-diff.txt"
        keep_me.write_text("keep\n", encoding="utf-8")
        self.repo.write()
        self.assertFalse(stale.exists())
        self.assertTrue(keep_me.exists(), "a non-.diff file in files/ must not be swept")

    def test_the_old_manifest_is_unlinked_before_anything_else_is_touched(self):
        """Review Minor #5: if a later step fails (a per-file `git diff` call
        here), the function must return `diff: unavailable` with NO
        MANIFEST.md left on disk at all - never the previous round's
        manifest pointing at files this round already deleted/overwrote."""
        path1, _ = self.repo.write()
        self.assertTrue(Path(path1).is_file())

        real_sh = bb._diff_sh
        calls = {"n": 0}

        def failing_on_per_file(argv, cwd):
            # The first `-U<n>` diff call is the single combined call that
            # produces branch.diff's body; every `-U<n>` call after that is
            # one iteration of the per-file loop. Let the first through, fail
            # the second (the per-file loop's first - and here only - file).
            if any(a.startswith("-U") for a in argv):
                calls["n"] += 1
                if calls["n"] >= 2:
                    return 128, "fatal: simulated per-file failure"
            return real_sh(argv, cwd)

        path2, line2 = self.repo.write(sh=failing_on_per_file)
        self.assertIsNone(path2)
        self.assertTrue(line2.startswith(ct.DIFF_UNAVAILABLE), line2)
        self.assertFalse(Path(path1).exists(),
                         "the previous round's MANIFEST.md must be gone, not stale")


class SymlinkSafety(unittest.TestCase):
    """Review Important #2: `files/` (and its parent, `card/`) must never be
    written through if either is a symlink - a stale-cleanup or a fresh
    per-file write must never land outside the packet directory."""

    def setUp(self):
        self.repo = _Repo({"server.py": "x = 1\n"})
        self.addCleanup(self.repo.cleanup)
        self.outside = Path(tempfile.mkdtemp(prefix="c6-diff-outside-"))
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)

    def test_a_symlinked_files_dir_is_replaced_not_written_through(self):
        sentinel = self.outside / "sentinel-from-round-1.diff"
        sentinel.write_text("do not touch\n", encoding="utf-8")

        card_dir = bb.card_packet_dir(self.repo.root, self.repo.slug)
        card_dir.mkdir(parents=True, exist_ok=True)
        files_dir = bb.review_files_dir(self.repo.root, self.repo.slug)
        files_dir.symlink_to(self.outside, target_is_directory=True)
        self.assertTrue(files_dir.is_symlink())

        path, line = self.repo.write()
        self.assertIsNotNone(path, line)

        # The sentinel outside the packet must survive untouched.
        self.assertTrue(sentinel.is_file())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not touch\n")
        # Nothing new was written into the outside dir.
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), [sentinel.name])
        # files/ is now a real directory holding this round's own diff.
        real_files_dir = bb.review_files_dir(self.repo.root, self.repo.slug)
        self.assertFalse(real_files_dir.is_symlink())
        self.assertTrue(real_files_dir.is_dir())
        self.assertEqual(len(list(real_files_dir.glob("*.diff"))), 1)

    def test_a_symlinked_card_dir_is_replaced_not_written_through(self):
        sentinel = self.outside / "sentinel.diff"
        sentinel.write_text("do not touch\n", encoding="utf-8")

        parent = bb.review_packet_path(self.repo.root, self.repo.slug).parent.parent
        parent.mkdir(parents=True, exist_ok=True)
        card_dir = bb.card_packet_dir(self.repo.root, self.repo.slug)
        card_dir.symlink_to(self.outside, target_is_directory=True)
        self.assertTrue(card_dir.is_symlink())

        path, line = self.repo.write()
        self.assertIsNotNone(path, line)

        self.assertTrue(sentinel.is_file())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not touch\n")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), [sentinel.name])
        real_card_dir = bb.card_packet_dir(self.repo.root, self.repo.slug)
        self.assertFalse(real_card_dir.is_symlink())
        self.assertTrue((real_card_dir / bb.REVIEW_MANIFEST_FILENAME).is_file())


class SanitizedFilenames(unittest.TestCase):
    """Direct unit tests of the sanitiser: a real git repo cannot contain a
    tracked path with a literal '..' path component, so these exercise the
    function git would never actually be asked to name."""

    def test_a_path_with_traversal_segments_produces_a_safe_single_component(self):
        name = bb._sanitize_diff_filename(0, "a/../../evil")
        self.assertNotIn("/", name)
        self.assertTrue(name.endswith(".diff"))
        self.assertEqual(Path(name).name, name)   # no directory component at all

    def test_a_path_with_spaces_and_odd_characters_is_safe(self):
        name = bb._sanitize_diff_filename(1, "a dir/file name (v2)?.py")
        self.assertNotIn("/", name)
        self.assertNotIn(" ", name)
        self.assertNotIn("?", name)
        self.assertNotIn("(", name)

    def test_two_paths_that_sanitise_the_same_still_get_distinct_names(self):
        n0 = bb._sanitize_diff_filename(0, "a/b")
        n1 = bb._sanitize_diff_filename(1, "a/b")
        self.assertNotEqual(n0, n1)
        self.assertTrue(n0.startswith("000-"))
        self.assertTrue(n1.startswith("001-"))

    def test_a_very_long_path_is_bounded(self):
        name = bb._sanitize_diff_filename(0, "x" * 5000)
        self.assertLess(len(name), 200)


class NoBranch(unittest.TestCase):
    """A packet that says so beats a refused handoff: the reviewer can still
    judge the card body and the spec, and a missing diff is a finding it can
    raise - a blocked review is a wedged card."""

    def test_an_unresolvable_base_yields_an_unavailable_packet(self):
        repo = _Repo({"server.py": "x = 1\n"})
        self.addCleanup(repo.cleanup)
        path, line = repo.write(base_ref="origin/main")
        self.assertIsNone(path)
        self.assertTrue(line.startswith(ct.DIFF_UNAVAILABLE), line)
        self.assertIn("origin/main", line)

    def test_a_repo_with_no_commits_at_all_yields_an_unavailable_packet(self):
        root = Path(tempfile.mkdtemp(prefix="c6-diff-empty-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _git(root, "init", "-q", "-b", "main")
        path, line = bb.write_review_diff(repo_root=str(root), worktree=str(root),
                                          slug="timer", base_ref="main")
        self.assertIsNone(path)
        self.assertTrue(line.startswith(ct.DIFF_UNAVAILABLE), line)

    def test_a_worktree_that_is_not_a_repo_yields_an_unavailable_packet(self):
        root = Path(tempfile.mkdtemp(prefix="c6-diff-notrepo-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path, line = bb.write_review_diff(repo_root=str(root), worktree=str(root),
                                          slug="timer", base_ref="main")
        self.assertIsNone(path)
        self.assertTrue(line.startswith(ct.DIFF_UNAVAILABLE), line)
        self.assertFalse((root / ".worktrees").exists(), "no packet dir on the failure path")

    def test_an_unsafe_base_ref_never_reaches_git(self):
        repo = _Repo({"server.py": "x = 1\n"})
        self.addCleanup(repo.cleanup)
        path, line = repo.write(base_ref="main; rm -rf /")
        self.assertIsNone(path)
        self.assertTrue(line.startswith(ct.DIFF_UNAVAILABLE), line)

    def test_an_empty_diff_still_writes_a_packet(self):
        """Nothing changed on the branch (a re-review after a revert): the
        packet exists and says the diff is empty, so the reviewer knows the
        difference between "no changes" and "no diff produced"."""
        repo = _Repo({}, base_files={"README.md": "hello\n"})
        self.addCleanup(repo.cleanup)
        path, line = repo.write()
        self.assertIsNotNone(path)
        self.assertIn("no changed files", Path(path).read_text(encoding="utf-8"))
        self.assertIn("0 file", line)


class NoRenamesPinned(unittest.TestCase):
    """SOUL.src.md:13-16 claims `branch.diff`
    "holds the exact same content as every file under files/ combined". A
    rename shows as one path in `--name-only`/`kept`, but without
    `--no-renames` a per-file or combined `-U5` diff for that path can
    render as a similarity-based `rename from`/`rename to` hunk instead of
    the file's real body - the two would then disagree. Pin `--no-renames`
    on both the combined body call and the per-file call through a fake
    `sh` that records every argv, rather than relying on a real repo that
    happens not to contain a rename."""

    def _sh(self):
        calls = []

        def sh(argv, cwd=None):
            calls.append(list(argv))
            if "--name-only" in argv:
                return 0, "a.py\nb.py\n"
            if "--numstat" in argv:
                paths = argv[argv.index("--") + 1:]
                return 0, "".join(f"1\t1\t{p}\n" for p in paths)
            if "--stat" in argv:
                return 0, " a.py | 1 +\n"
            if any(a.startswith("-U") for a in argv):
                return 0, "diff --git a/a.py b/a.py\n+x\n"
            if "rev-parse" in argv:
                return 0, "sprint/thing\n"
            return 0, ""
        sh.calls = calls
        return sh

    def test_no_renames_on_combined_and_per_file_diff_calls(self):
        root = Path(tempfile.mkdtemp(prefix="c6-diff-norenames-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        sh = self._sh()
        path, line = bb.write_review_diff(repo_root=str(root), worktree=str(root),
                                          slug="thing", base_ref="main", sh=sh)
        self.assertIsNotNone(path, line)

        u_calls = [c for c in sh.calls if any(a.startswith("-U") for a in c)]
        # One combined body call, one per kept file (a.py, b.py) = 3 total.
        self.assertEqual(len(u_calls), 3, sh.calls)
        for call in u_calls:
            self.assertIn("--no-renames", call, call)


class Slug(unittest.TestCase):
    def test_a_bad_slug_is_refused_before_any_path_is_built(self):
        repo = _Repo({"server.py": "x = 1\n"})
        self.addCleanup(repo.cleanup)
        for bad in ("../escape", "a/b", "Timer!", ""):
            path, line = repo.write(slug=bad)
            self.assertIsNone(path, bad)
            self.assertTrue(line.startswith(ct.DIFF_UNAVAILABLE), line)


class BranchPacket(unittest.TestCase):
    def test_the_branch_writer_reuses_the_card_packets_deny_list(self):
        """One list, no second copy - and it still covers every file-suffix
        entry of vault_git.PUSH_DENY, which is row 46's GitHub boundary and
        what board_bridge.py:381-384 says this list mirrors.

        The trailing-slash PUSH_DENY entries (`state/`, `Transcripts/`,
        and the vault's editor-config folder) are path COMPONENTS in a notes vault, not file
        suffixes in a repo diff, so they are excluded on purpose.
        """
        import vault_git
        self.assertIn(".pem", bb.REVIEW_DIFF_DENY)
        for d in vault_git.PUSH_DENY:
            if not d.endswith("/"):
                self.assertIn(d, bb.REVIEW_DIFF_DENY, d)
        src = Path(bb.__file__).read_text(encoding="utf-8")
        self.assertEqual(src.count("REVIEW_DIFF_DENY = ("), 1)


if __name__ == "__main__":
    unittest.main()
