"""Factory envelope (docs/observability-conventions.md section 10, OBS-09): one JSON line."""
import hashlib
import re
import socket
import sys
from datetime import datetime, timezone

from capsule import event_kinds, paths

sys.path.insert(0, str(paths.REPO / "maintenance"))
import scrub  # noqa: E402  (G7: free text leaving the board is scrubbed first)

SEVERITIES = ("INFO", "WARN", "ERROR")
REQUIRED = ("ts", "severity_text", "event.name", "body", "service.name", "service.version",
            "service.instance.id", "deployment.environment.name", "host.name")
CORRELATION = ("capsule.board", "capsule.task_id", "capsule.run_id", "trace_id", "span_id")
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
_HEX = {"trace_id": re.compile(r"^[0-9a-f]{32}$"), "span_id": re.compile(r"^[0-9a-f]{16}$")}


# events.kind values that are not card events, and the envelope event.name each ships as.
# Card events ship as their payload's `event` (already an event_kinds kind). Explicit and
# closed: an events.kind missing here fails problems() instead of shipping unnamed.
KIND_NAMES = {k: k for k in (
    "job", "health", "audit", "message", "usage", "vault", "ai_usage", "backup_done",
    "backup_partial", "ledger_mismatch", "infra", "tick_health", "release", "dep_cache",
    "capsule_unblocked", "host_gap", "tick_stale_restart")}


def trace_id(run_id):
    """Fixed 32-hex W3C trace id for a run: the same run_id always gives the same id."""
    return hashlib.sha256(f"capsule-run:{run_id}".encode()).hexdigest()[:32]


def problems(line):
    """[] or messages: what a parsed envelope line breaks (types, vocabularies, formats)."""
    out = [f"missing {k}" for k in REQUIRED if k not in line]
    if line.get("severity_text", "INFO") not in SEVERITIES:
        out.append(f"severity_text {line['severity_text']!r} not in {SEVERITIES}")
    if "ts" in line and not _TS.match(str(line["ts"])):
        out.append(f"ts {line['ts']!r} is not RFC3339 UTC")
    if line.get("event.name", "x") not in event_kinds.ALLOWED and (
            line["event.name"] not in KIND_NAMES.values()):
        out.append(f"event.name {line['event.name']!r} not in event_kinds.ALLOWED")
    if "\n" in str(line.get("body", "")):
        out.append("body is not one line")
    for k, rx in _HEX.items():
        if k in line and not rx.match(str(line[k])):
            out.append(f"{k} {line[k]!r} is not W3C hex")
    run = line.get("capsule.run_id")
    if run and "trace_id" in line and line["trace_id"] != trace_id(run):
        out.append("trace_id is not the hash of capsule.run_id")
    out += [f"{k} is not a string" for k, v in line.items() if not isinstance(v, str)]
    return out


def envelope(event_name, body, resource, *, severity="INFO", board=None, task_id=None, run_id=None,
             step=None, now=None, **extra):
    """The envelope dict. `resource` carries the four v1 resource attributes (section 1);
    host.name defaults to this host. Correlation keys appear only when their input is given."""
    now = now or datetime.now(timezone.utc)
    line = {"ts": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", "severity_text": severity,
            "event.name": event_name, "body": " ".join(scrub.scrub(str(body)).split()),
            "host.name": socket.gethostname(), **resource}
    ids = (("capsule.board", board), ("capsule.task_id", task_id), ("capsule.run_id", run_id))
    for key, val in ids:
        if val is not None:
            line[key] = str(val)
    if run_id is not None:
        line["trace_id"] = trace_id(run_id)
        if step is not None:
            span = hashlib.sha256(f"capsule-span:{run_id}:{step}".encode())
            line["span_id"] = span.hexdigest()[:16]
    line.update({k: str(v) for k, v in extra.items()})
    return line

