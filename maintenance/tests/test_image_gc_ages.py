"""image_gc age rules. Rule A: a capsule=1 image with capsule.kind test or run that no container
uses goes after 24 hours (kind image, or no kind, never by this rule). Rule B, the one-time legacy
sweep: an untagged capsule-layer image, or exactly capsule-corp-worker:test, made before
LEGACY_BEFORE and over 24 hours old, unused, goes; after LEGACY_BEFORE it selects nothing. An
intermediate layer is never listed, so never a candidate; before each removal the verdict is
re-made from a fresh inspect.

Oracle 1: the fake docker's image store after a real image_gc.py run (Rule A, and the argv of the
legacy untag). Oracle 2: a hand-written list of what must remain, and the verdicts of the pure
decision function at a fixed clock, so no case depends on today's date.
"""
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_stack_fakes import ROOT, Fake, digest, image  # noqa: E402

sys.path.insert(0, str(ROOT / "maintenance"))
import image_gc  # noqa: E402

KIND, WORKER = "capsule.kind", "capsule-corp-worker:2026-10-20-2c73797"


def tagged(kind):
    return {"capsule": "1", KIND: kind}


class RuleAEndToEnd(Fake):
    def setUp(self):
        super().setUp()
        self.lock.write_text(f"capsule-base@{digest('a')}\ncapsule-web@{digest('c')}\n")
        for args in (("init", "-q"), ("add", "IMAGES.lock"), ("commit", "-q", "-m", "lock")):
            ident = ("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false")
            subprocess.run(["git", "-C", str(self.tmp), *ident, *args], check=True,
                           capture_output=True)
        self.store([
            image("1", None, 3, labels=tagged("test")),                         # goes
            image("2", None, 3, ["capsule-corp-worker:test"], labels=tagged("run")),   # goes
            image("3", None, 0.5, labels=tagged("test")),                       # under 24 hours
            image("4", None, 9, labels=tagged("test")),                         # in use
            image("5", None, 17, [WORKER], labels=tagged("image")),             # the real worker
            image("6", None, 9, labels={"capsule": "1"}),                       # no kind
            image("7", None, 9, ["myapp:dev"], labels=tagged("test")),          # tag outside
            image("8", None, 9, ["capsule-x:1", "capsule-x:2"], labels=tagged("test")),  # 2 tags
            image("9", None, 17, ["capsule-corp-worker:2026-10-20-2c73797x"]),  # unlabelled
            image("a", "base", 40, ["capsule-base:local"], labels=tagged("test")),   # in the lock
            image("c", None, 9, labels=tagged("test")),                          # in the lock
        ], boxes=[digest("4")])

    def test_apply_removes_only_old_unused_test_and_run_images_and_pins_every_refusal(self):
        r = self.gc("1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.left(), set("3456789ac"))
        rm = [c for c in self.calls() if c.startswith("image rm")]
        # 2 carries only the worker:test tag, so it goes by that exact tag
        self.assertEqual(sorted(rm), ["image rm capsule-corp-worker:test",
                                      f"image rm {digest('1')}"])
        for kept in ("5", "9"):               # the worker images are never even mentioned
            self.assertFalse([c for c in rm if digest(kept) in c])

    def test_dry_run_changes_nothing_and_says_what_it_would_remove(self):
        r = self.gc()
        self.assertEqual(len(self.left()), 11)
        self.assertIn("would remove " + "1" * 12, r.stdout)
        self.assertIn("would remove " + "2" * 12, r.stdout)
        self.assertIn("keep " + "5" * 12 + f" {WORKER} (no age rule applies)", r.stdout)
        self.assertNotIn("[legacy]", r.stdout)

    def test_the_legacy_tag_is_removed_by_that_exact_tag_and_nothing_else_goes(self):
        old = image("1", None, 20, ["capsule-corp-worker:test"])
        old["Created"] = "2026-10-01T00:00:00.123456789Z"
        self.store([old, image("9", None, 20, [WORKER])], boxes=[])
        r = self.gc("1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.left(), {"9"})
        self.assertEqual([c for c in self.calls() if c.startswith("image rm")],
                         ["image rm capsule-corp-worker:test"])
        self.assertIn("[legacy]", r.stdout)

    def test_an_intermediate_layer_is_never_a_candidate_whatever_its_age(self):
        def layer(ch, age, **kw):
            return image(ch, "base", age, **kw)
        parents = [dict(layer("i", 100), Intermediate=True),
                   dict(layer("j", 15), Intermediate=True)]
        self.store(parents + [layer("k", 100)], boxes=[])      # k: a top-level dangling control
        r = self.gc("1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.left(), {"i", "j"})
        self.assertEqual([c for c in self.calls() if c.startswith("image rm")],
                         [f"image rm {digest('k')}"])
        self.assertNotIn("FAILED", r.stdout)
        self.assertNotIn("i" * 12, r.stdout)                   # never even listed


NOW =datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def verdict(now=NOW, **kw):
    """image_gc._keep_reason at a fixed clock; reason None means 'would remove'."""
    locked, previous, used = kw.pop("sets", (set(), set(), set()))
    made = now - timedelta(**{"days": 5, **kw.pop("age", {})})
    img = {"id": digest("x"), "labels": {"capsule-layer": "base"}, "refs": [], "digests": [],
           "size": 1, "created": made, "built": made, **kw}
    return image_gc._keep_reason(img, locked, previous, used, now)


class Legacy(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(image_gc, "LEGACY_BEFORE",
                                            datetime(2026, 11, 2, tzinfo=timezone.utc)))

    def test_an_old_unused_untagged_capsule_layer_image_is_no_longer_recent(self):
        self.assertIsNone(verdict())

    def test_under_24_hours_it_is_still_recent(self):
        self.assertEqual(verdict(age={"days": 0, "hours": 12}), "recent")

    def test_the_sweep_selects_nothing_made_after_the_cut_over(self):
        later = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.assertEqual(verdict(later, age={"days": 2}), "recent")     # made 11-03
        self.assertIsNone(verdict(later, age={"days": 4}))              # made 11-01: before it

    def test_refusals_in_use_lock_previous_and_a_tag_keep_their_reason(self):
        d = digest("x")
        self.assertEqual(verdict(sets=(set(), set(), {d})), "in use")
        self.assertEqual(verdict(sets=({d}, set(), set())), "lock")
        self.assertEqual(verdict(sets=(set(), {d}, set())), "previous")
        self.assertEqual(verdict(refs=["capsule-x:1"]), "recent")       # tagged: 14-day rule

    def test_an_image_with_a_kind_is_never_swept_as_legacy(self):
        self.assertEqual(verdict(labels={"capsule-layer": "base", KIND: "image"}), "recent")

    def test_only_the_exact_worker_test_tag_is_swept(self):
        bare = {"labels": {}}
        self.assertIsNone(verdict(refs=["capsule-corp-worker:test"], **bare))
        self.assertEqual(verdict(refs=["capsule-corp-worker:test"], sets=(set(), set(), {
            digest("x")}), **bare), "in use")
        self.assertEqual(verdict(refs=["capsule-corp-worker:test"], age={"days": 0, "hours": 3},
                                 **bare), "no age rule applies")
        for refs in ([WORKER], ["capsule-corp-worker:test2"], ["capsule-corp-worker:1"]):
            self.assertEqual(verdict(refs=refs, **bare), "no age rule applies", refs)
        self.assertEqual(verdict(refs=["ghcr.io/x/capsule-corp-worker:test"], **bare),
                         "tag outside capsule-*")
        self.assertEqual(verdict(refs=["capsule-corp-worker:test", "capsule-corp-worker:x"],
                                 **bare), "2 tags")

    def test_a_cached_rebuild_made_today_is_not_old_even_if_created_long_ago(self):
        recent = NOW - timedelta(hours=3)             # last tagged 3 hours ago, created 5 days ago
        self.assertEqual(verdict(refs=["capsule-corp-worker:test"], labels={}, built=recent),
                         "no age rule applies")
        self.assertEqual(verdict(built=recent), "recent")
        self.assertIsNone(verdict(refs=["capsule-corp-worker:test"], labels={},
                                  built=NOW - timedelta(hours=25)))

    def test_a_capsule_layer_image_of_kind_image_keeps_the_14_day_rule_not_the_24_hour_one(self):
        layer = {"capsule-layer": "base", "capsule": "1", KIND: "image"}
        self.assertEqual(verdict(labels=layer, age={"days": 13}), "recent")
        self.assertIsNone(verdict(labels=layer, age={"days": 15}))   # the 14-day rule applies
        only = {"capsule": "1", KIND: "image"}                       # not a capsule-layer image
        self.assertEqual(verdict(labels=only, age={"days": 15}), "no age rule applies")

    def test_the_constants_are_the_agreed_ones(self):
        self.assertEqual(image_gc.AGE_KINDS, ("test", "run"))
        self.assertEqual(image_gc.LEGACY_TAG, "capsule-corp-worker:test")
        self.assertEqual(image_gc.AGE_LIMIT, timedelta(hours=24))


def remove_with(listed, fresh, users=()):
    """image_gc._remove for the record of `listed` when a fresh `docker image inspect` answers
    `fresh` (None: gone), no lock, and the image ids in `users` are used by a container at that
    moment; returns (why not or None, docker argv seen). The sweep window is held open so the case
    does not depend on today's date."""
    seen = []

    def fake(argv, **kw):
        seen.append(argv)
        out = {"ps": "box0\n" if users else "", "inspect": "\n".join(users)}.get(argv[0], "")
        if argv[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0 if fresh else 1,
                                               json.dumps([fresh] if fresh else []), "")
        return subprocess.CompletedProcess(argv, 0, out, "")
    far = datetime(2100, 1, 1, tzinfo=timezone.utc)
    with mock.patch.object(image_gc.events, "docker_run", fake), \
            mock.patch.object(image_gc, "LEGACY_BEFORE", far):
        return image_gc._remove(image_gc._record(listed), set(), set()), seen


def rms(seen):
    return [a for a in seen if a[:2] == ["image", "rm"]]


class Recheck(unittest.TestCase):
    """_remove asks the listing's own question again, on a fresh inspect, right before removing."""
    def worker_test(self, ch="1"):
        return image(ch, None, 20, [image_gc.LEGACY_TAG])

    def test_a_tag_still_naming_the_same_old_image_is_untagged_by_that_exact_tag(self):
        why, seen = remove_with(self.worker_test(), self.worker_test())
        self.assertIsNone(why)
        self.assertEqual(rms(seen), [["image", "rm", "capsule-corp-worker:test"]])

    def test_a_tag_that_moved_to_another_image_or_vanished_is_kept(self):
        for fresh in (self.worker_test("2"), None):
            why, seen = remove_with(self.worker_test(), fresh)
            self.assertTrue(why.startswith("changed since listing"), why)
            self.assertEqual(rms(seen), [])

    def test_a_cached_rebuild_with_the_same_id_but_a_fresh_tag_time_is_kept(self):
        fresh = self.worker_test()
        stamp = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.1Z")
        fresh["Metadata"] = {"LastTagTime": stamp}
        why, seen = remove_with(self.worker_test(), fresh)
        self.assertEqual(why, "changed since listing: no age rule applies")
        self.assertEqual(rms(seen), [])

    def test_an_image_a_container_started_using_since_listing_is_kept(self):
        listed = image("3", "base", 20)
        why, seen = remove_with(listed, image("3", "base", 20), users=[digest("3")])
        self.assertEqual(why, "changed since listing: in use")
        self.assertEqual(rms(seen), [])
        why, seen = remove_with(listed, image("3", "base", 20), users=[digest("4")])
        self.assertIsNone(why)                       # a container of another image: it still goes

    def test_a_dangling_image_that_got_a_tag_since_listing_is_kept(self):
        listed = image("3", "base", 20)
        why, seen = remove_with(listed, image("3", "base", 20))
        self.assertIsNone(why)                                # control: still dangling, it goes
        self.assertEqual(rms(seen), [["image", "rm", digest("3")]])
        why, seen = remove_with(listed, image("3", "base", 20, ["myapp:dev"]))
        self.assertEqual(why, "changed since listing: tag outside capsule-*")
        self.assertEqual(rms(seen), [])


if __name__ == "__main__":
    unittest.main()
