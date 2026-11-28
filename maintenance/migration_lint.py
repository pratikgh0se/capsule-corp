"""Migration reversibility scanner (C3/M062)."""
import re
import sys
from pathlib import Path

DOWN_RE = re.compile(r"^--\s*down\b", re.M | re.I)
IRREV_RE = re.compile(r"^--\s*irreversible:\s*(t_[0-9a-f]{6,}|row \d+)\b", re.M | re.I)


def lint(dirpath):
    findings = []
    for path in sorted(Path(dirpath).glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if DOWN_RE.search(text) or IRREV_RE.search(text):
            continue
        findings.append(f"{path.name}: no `-- down` block and no `-- irreversible: <DECISION card id>` header")
    return findings


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    dirpath = argv[0] if argv else str(Path(__file__).resolve().parent.parent / "observability" / "postgres" / "migrations")
    findings = lint(dirpath)
    for f in findings:
        print(f)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
