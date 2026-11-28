"""t_62738029 (decision D39): the base-image build script is a judge path. A worker that could
edit `docker/build-base.sh` could choose the image it and its reviewers run in; only the CoS or
The owner may change it (D7)."""
import unittest

from capsule import workspace


class WorkerImageProtected(unittest.TestCase):
    def test_a_worker_write_to_the_base_build_script_is_refused(self):
        self.assertEqual(workspace.touches_protected(["docker/build-base.sh", "capsule/tick.py"]),
                         ["docker/build-base.sh"])

    def test_the_retired_image_pin_is_no_longer_a_judge_path(self):
        self.assertEqual(workspace.touches_protected(["docker/WORKER_IMAGE"]), [])

    def test_neighbouring_docker_files_stay_writable(self):
        neighbours = ["docker/README.md", "docker/IMAGES.lock.bak",
                      "docker/build-base.sh.bak", "docker/build-other.sh"]
        self.assertEqual(workspace.touches_protected(neighbours), [])


if __name__ == "__main__":
    unittest.main()
