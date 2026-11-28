"""maintenance/storage_gc.py: containers, volumes, image delegation and failure handling, against
a fake docker (one patched seam, never real docker). Temp dirs and intent rows: test_storage_gc_tmp.

Oracle 1: the fake's container/volume store after the run, read back by the test. Oracle 2: the
argv list the fake saw (no forced or prune form, no removal of a kept id) and the JSON log written
to a temp state dir, never the script's own printed verdicts. Fakes: storage_gc_fakes.py.
"""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from storage_gc_fakes import H64, REAL_IMAGES, Base, FakeDocker, ago, box, storage_gc, vol  # noqa


class Containers(Base):
    def test_preview_changes_nothing_and_says_why(self):
        fake = self.fleet()
        rc, out, _ = self.gc(fake)
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.boxes), 7)
        self.assertFalse([c for c in fake.calls if c[0] == "rm"])
        self.assertIn("would remove container " + "1" * 12, out)
        self.assertIn("(younger than 1 hour)", out)
        self.assertIn("(compose stack)", out)
        self.assertFalse(self.log())

    def test_apply_removes_only_old_stopped_labelled_and_logs_each(self):
        fake = self.fleet()
        rc, _, _ = self.gc(fake, "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual({b["Id"][0] for b in fake.boxes}, set("34567"))
        rm = [c for c in fake.calls if c[0] == "rm"]
        self.assertEqual({c[1][0] for c in rm}, set("12"))
        self.assertTrue(all(len(c) == 2 for c in rm), rm)          # `rm <id>`, no flags
        logged = [r for r in self.log() if r["what"] == "container"]
        self.assertEqual([r["result"] for r in logged], ["removed", "removed"])
        self.assertEqual(logged[0]["before"]["name"], "capsule-w-old")

    def test_a_running_container_is_never_removed(self):
        fake = self.fleet()
        self.gc(fake, "--apply")
        self.assertNotIn("4" * 64, [c[1] for c in fake.calls if c[0] == "rm"])
        self.assertIn("4" * 64, [b["Id"] for b in fake.boxes])

    def late(self, fake, change):
        """Run the real _rm_container after `change(fake, item)` altered the world."""
        real = storage_gc._rm_container

        def wrapped(item, now):
            change(fake, item)
            return real(item, now)
        with mock.patch.object(storage_gc, "_rm_container", wrapped):
            rc, _, _ = self.gc(fake, "--apply")
        return rc, {r["result"] for r in self.log() if r["what"] == "container"}

    def test_state_change_between_check_and_remove_skips_it(self):
        fake = self.fleet()
        rc, results = self.late(fake, lambda f, i: [b["State"].update(Status="running")
                                                    for b in f.boxes])
        self.assertEqual((rc, results, len(fake.boxes)), (0, {"skipped: state running"}, 7))

    def test_a_container_that_became_young_before_its_removal_is_skipped(self):
        fake = self.fleet()

        def young(f, item):
            for b in f.boxes:
                b["State"]["FinishedAt"] = b["Created"] = ago(minutes=1)
        _, results = self.late(fake, young)
        self.assertEqual((results, len(fake.boxes)), ({"skipped: younger than 1 hour"}, 7))

    def test_an_item_gone_meanwhile_is_skipped_not_failed(self):
        fake = self.fleet()
        rc, results = self.late(fake, lambda f, i: f.boxes.__setitem__(
            slice(None), [b for b in f.boxes if b["Id"] != i["id"]]))
        self.assertEqual((rc, results), (0, {"skipped: gone"}))

    def test_one_item_blowing_up_is_logged_and_the_next_one_still_goes(self):
        fake = self.fleet()

        def boom(*a):
            if a[0] == "rm" and a[1][0] == "1":
                raise OSError("disk on fire")
            return fake(*a)
        rc, _, err = self.gc(boom, "--apply")
        self.assertEqual(rc, 1)
        results = {r["id"][0]: r["result"] for r in self.log() if r["what"] == "container"}
        self.assertEqual(results["2"], "removed")
        self.assertTrue(results["1"].startswith("FAILED: OSError: disk on fire"))
        self.assertIn("an item failed", err)

    def test_an_inspect_error_that_is_not_no_such_object_fails_instead_of_gone(self):
        fake = self.fleet()

        def down(*a):
            if a[:2] == ("container", "inspect") and "--size" not in a:
                return subprocess.CompletedProcess(list(a), 1, "", "Cannot connect to the "
                                                   "Docker daemon at unix:///x\nIs it running?")
            return fake(*a)
        rc, _, err = self.gc(down, "--apply")
        self.assertEqual(rc, 1)
        results = {r["result"] for r in self.log() if r["what"] == "container"}
        self.assertEqual(results, {"FAILED: RuntimeError: docker container inspect failed: "
                                   "Cannot connect to the Docker daemon at unix:///x"})
        self.assertEqual(len(fake.boxes), 7)
        self.assertIn("an item failed", err)

    def test_second_run_changes_nothing(self):
        fake = self.fleet()
        self.gc(fake, "--apply")
        before, n = list(fake.boxes), len(self.log())
        self.gc(fake, "--apply")
        self.assertEqual(fake.boxes, before)
        self.assertEqual(len(self.log()), n)

    def test_no_forced_or_prune_form_is_ever_sent(self):
        fake = self.fleet()
        self.gc(fake, "--apply")
        for call in fake.calls:
            self.assertFalse([x for x in call if x in ("-f", "--force", "prune")], call)
            self.assertNotIn("prune", call)


class Volumes(Base):
    def store(self, **kw):
        return FakeDocker(
            [box("a", "capsule-w-stopped", hours=0, volumes=[H64[::-1]])],   # too young to go
            [vol(H64),                                       # anonymous, no label: not proven
             vol("d" * 64, labels={"capsule": "1"}),         # anonymous and labelled, old: chosen
             vol("c" * 64, labels={"capsule": "1"}, days=1), # labelled but young
             vol("userdata"),                                # named, unlabelled
             vol("capsule-corp_pgdata", labels={"capsule": "1"}),   # compose by name
             vol("composed", labels={"com.docker.compose.project": "capsule-corp"}),
             vol("capsule-cache", labels={"capsule": "1"}),  # labelled, old: chosen
             vol(H64[::-1], labels={"capsule": "1"})],       # mounted by a stopped box
            **kw)

    def test_apply_removes_only_old_labelled_unmounted_volumes(self):
        fake = self.store()
        rc, out, _ = self.gc(fake, "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual({v["Name"] for v in fake.vols},
                         {H64, "c" * 64, "userdata", "capsule-corp_pgdata", "composed",
                          H64[::-1]})
        self.assertIn("(younger than 2 days)", out)
        self.assertIn("(compose volume)", out)
        self.assertEqual([r["result"] for r in self.log() if r["what"] == "volume"],
                         ["removed", "removed"])

    def test_an_unlabelled_anonymous_volume_is_kept_and_summarised_once(self):
        fake = self.store()
        _, out, _ = self.gc(fake, "--apply")
        self.assertIn("kept: anonymous, not proven Capsule's: 1 volumes, 0.00 GB", out)
        self.assertNotIn(H64, out)                         # ids only with --verbose
        self.assertNotIn(("volume", "rm", H64), fake.calls)
        _, out, _ = self.gc(self.store(), "--verbose")
        self.assertIn(f"kept: volume {H64} (anonymous, not proven Capsule's)", out)

    def test_a_name_replaced_by_a_young_unlabelled_volume_is_kept(self):
        fake = self.store()
        real = storage_gc._rm_volume

        def swapped(item, now):
            fake.vols[:] = [vol(v["Name"], days=0) if v["Name"] == item["id"] else v
                            for v in fake.vols]
            return real(item, now)
        with mock.patch.object(storage_gc, "_rm_volume", swapped):
            rc, _, _ = self.gc(fake, "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual({r["result"] for r in self.log() if r["what"] == "volume"},
                         {"kept: changed since the check"})
        self.assertEqual(len(fake.vols), 8)
        self.assertFalse([c for c in fake.calls if c[:2] == ("volume", "rm")])

    def test_a_volume_inspect_error_that_is_not_no_such_object_fails_instead_of_gone(self):
        fake = self.store()

        def down(*a):
            if a[:2] == ("volume", "inspect") and len(a) == 3:
                return subprocess.CompletedProcess(list(a), 1, "", "context deadline exceeded")
            return fake(*a)
        rc, _, err = self.gc(down, "--apply")
        self.assertEqual(rc, 1)
        self.assertTrue(all(r["result"].startswith("FAILED: RuntimeError: docker volume inspect "
                                                   "failed: context deadline")
                            for r in self.log() if r["what"] == "volume"))
        self.assertEqual(len(fake.vols), 8)
        self.assertIn("an item failed", err)

    def test_a_volume_a_stopped_container_mounts_survives_even_if_listed_dangling(self):
        fake = self.store(ls_ignores_dangling=True)
        _, out, _ = self.gc(fake, "--apply")
        self.assertIn(H64[::-1], [v["Name"] for v in fake.vols])
        self.assertNotIn(("volume", "rm", H64[::-1]), fake.calls)
        self.assertIn("(a container mounts it)", out)

    def test_preview_reports_bytes_and_removes_nothing(self):
        fake = self.store()
        _, out, _ = self.gc(fake)
        self.assertEqual(len(fake.vols), 8)
        self.assertIn("storage-gc: volume: 2 chosen (5000000 bytes), 5 kept", out)

    def test_second_run_changes_nothing(self):
        fake = self.store()
        self.gc(fake, "--apply")
        left, n = list(fake.vols), len(self.log())
        self.gc(fake, "--apply")
        self.assertEqual(fake.vols, left)
        self.assertEqual(len(self.log()), n)


class Failures(Base):
    def test_docker_down_fails_with_reason_but_temp_cleanup_still_runs(self):
        d = self.mk("capsule-suite-x")
        down = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "no daemon"))
        rc, _, err = self.gc(down, "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("FAILED container: docker ps", err)
        self.assertFalse(d.exists())

    def test_a_removal_that_fails_exits_nonzero(self):
        fake = self.fleet()
        fake_rm = mock.Mock(side_effect=lambda *a: subprocess.CompletedProcess(list(a), 1, "", "")
                            if a[0] == "rm" else fake(*a))
        rc, _, err = self.gc(fake_rm, "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("an item failed", err)


class Images(Base):
    def test_image_gc_runs_dry_unless_apply_even_with_ambient_env(self):
        out = subprocess.CompletedProcess([], 0, "remove abc capsule-x:1 (700 bytes)\n", "")
        for apply, want in ((False, None), (True, "1")):
            with mock.patch.dict(os.environ, {"OPS_IMAGE_GC_APPLY": "1"}), \
                    mock.patch.object(storage_gc.subprocess, "run", return_value=out) as run, \
                    contextlib.redirect_stdout(io.StringIO()) as buf:
                REAL_IMAGES(apply, lambda r: None)
            self.assertEqual(run.call_args.kwargs["env"].get("OPS_IMAGE_GC_APPLY"), want)
            self.assertIn("images: 700 bytes by image_gc.py", buf.getvalue())

    def test_a_failed_image_removal_or_nonzero_exit_counts_and_is_not_reclaimed(self):
        cases = (("would remove a x (50 bytes)\nremove b y (700 bytes) FAILED\n", 0, "oops\n",
                  True),
                 ("remove c z (9 bytes)\n", 3, "", True),
                 ("remove c z (9 bytes)\n", 0, "", False))
        for text, rc, err, failed in cases:
            done = subprocess.CompletedProcess([], rc, text, err)
            with mock.patch.object(storage_gc.subprocess, "run", return_value=done), \
                    contextlib.redirect_stdout(io.StringIO()) as buf:
                got = REAL_IMAGES(True, lambda r: None)
            self.assertEqual(got, failed, text)
            self.assertNotIn("700 bytes by", buf.getvalue())
            self.assertEqual("images: stderr: oops" in buf.getvalue(), bool(err))

    def test_a_failed_image_run_makes_the_whole_run_exit_nonzero(self):
        self.images.return_value = True
        rc, _, err = self.gc(FakeDocker(), "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("images: image_gc.py failed", err)

    def test_main_calls_the_image_section_with_the_apply_choice(self):
        self.gc(FakeDocker())
        self.gc(FakeDocker(), "--apply")
        self.assertEqual([c.args[0] for c in self.images.call_args_list], [False, True])


if __name__ == "__main__":
    unittest.main()
