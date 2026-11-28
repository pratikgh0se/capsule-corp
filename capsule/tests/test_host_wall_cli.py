"""The host wall against the REAL `claude` binary (D161, D162): a fake Messages API on localhost
scripts the model's tool calls, a dummy token and a temp config dir keep it off the network, so no
model is called. A stand-in `cat` never realpaths a path the way the CLI's Read and Write do (the
stat of the denied spool root made them fail with EPERM): only the real binary shows that.
Skipped when `claude` is not installed or off macOS (no sandbox-exec)."""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from capsule.models import claude
from capsule.runners import host_wall

CLAUDE = shutil.which("claude")
NEEDS = unittest.skipUnless(sys.platform == "darwin" and CLAUDE, "needs macOS and the claude CLI")
ROUTE, BUNDLE = {"model": "claude-sonnet-4-5", "turn_budget": 8}, {"system": "s", "prompt": "go"}
EPERM = "EPERM: operation not permitted"


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def message(block):
    """One assistant message as SSE: a tool_use block (stop `tool_use`) or a final text."""
    usage = {"input_tokens": 1, "output_tokens": 1}
    use = block["type"] == "tool_use"
    start = dict(block, input={}) if use else {"type": "text", "text": ""}
    delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if use
             else {"type": "text_delta", "text": block["text"]})
    return sse([
        {"type": "message_start", "message": {
            "id": "msg_x", "type": "message", "role": "assistant", "model": ROUTE["model"],
            "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}},
        {"type": "content_block_start", "index": 0, "content_block": start},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "usage": {"output_tokens": 5}, "delta": {
            "stop_reason": "tool_use" if use else "end_turn", "stop_sequence": None}},
        {"type": "message_stop"}])


class FakeApi:
    """Answers `/v1/messages` with the next scripted tool call, then a final text; keeps what the
    CLI sent back as each tool's result."""

    def __init__(self, calls):
        outer, self.results = self, []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                size = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(size) or b"{}")
                done = [b for m in body.get("messages", []) if isinstance(m.get("content"), list)
                        for b in m["content"] if b.get("type") == "tool_result"]
                outer.results = done
                step = len(done)
                block = {"type": "text", "text": '{"ok": true}'}
                if step < len(calls):
                    block = {"type": "tool_use", "id": f"toolu_{step}", **calls[step]}
                data = message(block) if body.get("stream") else b"{}"
                self.send_response(200)
                self.send_header("content-type", "text/event-stream" if body.get("stream")
                                 else "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def outcome(self, i):
        """(is_error, text) of the i-th tool result the CLI reported."""
        item = self.results[i]
        return bool(item.get("is_error")), str(item.get("content"))


def tool(name, path, **extra):
    return {"name": name, "input": dict({"file_path": str(path)}, **extra)}


@NEEDS
class RealCli(unittest.TestCase):
    """A board dir, a blind repo, a config dir and a workspace, all temp; `session` runs the
    adapter (so the wall is planned, proved and applied exactly as production does)."""

    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="hw cli ")
        self.addCleanup(td.cleanup)
        self.root = Path(os.path.realpath(td.name))
        self.board = self.root / "home" / "boards" / "b"
        self.root_spool = self.board / "spool"
        self.cfg, self.ws = self.root / "cfg", self.root / "ws"
        self.repo = self.root / "blind repo"
        for d in (self.cfg / "projects", self.cfg / "jobs", self.ws / "extra", self.repo,
                  self.board / "blind-work" / "t1"):
            d.mkdir(parents=True)
        (self.ws / "extra" / "x.txt").write_text("EXTRA")
        self.mode("gate")
        for run in ("r0", "r1", "ev", "tester"):
            self.spool(run)
        (self.root_spool / "r0" / "report.json").write_text("OTHER")
        (self.root_spool / "ev" / "report.json").write_text("EVIDENCE")
        (self.root_spool / "ev" / "blind-out").mkdir()
        (self.root_spool / "ev" / "blind-out" / "o.txt").write_text("BLIND-OUT")
        (self.root_spool / "tester" / "cli_result.txt").write_text("TESTER")
        self.write_job("tester", self.board / "blind-work" / "t1")
        env = {"ANTHROPIC_AUTH_TOKEN": "sk-dummy", "CLAUDE_CONFIG_DIR": str(self.cfg),
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for key in [k for k in os.environ if k.startswith("CLAUDE_CODE_")
                    and k != "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] + ["CLAUDECODE"]:
            os.environ.pop(key, None)

    def mode(self, mode, **blind):
        walled = {"repo": str(self.repo), "also_walled": [str(self.ws / "extra")]}
        cfg = {"board": "b", "redesign": {"blind_tests": mode},
               "blind_tests": dict(walled, **blind)}
        (self.board / "config.json").write_text(json.dumps(cfg))

    def spool(self, run):
        sp = self.root_spool / run
        (sp / "requests").mkdir(parents=True)
        (sp / "prior").mkdir()
        (sp / "prior" / "p.txt").write_text("PRIOR")
        self.write_job(run, self.ws)
        return sp

    def write_job(self, run, workspace):
        (self.root_spool / run / "job.json").write_text(json.dumps({"workspace": str(workspace)}))

    def session(self, role, calls, run="r1", *, inputs=None, resume=None):
        """(result, fake) of one real `claude -p` session for `role` in spool `run`."""
        sp = self.root_spool / run
        job = {"workspace": str(self.ws), "limits": {}, "inputs": inputs or {}}
        wall = host_wall.plan(role, sp, job)
        limits = {"schema": {"type": "object"}, "wall": wall, "tools": "Read,Write",
                  "allowed": "Read", "permission_mode": "acceptEdits", "timeout": 120,
                  "add_dirs": [str(sp), str(self.root_spool), str(self.cfg / "projects"),
                               str(self.cfg / "jobs")], **({"resume": resume} if resume else {})}
        fake = FakeApi(calls)
        self.addCleanup(fake.close)
        with mock.patch.dict(os.environ, ANTHROPIC_BASE_URL=fake.url):
            res = claude.ClaudeAdapter(ROUTE).run(role, BUNDLE, str(self.ws), limits)
        return res, fake

    def project(self):
        return self.cfg / "projects" / host_wall._project(self.cfg, str(self.ws)).name

    def test_every_walled_role_reads_prior_and_writes_requests_in_its_own_spool(self):
        for role in ("implementer", "reviewer", "guide", "korin"):
            sp = self.root_spool / "r1"
            for old in sp.glob("requests/*"):
                old.unlink()
            write = tool("Write", sp / "requests" / "w.json", content="{}")
            res, fake = self.session(role, [tool("Read", sp / "prior" / "p.txt"), write])
            self.assertEqual((res.error_class, res.exit), (None, 0), (role, res.raw[:300]))
            self.assertEqual([fake.outcome(0)[0], fake.outcome(1)[0]], [False, False], role)
            self.assertIn("PRIOR", fake.outcome(0)[1])
            self.assertEqual((sp / "requests" / "w.json").read_text(), "{}", role)

    def test_other_spools_and_extra_paths_are_refused_for_every_role(self):
        reads = [tool("Read", self.root_spool / "r0" / "report.json"),
                 tool("Read", self.ws / "extra" / "x.txt"),
                 tool("Read", self.root_spool / "tester" / "cli_result.txt")]
        for role in ("implementer", "reviewer", "guide"):
            _, fake = self.session(role, reads)
            self.assertEqual([fake.outcome(i)[0] for i in range(3)], [True] * 3, role)
            self.assertTrue(all(EPERM in fake.outcome(i)[1] for i in range(3)), role)

    def test_off_reads_everything_so_the_refusals_above_are_the_wall(self):
        self.mode("off")
        _, fake = self.session("implementer", [tool("Read", self.root_spool / "r0" / "report.json"),
                                               tool("Read", self.ws / "extra" / "x.txt")])
        self.assertEqual([fake.outcome(0)[0], fake.outcome(1)[0]], [False, False])
        self.assertIn("OTHER", fake.outcome(0)[1])

    def test_korin_reads_its_evidence_spools_but_not_blind_parts_or_a_testers(self):
        ev = self.root_spool / "ev"
        inputs = {"evidence": [str(ev), str(self.root_spool / "tester"), "event:5"]}
        res, fake = self.session("korin", [
            tool("Read", ev / "report.json"), tool("Read", ev / "blind-out" / "o.txt"),
            tool("Read", self.root_spool / "tester" / "cli_result.txt"),
            tool("Read", self.root_spool / "r0" / "report.json")], inputs=inputs)
        self.assertEqual(res.error_class, None, res.raw[:300])
        self.assertEqual([fake.outcome(i)[0] for i in range(4)], [False, True, True, True])
        self.assertIn("EVIDENCE", fake.outcome(0)[1])
        self.assertTrue(all(EPERM in fake.outcome(i)[1] for i in (1, 2, 3)))

    def test_a_resume_reads_its_own_transcript_under_the_wall(self):
        first, _ = self.session("implementer", [])
        self.assertTrue(first.session_id, first.raw[:300])
        self.assertEqual(len(list(self.project().glob("*.jsonl"))), 1)
        sp = self.root_spool / "r1"
        res, fake = self.session("implementer", [tool("Read", sp / "prior" / "p.txt")],
                                 resume=first.session_id)
        self.assertEqual((res.error_class, res.exit), (None, 0), res.raw[:300])
        self.assertEqual(res.session_id, first.session_id)

    def test_the_transcript_of_another_project_is_walled_and_the_own_one_is_not(self):
        other = self.cfg / "projects" / "-some-other-cwd"
        other.mkdir()
        (other / "s.jsonl").write_text("TRANSCRIPT")
        self.session("implementer", [])
        own = next(self.project().glob("*.jsonl"))
        _, fake = self.session("reviewer", [tool("Read", other / "s.jsonl"), tool("Read", own)])
        self.assertEqual([fake.outcome(0)[0], fake.outcome(1)[0]], [True, False])
        self.assertIn(EPERM, fake.outcome(0)[1])

    def test_korin_cannot_read_any_transcript_not_even_its_own_workspaces_folder(self):
        self.session("implementer", [])                 # leaves a transcript in that folder
        old = next(self.project().glob("*.jsonl"))
        other = self.cfg / "projects" / "-some-other-cwd"
        other.mkdir()
        (other / "s.jsonl").write_text("TRANSCRIPT")
        res, fake = self.session("korin", [tool("Read", old), tool("Read", other / "s.jsonl")])
        self.assertEqual((res.error_class, res.exit), (None, 0), res.raw[:300])    # it still runs
        self.assertEqual([fake.outcome(i)[0] for i in range(2)], [True, True])
        self.assertTrue(all(EPERM in fake.outcome(i)[1] for i in range(2)))

    def test_the_blind_tester_keeps_no_transcript_and_a_walled_role_does(self):
        res, _ = self.session("blind-tester", [], run="tester")
        self.assertEqual((res.error_class, res.exit), (None, 0), res.raw[:300])
        self.assertEqual(list(self.cfg.rglob("*.jsonl")), [])
        self.session("implementer", [])
        self.assertEqual(len(list(self.cfg.rglob("*.jsonl"))), 1)


if __name__ == "__main__":
    unittest.main()
