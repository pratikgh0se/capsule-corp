"""Environment precheck for tests/mutation_check.py (card t_589d9eff).

The gate runs the project's tests under `sys.executable`. When that interpreter cannot import the
project's own packages (a venv holding only the test runner, a missing dependency), every revert
and weaken row reads ERROR - noise that hides the real cause. The packages are the repo's
top-level directories holding an `__init__.py`, minus the test directories (`TEST_DIRS`);
importing each once is the check. Lives here, not in tests/mutation_check.py (size baseline)."""
import subprocess
import sys
import tempfile
from pathlib import Path

from size_baseline import TEST_DIRS


def _import_fails(pkg, cwd, timeout):
    """None when `import pkg` works under this interpreter in `cwd`, else the last error line."""
    try:
        p = subprocess.run([sys.executable, "-B", "-c", f"import {pkg}"], cwd=str(cwd),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"import hung past {timeout}s"
    lines = (p.stderr or "").strip().splitlines()
    return (lines[-1] if lines else "no output") if p.returncode else None


def refuse(repo, base, timeout=60):
    """0 when every project package imports under this interpreter. Else print one line and
    return 1 when the change broke it (a hang, a package new in the range, or it imports at
    `base`: the card's fault, red) or 75 (EX_TEMPFAIL) when `base` cannot import it either."""
    packages = sorted(d.name for d in Path(repo).iterdir() if d.name.isidentifier()
                      and (d / "__init__.py").is_file() and f"{d.name}/" not in TEST_DIRS)
    for pkg in packages:
        err = _import_fails(pkg, repo, timeout)
        if err is None:
            continue
        with tempfile.TemporaryDirectory(prefix="mc-env-base-") as tmp:
            git = subprocess.Popen(["git", "archive", base], cwd=str(repo), stdout=subprocess.PIPE)
            subprocess.run(["tar", "-x", "-C", tmp], stdin=git.stdout, check=False)
            git.stdout.close()
            git.wait()
            at_base = (_import_fails(pkg, tmp, timeout) if (Path(tmp) / pkg).is_dir()
                       else "absent at base")
        env = at_base is not None and not err.startswith("import hung") and git.returncode == 0 \
            and at_base != "absent at base"
        print(f"{'environment error' if env else 'change error'}: cannot import {pkg} ({err})")
        return 75 if env else 1
    return 0
