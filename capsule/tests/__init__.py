"""Any import of the capsule test package pins the no-send marker (card rq_6dd2a60d): a unittest run
whose caller forgot AGENT_OPS_NO_SEND shipped board='test' rows to the live ops-pg. Forced, not
setdefault: an inherited empty or "0" value must not re-open the path."""
import os

os.environ["AGENT_OPS_NO_SEND"] = "1"

# `capsule human` and `capsule incidents retest` refuse an agent session that omits --author.
# This runs only when something imports the capsule.tests package (a `-m unittest capsule.tests.x`
# or a full discover), NOT for `discover -s capsule/tests -p test_x.py` (tests/mutation_check.py
# strips the markers itself). So a test that drives those verbs passes --author or sets/clears the
# markers itself; this is only a backstop for the ones that rely on the person's default.
for _marker in ("CLAUDECODE", "AI_AGENT"):
    os.environ.pop(_marker, None)

# t_dd689940: a single-module run (`-m unittest capsule.tests.test_x`) imports this package
# but never test_0_tmpsweep.py, and leaked its temp dirs; install the same private, swept
# root `discover` gets.
from . import tmpclean  # noqa: E402

tmpclean.install()

# The imports below reach maintenance/events.py (sandbox -> runners.docker -> docker_door) and
# `events.SPOOL` binds on that first import. Each maintenance test module does
# setdefault("EVENTS_SPOOL", its own temp file) and reads the variable at test time, so a spool
# bound here without it differs from the one they read (an empty spool where an event was
# expected). Pin one in the swept root first; a caller's value or an earlier module's still wins.
os.environ.setdefault("EVENTS_SPOOL",
                      os.path.join(tmpclean.install(), "capsule-corp-events-no-send.jsonl"))

# Row 632 b: the lander runs a candidate's code only in a container. Its tests keep real-shell
# fixtures through a host stand-in; test_sandbox.py reaches the real one as REAL_SESSION.
from capsule.runners import sandbox  # noqa: E402
from . import host_box  # noqa: E402

sandbox.REAL_SESSION = sandbox.session
sandbox.session = host_box.session
