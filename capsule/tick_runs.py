"""The tick's run mechanics: leases, process liveness and kill, clones, claim-and-launch, spawn.

A mixin of `capsule.tick.Tick`; it never imports `capsule.tick`.
"""
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from capsule import (blocks, claims, config, db, epic_clean, lease, outbox, paths, restart,
                     safe_write, workspace)
from capsule import incidents
from capsule.runners import docker
from capsule.db import event, txn

_LOGGED = set()     # (path, mtime) of malformed publish results already logged
_DONE = ("MERGED", "CLOSED")
_SLOW = {}      # card id -> (not before, delay, count): starts that timed out on a live engine
_PULL = r"github\.com/([^/\s]+/[^/\s]+)/pull/"


def _guard_launch(conn, cfg, row, ws, now):
    """Before a card launches on a reused clone: dirt that is not this card's own failed round is
    backed up and cleaned (never committed as its work). None, or ("worktree_dirty", reason)."""
    try:
        dirty = workspace._git("status", "--porcelain", *workspace.SCRATCH_EXCLUDE, ws=ws).stdout
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return ("worktree_dirty", f"could not read the lane clone status: {type(exc).__name__}")
    if not dirty.strip():
        return None
    who = epic_clean.owner(conn, row["branch_name"])
    if who == row["id"]:
        return None
    # the paths of `git status --porcelain` like `sync_to_origin` (a rename keeps `old -> new`)
    listed = " ".join(line[3:].strip() for line in dirty.splitlines() if line.strip())[:500]
    if epic_clean.backup_failed_open(conn, row["branch_name"]):   # the first card got the notice
        return ("worktree_dirty", f"lane clone holds work not this card's (owner {who}); the "
                                  f"backup-failed incident is open, nothing discarded: {listed}")
    path = epic_clean.set_aside(conn, cfg, row["branch_name"], ws, now)
    if path is None:
        return ("worktree_dirty", f"lane clone holds work not this card's (owner {who}); "
                                  f"set-aside failed, nothing discarded: {listed}")
    with txn(conn):
        event(conn, row["id"], "lane_leftover_set_aside",
              {"owner": who, "backup": str(path), "paths": listed}, now=now)
        outbox.notify(conn, "running", f"lane {row['branch_name']} leftover set aside",
                      fields=[("backup", str(path)), ("owner", str(who))],
                      file_body=f"capsule: lane clone held leftover work (owner {who}): {path}",
                      now=now)
    return None


def _slow_start(task_id, now):
    """One start timeout of a card while the engine answers: the card's next try waits 60 s,
    doubling to 900 s. Returns (delay, timeouts in a row)."""
    delay, n = _SLOW.get(task_id, (0, 0, 0))[1:]
    delay = min(delay * 2 or docker.MEMO_FIRST_S, docker.MEMO_CAP_S)
    _SLOW[task_id] = (now + delay, delay, n + 1)
    return delay, n + 1


