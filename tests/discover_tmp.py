"""A private, swept $TMPDIR for one `unittest discover -p <module>` run (t_dd689940).

The suite dirs sweep their temp dirs through the sorted-first `test_0_tmpsweep.py` stub, but
`discover -p test_x.py` (what mutation_check's run_discover does, once per mutation) never
imports it, so every such run left its mkdtemp dirs in the real $TMPDIR. `run` gives the child
a fresh root and removes it afterwards (TemporaryDirectory resets permissions on read-only
trees). Self-contained on purpose: this file ships beside mutation_check.py into fixture and
scratch repos that have no capsule/ package, and importing capsule.tests.tmpclean would pin
this process's HOME and TMPDIR."""
import os
import tempfile


def run(fn, *args, **kwargs):
    """fn(*args, **kwargs) with os.environ['TMPDIR'] pointing at a fresh dir that is swept after."""
    old = os.environ.get("TMPDIR")
    with tempfile.TemporaryDirectory(prefix="capsule-suite-", ignore_cleanup_errors=True) as root:
        os.environ["TMPDIR"] = root
        try:
            return fn(*args, **kwargs)
        finally:
            if old is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = old
