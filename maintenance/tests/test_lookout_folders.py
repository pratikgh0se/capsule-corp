"""OBS-05 (The Lookout): folder provisioning (spec 6.1, 9.1 M2/M4). Oracle 1: the committed
provisioning YAML. Oracle 2: the dashboard JSON on disk, whose uids must be unique across every
provider path (Grafana's file provider recurses, so overlapping paths load a dashboard twice)."""
import collections
import json
import re
import unittest
from pathlib import Path

G = Path(__file__).resolve().parents[2] / "observability" / "grafana"
MOUNT = "/var/lib/grafana/dashboards"


def providers():
    """name -> {folder, folderUid, path}; the file is flat YAML, read with a regex."""
    text = (G / "provisioning" / "dashboards" / "dashboards.yaml").read_text()
    out = {}
    for block in text.split("  - name:")[1:]:
        name = block.split("\n")[0].strip()
        found = {k: re.search(rf"^\s+{k}: (.+)$", block, re.M)
                 for k in ("folder", "folderUid", "path")}
        out[name] = {k: m.group(1).strip() if m else None for k, m in found.items()}
    return out


class Folders(unittest.TestCase):
    def test_lookout_and_plumbing_providers(self):
        p = providers()
        self.assertEqual(p["the-lookout"], {"folder": "The Lookout", "folderUid": "the-lookout",
                                            "path": MOUNT + "/lookout"})
        self.assertEqual(p["plumbing"], {"folder": "Plumbing", "folderUid": "plumbing",
                                         "path": MOUNT + "/plumbing"})

    def test_old_capsule_corp_provider_is_gone(self):
        text = (G / "provisioning" / "dashboards" / "dashboards.yaml").read_text()
        self.assertNotIn("folder: Capsule Corp", text)
        self.assertNotIn(f"path: {MOUNT}\n", text)

    def test_nothing_at_the_dashboards_root(self):
        self.assertEqual(list((G / "dashboards").glob("*.json")), [])

    def test_every_dashboards_subfolder_has_a_provider(self):
        paths = {v["path"] for v in providers().values()}
        for d in (G / "dashboards").iterdir():
            if d.is_dir():
                self.assertIn(f"{MOUNT}/{d.name}", paths, f"no provider for dashboards/{d.name}")

    def test_every_provider_path_exists_and_none_overlaps(self):
        roots = {n: G / v["path"].replace("/var/lib/grafana/", "") for n, v in providers().items()}
        for n, r in roots.items():
            self.assertTrue(r.is_dir(), (n, r))
        for a in roots.values():
            for b in roots.values():
                if a != b:
                    self.assertNotIn(a, b.parents)

    def test_dashboard_uids_are_unique_across_providers(self):
        uids = collections.Counter()
        for r in [d for d in (G / "dashboards").iterdir() if d.is_dir()] + [G / "archive"]:
            for f in r.rglob("*.json"):
                uids[json.loads(f.read_text())["uid"]] += 1
        self.assertEqual([u for u, n in uids.items() if n > 1], [])

    def test_compose_mounts_the_dashboards_dir(self):
        compose = (G.parent / "docker-compose.yml").read_text()
        self.assertIn(f"./grafana/dashboards:{MOUNT}:ro", compose)


if __name__ == "__main__":
    unittest.main()
