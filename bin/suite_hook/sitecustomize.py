"""capsule-suite puts this on PYTHONPATH: SIGUSR1 dumps all thread stacks (no abort, no report)."""
import faulthandler
import importlib.util
import os
import signal
import sys

faulthandler.register(signal.SIGUSR1, all_threads=True)
_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
_self = sys.modules.pop("sitecustomize", None)
if importlib.util.find_spec("sitecustomize"):  # chain to the interpreter's own, if any
    import sitecustomize  # noqa: F401
else:
    sys.modules["sitecustomize"] = _self
