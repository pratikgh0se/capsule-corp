"""GitHub Verify installs the same ruff as size_check.RUFF_VERSION (t_f9db09f9). Two ruff
versions can disagree on the same selection, so CI could go red on code the local gate passed.
The workflow carries a literal ruff==X.Y.Z that must equal it."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import size_check  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "verify.yml"


class VerifyRuffPin(unittest.TestCase):
    def install_lines(self):
        lines = WORKFLOW.read_text().splitlines()
        return [ln for ln in lines if "pip install" in ln and "ruff" in ln]

    def test_one_ruff_install_step(self):
        self.assertEqual(len(self.install_lines()), 1)

    def test_workflow_ruff_matches_size_check_pin(self):
        line = self.install_lines()[0]
        literal = re.search(r"ruff==(\d+\.\d+\.\d+)", line)
        self.assertIsNotNone(literal, line)
        self.assertEqual(literal.group(1), size_check.RUFF_VERSION)

    def test_python_stack_image_ruff_matches_size_check_pin(self):
        text = (ROOT / "docker" / "stacks" / "python" / "Dockerfile").read_text()
        self.assertEqual(re.findall(r'"ruff==([\d.]+)"', text), [size_check.RUFF_VERSION])

    def test_selection_unchanged(self):
        self.assertIn("ruff check --select E9,F63,F7,F82 .", WORKFLOW.read_text())


if __name__ == "__main__":
    unittest.main()
