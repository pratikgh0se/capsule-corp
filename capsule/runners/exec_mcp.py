"""The exec MCP tool: a stdio JSON-RPC server exposing one tool, `exec`, that runs a shell
command inside a run's already-started container (spec §7.2).

Output over 64 KiB is spilled to `<spool>/spill/<n>.txt` with the first/last 8 KiB kept inline,
modelled on the old system's (no longer used here) tools/tool_result_storage.py:314-393 (persist
oversized tool output, preview + path) -- not a verbatim lift, no licence header required. (The
64 KiB threshold is measured in encoded bytes; the head/tail slices kept inline are measured in
characters, which can differ slightly for multi-byte text -- both are approximate previews,
never the file offsets. The inline marker names the host-side spool path, which is visible to
whatever reads the tool result; this is accepted for now.)

The exit status is part of the result,
`timeout_s` defaults to 600s (capped at 3600s) instead of hanging forever, a timeout also
kills the process inside the container (not just the local `docker exec` client), the strong
tier runs each command with its output in container files (a leftover server cannot hold the
docker exec pipe; it keeps running after a normal exit), a timeout kills the command's whole
session and returns the partial output plus a closing `[exec: timed out ...]` line, decoding
errors are replaced rather than raising, and one malformed request (bad JSON, unknown
method/tool, a bad `timeout_s`) returns a JSON-RPC error instead of ending the stdio server
for the rest of the run.
"""
import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_OUTPUT = 10 * 1024 * 1024   # a sandboxed command's output is read up to this, then cut
SPILL_THRESHOLD = 64 * 1024
SPILL_EDGE = 8 * 1024
DEFAULT_TIMEOUT_S = 600
MAX_TIMEOUT_S = 3600
# Slack given to the *local* `docker exec` client above the in-container `timeout`, so the
# container-side kill (which also reaps the process, unlike killing just the client) wins first.
_CLIENT_TIMEOUT_SLACK = 10
OUTAGE_MARKER = "exec_outage.json"   # at the spool root: a worker writes only under requests/
_PROBE_S = 5

EXEC_TOOL = {
    "name": "exec",
    "description": "Run a shell command inside the run's container workspace (/workspace). "
                   "The run's requests/ directory, when it has one, is at /requests.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "cmd": {"type": "string"},
            "timeout_s": {"type": "integer"},
        },
        "required": ["cmd"],
    },
}


def _parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--container")         # strong tier: `docker exec` into this container
    parser.add_argument("--sandbox-profile")   # light tier: seatbelt profile file ...
    parser.add_argument("--workspace")         # ... and the worktree to run in (LightRunner.limits)
    parser.add_argument("--developer-dir")     # Mac lane: the selected Xcode developer dir (D305)
    parser.add_argument("--sdk-root")          # Mac lane: SDKROOT, resolved host-side (D305)
    parser.add_argument("--spool", required=True)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    one_mode = bool(args.container) != bool(args.sandbox_profile)         # exactly one tier
    paired = bool(args.sandbox_profile) == bool(args.workspace)           # profile with worktree
    if not (one_mode and paired):
        parser.error("give --container, or --sandbox-profile with --workspace, not both")
    if args.developer_dir and not args.sandbox_profile:
        parser.error("--developer-dir goes with --sandbox-profile")
    return args


def _sandbox_env(cfg, scratch):
    """The only environment a sandboxed command gets: none of the host's (tokens, keys). TMPDIR
    and HOME are the scratch dir, inside the worktree so the profile lets it write. Light tier:
    DEVELOPER_DIR points the /usr/bin git and python3 shims at the Command Line Tools, so they
    read only /Library (already allowed) and never xcrun's cache or the Xcode app. Mac lane
    (`--developer-dir`): DEVELOPER_DIR is the selected Xcode, and the clang module cache, the
    SwiftPM caches and the XDG dirs all live in the scratch dir, never in the host's home."""
    from capsule.runners import host
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": scratch, "TMPDIR": scratch,
           "LANG": "en_US.UTF-8"}   # Mac lane: system dirs only (no Homebrew python: unreadable)
    developer_dir = getattr(cfg, "developer_dir", None)
    if developer_dir:
        # The toolchain dirs come first: the /usr/bin shims (xcrun) need the SDK path and a
        # warm cache a fresh scratch lacks, while the real tools run from these dirs without them.
        env["PATH"] = (f"{developer_dir}/Toolchains/XcodeDefault.xctoolchain/usr/bin:"
                       f"{developer_dir}/usr/bin:" + env["PATH"])
        if getattr(cfg, "sdk_root", None):
            env["SDKROOT"] = cfg.sdk_root
        env.update(DEVELOPER_DIR=developer_dir, TMPDIR=scratch.rstrip("/") + "/",
                   DARWIN_USER_CACHE_DIR=os.path.join(scratch, "xdg-cache"),
                   CLANG_MODULE_CACHE_PATH=os.path.join(scratch, "clang-module-cache"),
                   SWIFTPM_MODULECACHE_OVERRIDE=os.path.join(scratch, "swiftpm-module-cache"),
                   XDG_CACHE_HOME=os.path.join(scratch, "xdg-cache"),
                   XDG_CONFIG_HOME=os.path.join(scratch, "xdg-config"),
                   XDG_DATA_HOME=os.path.join(scratch, "xdg-data"))
    elif os.path.isdir(host.CLT):
        env["DEVELOPER_DIR"] = host.CLT
    return env


