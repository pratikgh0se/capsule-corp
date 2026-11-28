"""storage_gc volume age rules against a fake docker (one patched seam, never real docker).

Rule A: a capsule=1 volume with capsule.kind test or run that no container mounts goes after 24
hours; kind image never; a capsule=1 volume with no kind keeps today's 2 days. Rule B, the one-time
legacy sweep: an anonymous volume with no label except Docker's own anonymous marker, made before
LEGACY_BEFORE and over 24 hours old, unmounted, goes; after LEGACY_BEFORE it selects nothing.

Oracle 1: the fake's volume store after the run, read back by the test. Oracle 2: the argv list the
fake saw (a `volume rm` only for the ids the test wrote down) and the preview totals.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from storage_gc_fakes import Base, FakeDocker, box, storage_gc, vol  # noqa: E402
from test_image_gc_ages import WORKER, remove_with, rms, verdict  # noqa: E402
from web_stack_fakes import digest, image  # noqa: E402
import image_gc  # noqa: E402

KIND = "capsule.kind"
A, B, C, D, E, F, G, H = (c * 64 for c in "abcdef01")     # eight distinct 64-hex names


def named(**kw):
    return {"capsule": "1", **kw}


class RuleA(Base):
    def store(self):
        return FakeDocker(
            [box("a", "capsule-w-stopped", hours=0, volumes=[E])],
            [vol(A, named(**{KIND: "test"}), days=1.25),     # chosen: test, 30 hours
             vol(B, named(**{KIND: "run"}), days=1.25),      # chosen: run, 30 hours
             vol(C, named(**{KIND: "test"}), days=0.5),      # 12 hours: too young
             vol(D, named(**{KIND: "image"}), days=9),       # image kind: never by age
             vol(E, named(**{KIND: "test"}), days=9),        # a stopped container mounts it
             vol(F, named(), days=1.25),                     # no kind: still 2 days, too young
             vol(G, named(), days=3),                        # no kind, 3 days: chosen as today
             vol("capsule-corp_pg", named(**{KIND: "test"}), days=9),    # compose stack by name
             vol(H, {"owner": "me", KIND: "test"}, days=9)],  # kind but no capsule=1: not ours
            ls_ignores_dangling=True)           # so the script's own mounted check is what holds

    def test_apply_removes_old_unused_test_and_run_volumes_and_pins_every_refusal(self):
        fake = self.store()
        rc, _, _ = self.gc(fake, "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual({v["Name"] for v in fake.vols}, {C, D, E, F, "capsule-corp_pg", H})
        self.assertEqual({c[2] for c in fake.calls if c[:2] == ("volume", "rm")}, {A, B, G})

    def test_the_preview_names_the_same_three_and_removes_nothing(self):
        fake = self.store()
        rc, out, _ = self.gc(fake)
        self.assertEqual((rc, len(fake.vols)), (0, 9))
        self.assertFalse([c for c in fake.calls if c[:2] == ("volume", "rm")])
        self.assertIn("storage-gc: volume: 3 chosen", out)
        self.assertIn("(younger than 1 day)", out)
        self.assertIn("(kind image is never removed by age)", out)
        self.assertNotIn("[legacy]", out)

    def test_a_kind_test_volume_that_became_mounted_meanwhile_is_not_removed(self):
        fake = FakeDocker([], [vol(A, named(**{KIND: "test"}), days=3)])
        real = storage_gc._rm_volume

        def mounted(item, now):
            fake.boxes.append(box("b", "capsule-w-new", status="running", volumes=[A]))
            return real(item, now)
        with mock.patch.object(storage_gc, "_rm_volume", mounted):
            self.gc(fake, "--apply")
        self.assertEqual([v["Name"] for v in fake.vols], [A])


class Legacy(Base):
    def cut(self, **kw):
        """Turn the legacy sweep on with a cut-over `kw` away from now (positive = future)."""
        when = datetime.now(timezone.utc) + timedelta(**kw)
        self.enterContext(mock.patch.object(storage_gc, "LEGACY_BEFORE", when))

    def store(self):
        return FakeDocker(
            [box("a", "capsule-w-stopped", hours=0, volumes=[E])],
            [vol(A, None, days=3),                       # chosen: anonymous, no labels, old
             vol(B, {"com.docker.volume.anonymous": ""}, days=2),   # chosen: Docker's own label
             vol(C, None, days=0.5),                     # 12 hours: too young
             vol(D, {"owner": "me"}, days=9),            # carries a label: not ours to guess
             vol(E, None, days=9),                       # a stopped container mounts it
             vol("userdata", None, days=9),              # named, not anonymous
             vol("capsule-corp_pg", None, days=9),       # compose stack by name
             vol(F, {"com.docker.compose.project": "capsule-corp"}, days=9)],
            ls_ignores_dangling=True)

    def test_apply_removes_only_old_unlabelled_unmounted_anonymous_volumes(self):
        self.cut(days=1)
        fake = self.store()
        rc, _, _ = self.gc(fake, "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual({v["Name"] for v in fake.vols},
                         {C, D, E, "userdata", "capsule-corp_pg", F})
        self.assertEqual({c[2] for c in fake.calls if c[:2] == ("volume", "rm")}, {A, B})

    def test_preview_marks_them_legacy_and_changes_nothing(self):
        self.cut(days=1)
        fake = self.store()
        _, out, _ = self.gc(fake)
        self.assertEqual(len(fake.vols), 8)
        self.assertIn(f"would remove volume {A[:24]} [legacy]", out)
        self.assertIn("storage-gc: volume: 2 chosen (5000000 bytes), 6 kept", out)

    def test_it_selects_nothing_made_after_the_cut_over(self):
        self.cut(days=-4)            # cut-over four days ago: a 3-day-old volume is after it
        fake = FakeDocker([], [vol(A, None, days=3), vol(B, None, days=9)])
        self.gc(fake, "--apply")
        self.assertEqual([v["Name"] for v in fake.vols], [A])

    def test_the_real_cut_over_is_2026_11_02_and_both_scripts_share_it(self):
        self.assertEqual(image_gc.LEGACY_BEFORE, datetime(2026, 11, 2, tzinfo=timezone.utc))
        self.assertEqual(image_gc.AGE_KINDS, ("test", "run"))
        self.assertEqual(image_gc.AGE_LIMIT, timedelta(hours=24))


class ContainerKind(Base):
    """A container inherits its image's labels, so a debug shell run from a kind=image image
    carries capsule=1 capsule.kind=image; it must never go by the 1-hour rule."""
    LABELS = {"capsule": "1", KIND: "image"}

    def test_a_stopped_kind_image_container_is_kept_and_a_no_kind_one_still_goes(self):
        fake = FakeDocker([box("1", "debug-shell", labels=self.LABELS, hours=5),
                           box("2", "capsule-w-old", hours=5),                    # no kind: goes
                           box("3", "capsule-w-test", labels=named(**{KIND: "test"}), hours=5)])
        _, out, _ = self.gc(fake)
        self.assertIn("(kind image is never removed by age)", out)
        rc, _, _ = self.gc(fake, "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual({b["Id"][0] for b in fake.boxes}, {"1"})
        self.assertEqual({c[1][0] for c in fake.calls if c[0] == "rm"}, {"2", "3"})

    def test_the_check_right_before_the_removal_keeps_it_too(self):
        fake = FakeDocker([box("2", "capsule-w-old", hours=5)])      # chosen: no kind, 5 hours
        real = storage_gc._rm_container

        def relabelled(item, now):
            fake.boxes[0]["Config"]["Labels"] = dict(self.LABELS)     # image started it meanwhile
            return real(item, now)
        with mock.patch.object(storage_gc, "_rm_container", relabelled):
            rc, _, _ = self.gc(fake, "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual([r["result"] for r in self.log() if r["what"] == "container"],
                         ["skipped: kind image is never removed by age"])
        self.assertEqual(len(fake.boxes), 1)
        self.assertFalse([c for c in fake.calls if c[0] == "rm"])


class Replays:
    """The new Z1 intent rows (contracts/intent/storage_gc.jsonl once stamped), replayed by
    test_storage_gc_tmp.IntentRows (mixed in, so `self.gc` is its fake-docker runner). Volumes and
    containers run through storage_gc against FakeDocker; the image side asks image_gc's own
    verdict at a fixed clock, so no case depends on today's date."""
    def img(self, kind=None, **kw):
        """1 when image_gc would remove a capsule=1 image (kind optional) built to `kw`."""
        labels = {"capsule": "1", **({KIND: kind} if kind else {})}
        return int(verdict(labels=labels, **kw) is None)

    def replay_labelled(self):
        day, hours = {"days": 9}, {"days": 0, "hours": 30}
        fake = FakeDocker(
            [box("a", "w-stopped", hours=0, volumes=[E])],
            [vol(A, named(**{KIND: "test"}), days=1.25), vol(C, named(**{KIND: "run"}), days=0.5),
             vol(D, named(**{KIND: "image"}), days=9), vol(F, named(), days=1.25),
             vol(E, named(**{KIND: "test"}), days=9),
             vol("capsule-corp_pg", named(**{KIND: "test"}), days=9)], ls_ignores_dangling=True)
        rc, _, _ = self.gc(fake, "--apply")
        left = {v["Name"] for v in fake.vols}
        used, locked = (set(), set(), {digest("x")}), ({digest("x")}, set(), set())
        return {"exit": rc,
                "old_unused_test_or_run_removed": int(A not in left) + self.img("run", age=hours),
                "young_removed": int(C not in left)
                + self.img("test", age={"days": 0, "hours": 12}),
                "kind_image_removed": int(D not in left) + self.img("image", age={"days": 17}),
                "no_kind_removed": int(F not in left) + self.img(None, age=day),
                "in_use_removed": int(E not in left) + self.img("test", age=day, sets=used),
                "locked_removed": self.img("test", age=day, sets=locked),
                "compose_removed": int("capsule-corp_pg" not in left)}

    def replay_legacy(self):
        now = datetime.now(timezone.utc)
        bare, used = {"labels": {}}, (set(), set(), {digest("x")})
        fake = FakeDocker(
            [box("a", "w-stopped", hours=0, volumes=[E])],
            [vol(A, {"com.docker.volume.anonymous": ""}, days=3), vol(C, None, days=0.5),
             vol("userdata", None, days=9), vol(D, {"owner": "me"}, days=9), vol(E, None, days=9)],
            ls_ignores_dangling=True)
        late = FakeDocker([], [vol(B, None, days=3)])
        image_cut = mock.patch.object(image_gc, "LEGACY_BEFORE",
                                      datetime(2026, 11, 2, tzinfo=timezone.utc))
        with image_cut, mock.patch.object(storage_gc, "LEGACY_BEFORE", now + timedelta(days=1)):
            rc, _, _ = self.gc(fake, "--apply")
        with image_cut, mock.patch.object(storage_gc, "LEGACY_BEFORE", now - timedelta(days=4)):
            self.gc(late, "--apply")                # a 3-day-old volume is after this cut-over
            after = verdict(datetime(2026, 11, 5, tzinfo=timezone.utc), age={"days": 2})
            young = verdict(age={"days": 0, "hours": 12})
            tag = [verdict(refs=[image_gc.LEGACY_TAG], **bare), verdict(age={"days": 5})]
            moved = remove_with(image("1", None, 20, [image_gc.LEGACY_TAG]),
                                image("2", None, 20, [image_gc.LEGACY_TAG]))[1]
            worker = verdict(refs=[WORKER], **bare)
            kept_in_use = verdict(sets=used)
        left = {v["Name"] for v in fake.vols}
        return {"exit": rc,
                "old_legacy_removed": int(A not in left) + sum(v is None for v in tag),
                "after_cut_over_removed": int(B not in {v["Name"] for v in late.vols})
                + int(after is None),
                "young_removed": int(C not in left) + int(young is None),
                "named_or_labelled_or_in_use_removed": len({"userdata", D, E} - left)
                + int(kept_in_use is None),
                "moved_tag_removed": len(rms(moved)),
                "real_worker_image_removed": int(worker is None)}

    def replay_container(self):
        fake = FakeDocker([box("1", "debug-shell", labels=named(**{KIND: "image"}), hours=5),
                           box("2", "capsule-w-old", hours=5)])
        rc, _, _ = self.gc(fake, "--apply")
        left = {b["Id"][0] for b in fake.boxes}
        return {"exit": rc, "kind_image_removed": int("1" not in left),
                "no_kind_container_removed": int("2" not in left)}


class Unchanged(Base):
    """Rows the existing volume rules already cover, pinned again next to the new ones."""
    def test_a_capsule_volume_with_no_kind_goes_at_two_days_and_not_before(self):
        fake = FakeDocker([], [vol(A, named(), days=3), vol(B, named(), days=1.5)])
        self.gc(fake, "--apply")
        self.assertEqual([v["Name"] for v in fake.vols], [B])


if __name__ == "__main__":
    unittest.main()