class TickRuns:
    def _wake_engine(self, conn, task_id, now):
        """`_ensure_engine`, then one `engine_restarted` event on the first waiting card when this
        wake hard-restarted a hung engine (even when the engine stayed down)."""
        before = docker.restart_stamps(self.home)
        try:
            self.strong._ensure_engine()
        finally:
            if len(docker.restart_stamps(self.home)) > len(before):
                with txn(conn):
                    event(conn, task_id, "engine_restarted",
                          {"restarts_today": len(docker.restart_stamps(self.home))}, now=now)

    mac_hold = None           # why the Mac lane is down this pass (a reason), None when it is up
    _mac_lock_fh = None       # the held `mac-lane.lock` while `_mac_lane` launches a mac run

    def _up_tiers(self):
        """The tiers up beyond strong: light, and mac (its reason in `mac_hold` when it is not)."""
        up = {"light"} if self.runners("light", "implementer").available() else set()
        mac = self.runners("mac", "implementer")
        # one probe per pass, none cached across passes: a fixed cause is seen on the next tick
        if hasattr(mac, "unavailable_reason"):
            self.mac_hold = mac.unavailable_reason()
            mac_up = self.mac_hold is None
        else:
            self.mac_hold, mac_up = None, mac.available()
        up |= {"mac"} if mac_up else set()
        return up

    @contextlib.contextmanager
    def _mac_lane(self, tier):
        """One mac run at a time per Capsule home (D305). Yields False, claiming nothing and
        writing nothing, while another mac run holds `mac-lane.lock`. Else it takes the lock BEFORE
        the caller claims, and `_spawn` hands it to the `run-one` process (an inherited handle),
        which outlives the tick: the lock frees when that process ends or dies. A non-mac tier
        yields True with no lock. This tick's own handle closes on the way out, whatever happens."""
        from capsule.runners import mac
        fh = mac.acquire(self.home) if tier == "mac" else None
        if tier == "mac" and fh is None:
            yield False
            return
        self._mac_lock_fh = fh
        try:
            yield True
        finally:
            self._mac_lock_fh = None
            if fh is not None:
                fh.close()

    def _leases(self, conn, cfg, now):
        ttl = cfg["caps"]["claim_ttl"]
        beats = 0
        rows = conn.execute("SELECT id, claim_lock, worker_pid FROM tasks WHERE status='running'"
                            " AND worker_pid IS NOT NULL").fetchall()
        for r in rows:
            if (r["claim_lock"] or "").startswith(self.host + ":") and self.alive(r["worker_pid"]):
                beats += bool(lease.heartbeat(conn, r["id"], r["claim_lock"], ttl=ttl, now=now))
        # A run that finished while the tick was down is closed by step 4, never reclaimed first.
        root = self._spool_root(cfg)
        for r in conn.execute("SELECT t.id, t.claim_lock, r.run_id FROM tasks t JOIN task_runs r"
                              " ON r.id = t.current_run_id WHERE t.status='running'").fetchall():
            if r["run_id"] and (root / r["run_id"] / "run.json").exists():
                lease.heartbeat(conn, r["id"], r["claim_lock"], ttl=ttl, now=now)
        released = lease.release_stale(conn, alive=self.alive, kill=self._kill, ttl=ttl,
                                       host=self.host, now=now)
        return beats, released

    @staticmethod
    def _unreviewed_key(path):
        """(incident key, evidence, head, card ids) for a publish result flagged `ready_unreviewed`
        whose PR is still open, else None; a malformed file is logged once per mtime and skipped.
        `pr<N>`, not `#<N>`: the fingerprint masks bare numbers (a repo's PRs would merge)."""
        want_path = path.with_name(path.name.replace(".result.json", ".want.json"))
        cards = None
        try:
            doc = json.loads(path.read_text())
            if doc.get("ready_unreviewed") and (doc.get("pr") or {}).get("state") not in _DONE:
                cards = [c["task_id"] for c in json.loads(want_path.read_text())["cards"]]
                key = (f"publish_ready_unreviewed:{re.search(_PULL, doc['pr_url']).group(1)}"
                       f"#pr{int(doc['pr_number'])}")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            seen = (str(path), path.stat().st_mtime_ns if path.exists() else 0)
            if seen not in _LOGGED:
                _LOGGED.add(seen)
                print(f"capsule tick: publish result {path.name} skipped: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return None
        head = str(doc.get("head", ""))[:12]
        if cards is not None:
            return key, [f"result:{path}", f"pr:{doc['pr_url']}", f"head:{head}"], head, cards

    def _publish_incidents(self, conn, cfg, now):
        """Korin (CLAUDE.md): a PR ready on GitHub with no valid review at its head
        (`capsule publish --poll` writes `ready_unreviewed`) opens one `silent_failure` incident,
        on the board that owns the PR (a card of its want file is in this board's tasks). Not
        again while one is open, nor after one closed at the same head. A cleared flag opens
        nothing and closes nothing: incidents close only through the gate."""
        for path in sorted((self.home / "lookout" / "publish").glob("*.result.json")):
            found = self._unreviewed_key(path)
            if not found or not any(
                    conn.execute("SELECT 1 FROM tasks WHERE id=?", (t,)).fetchone()
                    for t in found[3]):
                continue
            if not conn.execute(
                    "SELECT 1 FROM incidents WHERE fingerprint=?"
                    " AND (status NOT IN ('closed', 'superseded') OR evidence LIKE ?)",
                    (incidents.fingerprint("silent_failure", found[0]),
                     f'%"head:{found[2]}"%')).fetchone():
                incidents.open_incident(conn, board=cfg.get("board"), kind="silent_failure",
                                        key=found[0], evidence=found[1], now=now)

    def _publish_incidents_guarded(self, conn, cfg, now, counts):
        """`_publish_incidents`; a reader error is counted and printed, never skipping the pass."""
        try:
            self._publish_incidents(conn, cfg, now)
        except Exception as exc:
            counts["tick_errors"] += 1
            print(f"capsule tick: publish incidents failed: {exc!r}", file=sys.stderr)

    def _alive(self, pid):
        """Own child: `Popen.poll()`; any other pid (a tick restarted under a live worker):
        `kill(pid, 0)`."""
        proc = self.children.get(pid)
        if proc is not None:
            if proc.poll() is None:
                return True
            self.children.pop(pid, None)
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _is_local(self, run):
        """The run's claim is this host's: another host's run is never ours to end."""
        return (run["claim_lock"] or "").startswith(self.host + ":")

    def _kill(self, pid, run_id=None):
        """SIGTERM; if the worker is still alive after `kill_grace` seconds, SIGKILL. A worker
        leads its own session (`_spawn`), so both go to its process group: its `claude` child
        and the exec MCP server die with it. A pid that does not lead its own group gets the
        signal alone, never its group.

        A pid spawned this session (`self.children`) is trusted. A DB-only pid (tick restart,
        or `release_stale` on a claim this process never made) may have been recycled by the OS,
        so it is trusted only when its command line names THIS run's id (`_is_our_worker`).
        A pid that fails that check is never signalled: not verifiably ours is not verifiably
        safe to touch."""
        if pid not in self.children and not self._is_our_worker(pid, run_id):
            return
        try:
            group = os.getpgid(pid) == pid
        except ProcessLookupError:
            return
        send = os.killpg if group else os.kill
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                send(pid, sig)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + self.kill_grace
            while time.monotonic() < deadline:
                if not self.alive(pid) and not (group and self._group_alive(pid)):
                    return
                time.sleep(0.05)

    @staticmethod
    def _is_our_worker(pid, run_id):
        """Best-effort check that `pid` is still `python3 -m capsule run-one <board> <spool>
        <run_id> <role>` (the argv `_spawn` uses) for THIS `run_id`. A match on just
        "capsule"/"run-one" would also match a recycled pid running another run or an
        unrelated process. No `run_id` never trusts the pid; neither does a pid we cannot
        inspect."""
        if not run_id:
            return False
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return "capsule" in out and "run-one" in out and run_id in out

    @staticmethod
    def _group_alive(pgid):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _apply_untracks(self, conn, ws, task_id):
        """Replays the card's recorded `untrack <path>` requests (sticking past the next commit is
        on the worker, per the verb-help clause in `bundles._request_lines`)."""
        for r in conn.execute(
                "SELECT DISTINCT json_extract(payload,'$.path') AS path FROM task_events"
                " WHERE task_id=? AND kind='untrack'", (task_id,)).fetchall():
            if path := r["path"]:
                workspace.untrack(ws, path)

    def _clone(self, conn, cfg, task_id, branch):
        """Every clone the tick makes for a card, `untrack` requests replayed first."""
        ws = workspace.clone(cfg["repo_url"], branch, self._work_root(cfg))
        self._apply_untracks(conn, ws, task_id)
        return ws

    def _claim_and_launch(self, conn, cfg, row, now):
        implementer = cfg["assignees"][row["assignee"]] == "implementer"   # only it runs a shell
        with self._mac_lane(row["tier"] if implementer else None) as free:
            return free and self._claim_one(conn, cfg, row, now)

    def _claim_one(self, conn, cfg, row, now):
        role = cfg["assignees"][row["assignee"]]
        if (docker.recently_down(now) and role == "implementer"
                and (row["tier"] or "strong") == "strong"):
            return False             # the engine failed to wake this pass: the card waits
        if _SLOW.get(row["id"], (0,))[0] > now:
            return False             # its last start timed out on a live engine: backing off
        route = restart.route(conn, cfg, row["id"], role)   # M155: the Opus restart route when due
        ws = self._clone(conn, cfg, row["id"], row["branch_name"])
        # Skipped only when the card owns commits (HEAD is past its pinned start_sha): a
        # fast-forward would hand the reviewer origin's own commits as this card's work. A card
        # with no commits of its own re-pins to the origin tip (inc_24a9756a). A refusal
        # (diverged, or dirty and behind) blocks the card directly; no run on a stale workspace.
        pinned = lease.pinned_start_sha(conn, row["id"])
        before = workspace.head(ws)
        sync = _guard_launch(conn, cfg, row, ws, now) or (
            None if pinned and before != pinned else workspace.sync_to_origin(ws))
        if sync is not None:
            with txn(conn):
                event(conn, row["id"], "worktree_mismatch", {"role": role, "error_class": sync[0],
                                                              "error": sync[1][:500]}, now=now)
                blocks.block(conn, row["id"], kind="needs_input", reason=sync[1][:2000], now=now,
                             offer="unblock|reset")
            return False
        if claims.precheck(conn, row, ws, now):    # D84: the card names paths the clone lacks
            return False
        start_sha = workspace.head(ws)
        repin = {"repin": True} if pinned and before == pinned and start_sha != pinned else {}
        c = lease.claim(conn, row["id"], ttl=cfg["caps"]["claim_ttl"], role=role, host=self.host,
                        now=now, runner=row["tier"] or "strong", provider=route["provider"],
                        account=route["account"], model=route["model"], tier=row["tier"],
                        metadata=config.run_meta(cfg, row["branch_name"], start_sha=start_sha,
                                                 **repin))
        if c is None:
            return False
        task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone(),
                    tier=row["tier"])     # the tier the lock and the claim used, not a re-read
        woke = docker.WOKE["count"]
        try:
            self._launch(conn, cfg, self._run(conn, c.run_pk), task, ws=ws, now=now)
            _SLOW.pop(row["id"], None)
        except docker.EngineDown as exc:    # not a crash: free the claim, one infra event, retry
            return self._engine_down(conn, cfg, row, c, exc, woke, now)
        with txn(conn):
            outbox.notify(conn, "running", f"{row['id']} dispatched: {role} on {route['model']}",
                          fields=[("branch", row["branch_name"])], thread_key=row["id"], file=False,
                          now=now)
        return True

    def _engine_down(self, conn, cfg, row, c, exc, woke, now):
        """A worker start found the engine down or slow: free the claim, one infra event, retry."""
        slow = docker.WOKE["count"] == woke and self._engine_probe()   # engine answers
        if docker.WOKE["count"] == woke and not slow:
            docker.note_failure()
        delay, fails = (_slow_start(row["id"], now) if slow
                        else (docker.WOKE["delay"], docker.WOKE["count"]))
        with txn(conn):
            db.transition(conn, row["id"], "ready", frm=("running",), claim=True)
            lease.end_run(conn, c.run_pk, status="released", outcome="engine_down", now=now)
            if slow:
                event(conn, row["id"], "start_slow", {"retry_s": delay}, run_pk=c.run_pk, now=now)
            else:
                event(conn, row["id"], "engine_down", {"retry_s": delay}, run_pk=c.run_pk,
                      now=now)
        self.host_tiers.discard("strong")   # no more strong cards this pass
        if fails >= 3:     # the same escalation as a contract job's
            incidents.open_incident(
                conn, board=cfg["board"], kind="review_infra", key="Docker engine down",
                evidence=[f"event:{row['id']}", str(exc)[:300]], task_id=row["id"], now=now)
        return False

    def _launch_unspawned(self, conn, cfg, task_id, now):
        """A checkpoint restart's run starts once the board and account are not paused and its
        tier is up; until then it has no spool and step 4 leaves it alone."""
        from capsule import dispatch
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        run = self._run(conn, task["current_run_id"]) if task["current_run_id"] else None
        if (run is None or run["ended_at"] is not None
                or (self._spool_root(cfg) / run["run_id"]).exists()
                or dispatch.tier_down(conn, task, self.host_tiers, None, now)   # running: no event
                or self._paused(conn, run["account"], now)):
            return
        with self._mac_lane(task["tier"] if run["profile"] == "implementer" else None) as free:
            if free:
                ws = self._clone(conn, cfg, task_id, task["branch_name"])
                self._launch(conn, cfg, run, task, ws=ws, now=now)

    def _spawn(self, board_db, spool_dir, run_id, role):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(paths.REPO), env.get("PYTHONPATH")) if p)
        lock = getattr(self, "_mac_lock_fh", None)     # a mac run's child keeps the lane lock
        with safe_write.open_append(spool_dir, Path(spool_dir) / "worker.log") as log:
            return subprocess.Popen(
                [sys.executable, "-m", "capsule", "run-one", str(board_db), str(spool_dir),
                 run_id, role],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                start_new_session=True, pass_fds=(lock.fileno(),) if lock else ())

    @staticmethod
    def _run(conn, pk):
        return conn.execute("SELECT * FROM task_runs WHERE id=?", (pk,)).fetchone()

    def _live_run_ids(self, conn):      # a running contract job's container is live too
        live = {r[0] for r in conn.execute("SELECT run_id FROM task_runs WHERE ended_at IS NULL"
                                           " AND run_id IS NOT NULL")}
        return live | self.contracts.run_ids(conn)

    @staticmethod
    def _start_sha(conn, task_id):
        """The branch head before the task's first implementer run: the base of every review
        diff."""
        sha = lease.pinned_start_sha(conn, task_id)
        if sha is None:         # a run row with no metadata (an `accept`-made one) pins nothing
            raise RuntimeError(f"no implementer run of {task_id} recorded a start_sha")
        return sha

    @staticmethod
    def _paused(conn, account, now):
        for key in ("pause:board", f"pause:account:{account}"):
            value, _ = db.guard_get(conn, key)
            if isinstance(value, dict) and value.get("until", 0) > now:
                return True
        return False

    def _spool_root(self, cfg):
        return paths.board_dir(cfg["board"], self.home) / "spool"

    def _work_root(self, cfg):
        return paths.board_dir(cfg["board"], self.home) / "work"
