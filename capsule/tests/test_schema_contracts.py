import copy
import hashlib
import json
import unittest
from pathlib import Path

from capsule import paths
from capsule.jsonschema import ENVELOPE, load_all, validate

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"


class SchemaContracts(unittest.TestCase):
    """Spec §12.3a (row 416)."""

    @classmethod
    def setUpClass(cls):
        cls.schemas = load_all(paths.SCHEMAS)

    def test_every_schema_loads_and_names_itself(self):
        self.assertTrue({"report.v1", "request.v1", "decision.v1"} <= set(self.schemas))
        for name, schema in self.schemas.items():
            self.assertEqual(schema["properties"]["schema"], {"const": name}, name)
            self.assertTrue(set(ENVELOPE) <= set(schema["required"]), name)

    def test_valid_fixture_passes_and_each_missing_field_fails_by_name(self):
        for name, schema in self.schemas.items():
            doc = json.loads((FIXTURES / f"{name}.valid.json").read_text())
            self.assertEqual(validate(doc, schema), [], name)
            for field in schema["required"]:
                broken = copy.deepcopy(doc)
                del broken[field]
                errs = validate(broken, schema)
                self.assertTrue(any(repr(field) in e for e in errs), (name, field, errs))

    def test_blind_result_red_values_and_tail_cap(self):
        """Row 601: `red` is pass|fail|inconclusive|exempt-N; a tail over 2000 chars is refused."""
        schema = self.schemas["blind_result.v1"]
        doc = json.loads((FIXTURES / "blind_result.v1.valid.json").read_text())
        for red in ("pass", "fail", "inconclusive", "exempt-3"):
            self.assertEqual(validate(dict(doc, red=red), schema), [], red)
        for red in ("exempt", "exempt-x", "green", ""):
            self.assertTrue(validate(dict(doc, red=red), schema), red)
        long_tail = copy.deepcopy(doc)
        long_tail["tests"][0]["observed"]["stdout_tail"] = "x" * 2001
        self.assertTrue(validate(long_tail, schema))
        no_example = copy.deepcopy(doc)
        del no_example["tests"][0]["given"]
        self.assertTrue(validate(no_example, schema))

    @unittest.skipUnless(
        (paths.REPO / "docs/superpowers/specs/2026-09-24-capsule-corp-2-design.md").exists(),
        "private file not in public release")
    def test_report_schema_is_appendix_b_verbatim(self):
        spec = (paths.REPO / "docs/superpowers/specs/2026-09-24-capsule-corp-2-design.md").read_text()
        block = spec.split("The example, `report.v1`:")[1].split("```json")[1].split("```")[0]
        self.assertEqual(json.loads(block), json.loads((paths.SCHEMAS / "report.v1.json").read_text()))

    def test_pinned_hashes(self):
        """A schema change needs a new version file and a new SHA256SUMS line, under the row 393 gate."""
        sums = {}
        for line in (paths.SCHEMAS / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split(maxsplit=1)
            sums[name.lstrip("*")] = digest
        on_disk = {p.name for p in paths.SCHEMAS.glob("*.json")}
        self.assertEqual(set(sums), on_disk)
        for name, digest in sums.items():
            self.assertEqual(hashlib.sha256((paths.SCHEMAS / name).read_bytes()).hexdigest(), digest, name)