def _run_sandboxed(cfg, cmd, timeout_s):
    """Light tier (rows 373, 395): `cmd` under `sandbox-exec` in the worktree. There is no
    `timeout` binary on macOS: the command gets its own session and a timeout kills the whole
    process group, so no child outlives it."""
    from capsule.runners import host
    argv = [host.SANDBOX_EXEC, "-f", cfg.sandbox_profile, "/bin/sh", "-c", cmd]
    if cfg.dry_run:
        return {"exit_code": 0, "text": json.dumps(argv)}
    scratch = os.path.join(cfg.workspace, host.SCRATCH)
    env = _sandbox_env(cfg, scratch)
    # Output goes to a file, not a pipe: a child that left the group (setsid) and holds the pipe
    # would keep `communicate` waiting forever. The group is always killed at the end; a child that
    # escaped it is the tick's `LightRunner.reap` (by cwd and open files under the worktree).
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(argv, cwd=cfg.workspace, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            return {"exit_code": 127, "text": f"[exec failed: {e}]"}
        try:
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            code = -1
        finally:
            # none left in the group: fine (macOS: a group of only zombies answers EPERM)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        out.seek(0)
        raw = out.read(MAX_OUTPUT + 1)
    text = raw[:MAX_OUTPUT].decode("utf-8", "replace")
    if len(raw) > MAX_OUTPUT:
        text += f"\n[output cut at {MAX_OUTPUT} bytes]"
    return {"exit_code": code, "text": text if code != -1 else f"{text}[killed after {timeout_s}s]"}


def _next_spill_name(spill_dir):
    high = 0
    if spill_dir.is_dir():
        for p in spill_dir.iterdir():
            if p.stem.isdigit():
                high = max(high, int(p.stem))
    return f"{high + 1}.txt"


def _maybe_spill(text, spool):
    if len(text.encode("utf-8", "surrogatepass")) <= SPILL_THRESHOLD:
        return text
    spill_dir = Path(spool) / "spill"
    spill_dir.mkdir(parents=True, exist_ok=True)
    path = spill_dir / _next_spill_name(spill_dir)
    path.write_text(text)
    head = text[:SPILL_EDGE]
    tail = text[-SPILL_EDGE:]
    return f"{head}\n...[{len(text)} chars total, spilled to {path}]...\n{tail}"


def _validate_timeout(timeout_s):
    if timeout_s is None:
        return DEFAULT_TIMEOUT_S
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s <= 0:
        raise ValueError(f"timeout_s must be a positive integer, got {timeout_s!r}")
    return min(timeout_s, MAX_TIMEOUT_S)


_PIPE_TIP = "Tip: a background process may be holding the output pipe; redirect output to a file."
# The strong tier's in-container wrapper (args: timeout_s, command). The command runs in its own
# session (setsid, when the image has it) with stdout and stderr in files, so a process it leaves
# behind (a server) can never hold docker exec's pipe. On a normal exit the files are printed back
# (stdout, then stderr), the command's exit code kept, and the leftovers keep running as before.
# Only on the timeout's TERM is the whole session killed (its processes are listed first); then the
# partial output and one closing note come back, exit 137 (as `timeout -s KILL` gave).
_WRAP = (
    'd=$(mktemp -d /tmp/exec.XXXXXX); S=; command -v setsid >/dev/null && S=setsid; '
    '$S sh -lc "$1" >$d/o 2>$d/e </dev/null & p=$!; '
    "trap 'l=$(ps -s $p -o pid=,args= 2>/dev/null | cut -c1-60 | head -5 | tr \"\\n\" \";\"); "
    'kill -KILL -$p 2>/dev/null; cat $d/o; cat $d/e >&2; rm -f $d/o $d/e; rmdir $d; '
    'echo "[exec: timed out after ${0}s and was killed; still running at timeout: $l '
    + _PIPE_TIP + ']" >&2; exit 137\' TERM; '
    "wait $p; rc=$?; cat $d/o; cat $d/e >&2; rm -f $d/o $d/e; rmdir $d; exit $rc"
)


def _run_exec(cfg, cmd, timeout_s):
    """Returns {"exit_code": int, "text": str}. Never raises for a well-formed call:
    a client-side timeout or missing docker binary comes back as a non-zero exit_code."""
    timeout_s = _validate_timeout(timeout_s)
    if cfg.sandbox_profile:
        return _run_sandboxed(cfg, cmd, timeout_s)
    # The command runs after TREE_LISTING in the same `sh -lc` (inc_35435ea0): a file the host
    # replaced is stale in the container until its directory is listed. The module is docker.py's
    # because that one is protected; the exit code and output stay the command's.
    from capsule.runners import docker
    # `timeout` runs *inside* the container, so the command is really killed, not just detached
    # from the local `docker exec` client; the wrapper gets the timeout as $0, the command as $1.
    argv = [cfg.docker, "exec", "-w", "/workspace", cfg.container, "timeout", "-s", "TERM",
            "--preserve-status", "-k", "5", str(timeout_s), "sh", "-c", _WRAP, str(timeout_s),
            docker.TREE_LISTING + cmd]
    if cfg.dry_run:
        return {"exit_code": 0, "text": json.dumps(argv)}
    # Output goes to files, not pipes: what a command wrote before a timeout is kept, and a
    # process that outlives the command cannot hold a pipe open.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        except FileNotFoundError as e:
            return {"exit_code": 127, "text": f"[exec failed: {e}]"}
        client_note = ""
        try:
            code = proc.wait(timeout=timeout_s + _CLIENT_TIMEOUT_SLACK)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            code = -1
            client_note = (f"[client-side timeout after {timeout_s + _CLIENT_TIMEOUT_SLACK}s; "
                           f"in-container timeout was {timeout_s}s. {_PIPE_TIP}]")
        text = "".join(f.seek(0) or f.read().decode("utf-8", "replace") for f in (out, err))
    done = {"exit_code": code, "text": text + client_note}
    if done["exit_code"] != 0 and _daemon_lost(cfg):
        (Path(cfg.spool) / OUTAGE_MARKER).write_text(json.dumps({"exit_code": done["exit_code"]}))
        done["text"] = (f"[the command did not run: the docker daemon is down]\n{done['text']}")
    return done


def _daemon_lost(cfg):
    """The rule of runners/sandbox.py: the container is not running and the daemon does not
    answer. A probe that fails or times out (5 s) counts as not answering."""
    def ok(*args):
        try:
            return subprocess.run([cfg.docker, *args], capture_output=True, text=True,
                                  timeout=_PROBE_S)
        except (OSError, subprocess.SubprocessError):
            return None
    box = ok("inspect", "-f", "{{.State.Running}}", cfg.container)
    if box is not None and box.returncode == 0 and (box.stdout or "").strip() == "true":
        return False
    up = ok("version", "--format", "{{.Server.Version}}")
    return up is None or up.returncode != 0


def _handle(msg, cfg):
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        params = msg.get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "capsule-exec", "version": "1"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [EXEC_TOOL]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != "exec":
            return {"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": -32602, "message": f"unknown tool {params.get('name')!r}"}}
        arguments = params.get("arguments") or {}
        try:
            result = _run_exec(cfg, arguments.get("cmd", ""), arguments.get("timeout_s"))
        except ValueError as e:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": str(e)}}
        text = result["text"] if cfg.dry_run else _maybe_spill(result["text"], cfg.spool)
        exit_code = result["exit_code"]
        return {"jsonrpc": "2.0", "id": msg_id,
                 "result": {"content": [{"type": "text", "text": text}],
                            "isError": exit_code != 0, "exit_code": exit_code}}
    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method {method!r}"}}


def _write(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main(argv=None):
    cfg = _parse_args(sys.argv[1:] if argv is None else argv)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {e}"}})
            continue
        try:
            response = _handle(msg, cfg)
        except Exception as e:  # a malformed request must never end the worker's only exec channel
            response = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                        "error": {"code": -32603, "message": f"internal error: {e}"}}
        if response is not None:
            _write(response)
    return 0


if __name__ == "__main__":
    sys.exit(main())
