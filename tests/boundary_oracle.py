"""Driver for tests/test_capsule_tick_sh.sh (C07c oracle 2): one `capsule.boundary.step` pass
with the REAL restart effect (a detached `bin/capsule-tick.sh restart <board> --quiet 900`, so
the fake launchctl named by CAPSULE_LAUNCHCTL is what runs) and the REAL status probe. Only the
gap probe is narrowed to the board's open runs: the machine-wide docker/pgrep checks would read
the live system. Usage: boundary_oracle.py REPO HOME FLAGDB SCENARIO [SHA]; prints what `step`
returned."""
import sys
import time
from pathlib import Path

repo, home, flagdb, scenario = sys.argv[1:5]
sys.path.insert(0, repo)
from capsule import boundary, db   # noqa: E402

if not Path(flagdb).exists():
    db.create_board(flagdb)
conn = db.connect(flagdb)
now = int(time.time())
sha = sys.argv[5] if len(sys.argv) > 5 else "a" * 40
boards = ["cc", "dd"] if scenario == "serial" else ["cc"]
db.guard_set(conn, boundary.KEY, {"sha": sha, "since": now, "boards": boards}, now=now)
fired = []


def record(h, board):
    fired.append(board)


effects = {"gap": lambda b, h: not boundary.open_runs(b, Path(h)), "loaded": lambda s: False}
if scenario == "serial":
    effects["fire"] = record
    effects["status"] = lambda h, b: {}
got = boundary.step(conn, {"board": "cc"}, Path(home), now, **effects)
print(got + ("".join(f":{b}" for b in fired)))
