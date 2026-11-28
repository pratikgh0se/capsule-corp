"""Row 569: `contracts/schemas/rows/` holds the pinned schemas for operational rows (not agent-run
documents). They sit one directory below `contracts/schemas/` because every schema directly in it is
a run document that carries the run envelope (schema/run_id/task_id/claim_lock;
`test_schema_contracts` holds them to that) - a human file, an outbox row or a spool line has no run.
Same mechanism as the run schemas: stdlib validator only, one SHA256SUMS line per file, a change is
a new version file under the row 393 gate."""
import hashlib
import json
import unittest

from capsule import row_schemas
from capsule.jsonschema import load_schema


class RowSchemaPins(unittest.TestCase):
    def test_every_schema_loads_under_the_stdlib_validator_and_names_its_own_path(self):
        files = sorted(row_schemas.ROWS.glob("*.json"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(schema=path.name):
                schema = load_schema(path)       # raises on any keyword the validator does not check
                self.assertEqual(schema["$id"], f"contracts/schemas/rows/{path.name}")
                self.assertIs(schema["additionalProperties"], False)

    def test_pinned_hashes(self):
        """A schema change needs a new version file and a new SHA256SUMS line, under the row 393 gate."""
        sums = {}
        for line in (row_schemas.ROWS / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split(maxsplit=1)
            sums[name.lstrip("*")] = digest
        self.assertEqual(set(sums), {p.name for p in row_schemas.ROWS.glob("*.json")})
        for name, digest in sums.items():
            self.assertEqual(hashlib.sha256((row_schemas.ROWS / name).read_bytes()).hexdigest(), digest, name)

    def test_problems_validates_a_document_by_schema_name(self):
        name = "intervention.v1"
        good = json.loads('{"task_id": "t_0123abcd", "kind": "reset", "text": "go", "at": 1}')
        self.assertEqual(row_schemas.problems(name, good), [])
        self.assertTrue(row_schemas.problems(name, dict(good, at="1")))
        with self.assertRaises(ValueError):
            row_schemas.require(name, dict(good, task_id="x y"))
        row_schemas.require(name, good)


class PreflightAndAnchors(unittest.TestCase):
    """Fix round 1 (review F2, F3)."""

    def test_preflight_pins_the_rows_directory_too(self):
        import shutil
        import tempfile
        from pathlib import Path
        from unittest import mock

        from capsule import paths, preflight
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(paths.SCHEMAS, tmp / "schemas")
        with mock.patch.object(paths, "SCHEMAS", tmp / "schemas"):
            self.assertEqual(preflight._schemas_ok(), [])
            (tmp / "schemas" / "rows" / "outbox_row.v1.json").write_text("{}\n")
            (tmp / "schemas" / "rows" / "stray.v1.json").write_text("{}\n")
            bad = preflight._schemas_ok()
        self.assertIn("outbox_row.v1.json", bad)
        self.assertIn("stray.v1.json (not pinned)", bad)

    def test_no_row_schema_pattern_ends_in_a_bare_dollar(self):
        """Python's `$` matches before a trailing newline, and the validator uses re.search."""
        import json
        found = []

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("pattern"), str) and node["pattern"].endswith("$") \
                        and not node["pattern"].endswith("\\$"):
                    found.append(node["pattern"])
                for v in node.values():
                    walk(v)
        for path in row_schemas.ROWS.glob("*.json"):
            walk(json.loads(path.read_text()))
        self.assertEqual(found, [])

    def test_a_trailing_newline_is_rejected_where_the_schema_anchors_a_value(self):
        good = {"task_id": "t_0123abcd", "kind": "reset", "text": "go", "at": 1}
        self.assertEqual(row_schemas.problems("intervention.v1", good), [])
        self.assertTrue(row_schemas.problems("intervention.v1", dict(good, task_id="t_0123abcd\n")))
        self.assertTrue(row_schemas.problems("intervention.v1", dict(good, kind="reset\n")))
        item = {"item": "x", "source": "board:x", "state": "unblocked", "blocker": "", "owner": "", "age_s": 1}
        self.assertEqual(row_schemas.problems("unblocked_item.v1", item), [])
        self.assertTrue(row_schemas.problems("unblocked_item.v1", dict(item, source="run-layer\n")))
