#!/usr/bin/env python3
"""obs_inventory.py - deterministic inventory of Capsule Corp's observability stack:
what emits, what's consumed, what's covered, what has rotted."""
import argparse
import contextlib
import datetime
import difflib
import importlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from capsule import host  # noqa: E402
import vault_conf  # noqa: E402
import vault_git  # noqa: E402 - same door docs_sync.py/topics.py/decisions.py use

NOTE_REL = "Architecture/Observability Inventory.md"


def _refuse_default_vault_under_test(vault):
    """Guard against the 2026-09-07 incident, same shape as
    board_view._refuse_default_vault_under_test: under the suite's own marker
    (`AGENT_OPS_NO_SEND`), a write that reaches the real vault is always a bug, never
    intended."""
    if not os.environ.get("AGENT_OPS_NO_SEND"):
        return
    if not str(vault or "").strip():
        raise RuntimeError("refusing to touch the default vault under test: "
                            "pass --vault explicitly")
    real = vault_conf.host_json_vault()
    if real is not None and Path(vault).expanduser().resolve() == real.resolve():
        raise RuntimeError(f"refusing to write the real vault ({real.resolve()}) under test")


BASE_COMPONENT_ROWS = (
    "postgres", "loki", "fluent-bit", "grafana", "infisical", "kanban boards",
    "launchd jobs", "bridge", "gateway", "heal",
)


def component_rows():
    """The matrix rows: the built-in components plus host.json `observability.components`."""
    return list(BASE_COMPONENT_ROWS) + list(host.get_or_default("observability.components"))
MATRIX_COLUMNS = ["health", "structured logs", "metrics", "traces", "dashboard",
                   "alerts", "SLO", "runbook"]

CELL_RULES = (
    "health ✅ if a `/healthz`-style route or a heal component (`heal_table.py`'s "
    "component list, or a `docker-compose.yml` service with a `healthcheck:`) exists "
    "for it; structured logs ✅ if a fluent-bit `[INPUT]` tail (Path or Tag) covers "
    "it; metrics ✅ if any `capsule.<component>.*` sample name exists for it; "
    "traces ❌ always (not built this build); dashboard ✅ if any panel's "
    "rawSql/LogQL references one of its signals (kind/name/log-stream tag); alerts "
    "✅ if an alert rule's rawSql references one of its signals; SLO ❌ always "
    "(D-7 not built); runbook ✅ if `docs/RUNBOOK.md` has a `## ` or `### ` heading "
    "whose text "
    "contains the component's name. Everything else is ❌ - n/a is never used in "
    "this build."
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(name):
    """Match tokens for a component row: lowercase word pieces of length >= 4 (so
    'fluent-bit' -> ['fluent'], 'kanban boards' -> ['kanban', 'boards']), each also
    in its other number - 'boards' also matches 'board', 'jobs' also matches 'job'."""
    toks = [t for t in _TOKEN_RE.findall(name.lower()) if len(t) >= 4]
    toks = toks or _TOKEN_RE.findall(name.lower())
    out = []
    for t in toks:
        for form in (t, t[:-1] if t.endswith("s") else t + "s"):
            if form and form not in out:
                out.append(form)
    return out


# ------------------------------------------------------------- fresh imports --
def _fresh_import(modname, search_dir):
    """Import `modname` from `search_dir`, never a stale copy cached by an earlier call
    against a different repo_root (tests import against several fixture trees, and a
    real run, in one process)."""
    sys.modules.pop(modname, None)
    sd = str(search_dir)
    if sd in sys.path:
        sys.path.remove(sd)
    sys.path.insert(0, sd)
    return importlib.import_module(modname)


@contextlib.contextmanager
def _isolated_imports():
    """Snapshots and restores sys.path plus the `events`/`obs_probe`/ `build_dashboards`
    entries of sys.modules around a `_fresh_import` spree, so introspecting a --repo
    (or a test fixture) never leaves the process's real `events` module (this file's
    own `vault_git.events`, and every other module that does a plain `import events`)
    pointed at a throwaway fixture copy for the rest of the run."""
    saved_path = list(sys.path)
    watched = ("events", "obs_probe", "build_dashboards")
    saved_mods = {k: sys.modules.get(k) for k in watched}
    try:
        yield
    finally:
        sys.path[:] = saved_path
        for k, v in saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# ------------------------------------------------------------------ emitters --
_EVENTS_LOG_RE = re.compile(r"""events\.log\(\s*["']([\w-]+)["']""")
_EV_LOG_RE = re.compile(r"""\bev_log\s+([\w-]+)\s""")
_DOLLAR_EV_RE = re.compile(r"""\$EV\s+log\s+([\w-]+)\s""")


def _grep_event_emitters(repo_root):
    """kind -> ["file:line", ...] for every `events.log(`, `ev_log `, and `$EV log` call
    site directly under maintenance/ (not maintenance/tests/, since glob is non-
    recursive - a test fixture call site is not a real emitter)."""
    maint = repo_root / "maintenance"
    sites = {}
    if not maint.is_dir():
        return sites
    for path in sorted(maint.glob("*.py")) + sorted(maint.glob("*.sh")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rx in (_EVENTS_LOG_RE, _EV_LOG_RE, _DOLLAR_EV_RE):
                m = rx.search(line)
                if m:
                    sites.setdefault(m.group(1), []).append(f"{path.name}:{lineno}")
    return sites


_NAME_LINE_RE = re.compile(r'^\s*"(capsule\.[\w.]+)"\s*:')


def _sample_emitters(repo_root):
    """sample name -> \"maintenance/obs_probe.py:<line>\" for every NAMES entry,
    found by regex over the source (not the imported dict) so the line number
    is real."""
    path = repo_root / "maintenance" / "obs_probe.py"
    out = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _NAME_LINE_RE.match(line)
        if m:
            out[m.group(1)] = f"maintenance/obs_probe.py:{lineno}"
    return out


def _parse_fluent_bit_inputs(text):
    """[{\"line\": int, \"path\": str, \"tag\": str}, ...] - one per [INPUT]
    block. A tiny indentation-agnostic reader over Fluent Bit's classic
    key-value config format (no PyYAML needed, it is not YAML)."""
    lines = text.splitlines()
    inputs = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == "[INPUT]":
            block = {"line": i + 1, "path": "", "tag": ""}
            j = i + 1
            while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith("["):
                s = lines[j].strip()
                if s.startswith("Path "):
                    block["path"] = s.split(None, 1)[1].strip()
                elif s.startswith("Tag "):
                    block["tag"] = s.split(None, 1)[1].strip()
                j += 1
            inputs.append(block)
            i = j
        else:
            i += 1
    return inputs


def _log_streams(repo_root):
    path = repo_root / "observability" / "fluent-bit" / "fluent-bit.conf"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _parse_fluent_bit_inputs(text)


# ------------------------------------------------------- alerting rules.yaml --
def _parse_alert_rules(text):
    """[{"uid", "title", "sql", "line"}, ...] - a ~25-line indentation-aware reader over
    just the shape rules.yaml uses (a list of rule dicts, each with a `title` and a
    `rawSql` that is either a quoted one-liner or a `|` block scalar)."""
    lines = text.splitlines()
    rules = []
    cur = None
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        m = re.match(r"^-\s+uid:\s*(\S+)", s)
        if m:
            if cur:
                rules.append(cur)
            cur = {"uid": m.group(1), "title": m.group(1), "sql": "", "line": i + 1}
            i += 1
            continue
        if cur is not None:
            mt = re.match(r'^title:\s*(.+)$', s)
            if mt:
                cur["title"] = mt.group(1).strip().strip('"')
                i += 1
                continue
            mq = re.match(r'^rawSql:\s*"(.*)"\s*$', s)
            if mq:
                cur["sql"] += mq.group(1).replace('\\"', '"')
                i += 1
                continue
            if s == "rawSql: |":
                indent = len(raw) - len(raw.lstrip(" "))
                i += 1
                block = []
                while i < len(lines) and (lines[i].strip() == ""
                                           or (len(lines[i]) - len(lines[i].lstrip(" "))) > indent):
                    block.append(lines[i])
                    i += 1
                cur["sql"] += "\n".join(block)
                continue
        i += 1
    if cur:
        rules.append(cur)
    return rules


def _alert_rules(repo_root):
    path = repo_root / "observability" / "grafana" / "provisioning" / "alerting" / "rules.yaml"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _parse_alert_rules(text)


# ------------------------------------------------------------- dashboard panels --
def _dashboard_panels(repo_root):
    """[{\"dashboard\": filename, \"panel\": title, \"text\": rawSql+expr}, ...],
    every panel of every DASHBOARDS entry, rendered in memory."""
    grafana_dir = repo_root / "observability" / "grafana"
    out = []
    if not (grafana_dir / "build_dashboards.py").exists():
        return out
    mod = _fresh_import("build_dashboards", grafana_dir)
    for fname, builder in getattr(mod, "DASHBOARDS", {}).items():
        dash = builder()
        for panel in dash.get("panels", []):
            texts = []
            for t in panel.get("targets", []):
                texts.append(t.get("rawSql") or t.get("expr") or "")
            out.append({"dashboard": fname, "panel": panel.get("title", ""),
                        "text": "\n".join(texts)})
    return out


# ------------------------------------------------------------------- compose --
def _parse_compose_services(text):
    """service name -> {"image", "container_name", "ports": [...], "volumes": [...],
    "healthcheck": bool}."""
    services = {}
    cur = None
    list_key = None
    list_indent = None
    in_services = False
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        s = raw.strip()
        if s == "services:":
            in_services = True
            continue
        if not in_services:
            continue
        if indent == 2 and s.endswith(":") and not s.startswith("-"):
            cur = s[:-1]
            services[cur] = {"image": "", "container_name": "", "ports": [], "volumes": [],
                              "healthcheck": False}
            list_key = None
            continue
        if cur is None:
            continue
        if list_key and indent > list_indent and s.startswith("- "):
            services[cur][list_key].append(s[2:].strip())
            continue
        list_key = None
        if indent < 4:
            continue
        if s.startswith("image:"):
            services[cur]["image"] = s.split(":", 1)[1].strip()
        elif s.startswith("container_name:"):
            services[cur]["container_name"] = s.split(":", 1)[1].strip()
        elif s.startswith("healthcheck:"):
            services[cur]["healthcheck"] = True
        elif s.startswith("ports:"):
            rest = s.split(":", 1)[1].strip()
            if rest.startswith("["):
                services[cur]["ports"].extend(re.findall(r'"([^"]+)"', rest))
            else:
                list_key, list_indent = "ports", indent
        elif s.startswith("volumes:"):
            list_key, list_indent = "volumes", indent
    return services


def _image_version(image):
    m = re.search(r"#\s*v?([\d.]+)", image)
    if m:
        return m.group(1)
    if "@sha256:" in image:
        digest = image.split("@sha256:", 1)[1].split()[0]
        return "sha256:" + digest[:12]
    base = image.split()[0] if image.split() else image
    if ":" in base:
        return base.rsplit(":", 1)[1]
    return "latest"


def _data_dir(volumes):
    for v in volumes:
        if "${DATA_DIR}" in v:
            left = v.split(":", 1)[0]
            return left.replace("${DATA_DIR}/", "").replace("${DATA_DIR}", ".")
    return "n/a"


def _port(ports):
    if not ports:
        return "n/a"
    return ports[0].rsplit(":", 1)[-1]


def _loki_retention(repo_root):
    path = repo_root / "observability" / "loki" / "loki.yaml"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "n/a"
    m = re.search(r"retention_period:\s*(\S+)", text)
    return m.group(1) if m else "n/a"


def _events_retention(repo_root):
    path = repo_root / "maintenance" / "cleanup.sh"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "n/a"
    m = re.search(r"delete events older than (\d+) days", text)
    return f"{m.group(1)}d (cleanup.sh)" if m else "n/a"


def _samples_retention(repo_root):
    """Same shape as _events_retention: cleanup.sh is the only retention rule
    `samples` has, and the note used to claim it had none at all."""
    path = repo_root / "maintenance" / "cleanup.sh"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "n/a"
    m = re.search(r"delete samples older than (\d+) days", text)
    return f"{m.group(1)}d (cleanup.sh)" if m else "n/a"


def _components(repo_root, now):
    path = repo_root / "observability" / "docker-compose.yml"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    services = _parse_compose_services(text)
    loki_ret = _loki_retention(repo_root)
    events_ret = _events_retention(repo_root)
    rows = []
    for name, svc in services.items():
        retention = "n/a"
        if name == "loki":
            retention = loki_ret
        elif name == "postgres":
            retention = f"events {events_ret}"
        rows.append({
            "container": svc["container_name"] or name,
            # `image: postgres:16-alpine  # v16.4` - the pin comment is what
            # _image_version reads, but it does not belong in the image cell.
            "image": svc["image"].split("#", 1)[0].strip(),
            "version": _image_version(svc["image"]),
            "port": _port(svc["ports"]),
            "data_dir": _data_dir(svc["volumes"]),
            "retention": retention,
            "healthcheck": svc["healthcheck"],
        })
    return rows


# --------------------------------------------------------------- heal components --
def _heal_components(repo_root):
    path = repo_root / "maintenance" / "heal_table.py"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    m = re.search(r"ORDER\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(1))


# ------------------------------------------------------------------- runbook --
def _runbook_headings(repo_root):
    path = repo_root / "docs" / "RUNBOOK.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # `###` subsections count: the runbook's Capsule material lives under `### The
    # `samples` table and the self-probe` and friends, not under a top-level `## `
    # heading per component.
    return [line.lstrip("#").strip() for line in text.splitlines()
            if line.startswith("## ") or line.startswith("### ")]


def _contract_version(repo_root):
    path = repo_root / "docs" / "observability-conventions.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 1
    m = re.search(r"^contract_version:\s*(\d+)\s*$", text, re.MULTILINE)
    return int(m.group(1)) if m else 1


# ------------------------------------------------------------------ matching --
def _consumes_event(text, kind, payload_keys):
    if re.search(rf"kind\s*=\s*'{re.escape(kind)}'", text):
        return True
    for key in payload_keys:
        if re.search(rf"payload\s*->>\s*'{re.escape(key)}'", text):
            return True
    return False


def _consumes_sample(text, name):
    return bool(re.search(rf"name\s*=\s*'{re.escape(name)}'", text))


def _consumes_stream(text, tag):
    prefix = tag.rstrip("*").rstrip(".")
    if not prefix:
        return False
    return prefix.lower() in text.lower()


_KIND_LIT_RE = re.compile(r"kind\s*=\s*'([\w-]+)'")
_NAME_LIT_RE = re.compile(r"name\s*=\s*'([\w.]+)'")


# ------------------------------------------------------------------- signals --
def _build_signals(repo_root):
    """Returns (signals, dash_panels, rules) where `signals` is a list of
    dicts: key, type (event|sample|log-stream), emitter, store, retention,
    consumers (list of \"dashboard:<file>: <panel>\" / \"alert: <title>\")."""
    with _isolated_imports():
        events_mod = _fresh_import("events", repo_root / "maintenance")
        kinds = dict(getattr(events_mod, "KINDS", {}))
        obs_probe_mod = _fresh_import("obs_probe", repo_root / "maintenance")
        names = dict(getattr(obs_probe_mod, "NAMES", {}))
        dash_panels = _dashboard_panels(repo_root)

    event_emitters = _grep_event_emitters(repo_root)
    events_ret = _events_retention(repo_root)
    samples_ret = _samples_retention(repo_root)
    sample_emitters = _sample_emitters(repo_root)
    streams = _log_streams(repo_root)
    loki_ret = _loki_retention(repo_root)
    rules = _alert_rules(repo_root)

    signals = []
    for kind, payload_keys in sorted(kinds.items()):
        sites = event_emitters.get(kind, [])
        consumers = []
        for p in dash_panels:
            if _consumes_event(p["text"], kind, payload_keys):
                consumers.append(f"dashboard:{p['dashboard']}: {p['panel']}")
        for r in rules:
            if _consumes_event(r["sql"], kind, payload_keys):
                consumers.append(f"alert: {r['title']}")
        signals.append({
            "key": kind, "type": "event",
            "emitter": "; ".join(sites) if sites else "(no emitter found)",
            "store": "Postgres (events)", "retention": events_ret,
            "consumers": consumers,
        })
    for name in sorted(names):
        consumers = []
        for p in dash_panels:
            if _consumes_sample(p["text"], name):
                consumers.append(f"dashboard:{p['dashboard']}: {p['panel']}")
        for r in rules:
            if _consumes_sample(r["sql"], name):
                consumers.append(f"alert: {r['title']}")
        signals.append({
            "key": name, "type": "sample",
            "emitter": sample_emitters.get(name, "maintenance/obs_probe.py (line not found)"),
            "store": "Postgres (samples)", "retention": samples_ret,
            "consumers": consumers,
        })
    for st in streams:
        tag = st["tag"] or "(no tag)"
        consumers = []
        for p in dash_panels:
            if _consumes_stream(p["text"], tag):
                consumers.append(f"dashboard:{p['dashboard']}: {p['panel']}")
        for r in rules:
            if _consumes_stream(r["sql"], tag):
                consumers.append(f"alert: {r['title']}")
        signals.append({
            "key": tag, "type": "log stream",
            "emitter": f"observability/fluent-bit/fluent-bit.conf:{st['line']}",
            "store": "Loki", "retention": loki_ret,
            "consumers": consumers,
        })
    return signals, dash_panels, rules, kinds, names


def _orphans(signals, dash_panels, rules, kinds, names):
    """(consumer_orphans, emitter_orphans) - both lists of strings."""
    known_kinds = set(kinds)
    known_names = set(names)
    consumer_orphans = []
    seen = set()
    for src_label, texts in (("dashboard", [(p["dashboard"], p["panel"], p["text"]) for p in dash_panels]),
                              ("alert", [(None, r["title"], r["sql"]) for r in rules])):
        for fname, ref, text in texts:
            for k in _KIND_LIT_RE.findall(text):
                if k not in known_kinds and ("kind", k) not in seen:
                    seen.add(("kind", k))
                    where = f"{src_label} {fname + ': ' if fname else ''}{ref}"
                    consumer_orphans.append(f"'{k}' (kind) referenced by {where}, but nothing emits it")
            for n in _NAME_LIT_RE.findall(text):
                if n not in known_names and ("name", n) not in seen:
                    seen.add(("name", n))
                    where = f"{src_label} {fname + ': ' if fname else ''}{ref}"
                    consumer_orphans.append(f"'{n}' (sample) referenced by {where}, but nothing emits it")
    emitter_orphans = [
        f"'{s['key']}' ({s['type']}) is emitted but no dashboard panel or alert rule reads it"
        for s in signals if s["type"] in ("event", "sample") and not s["consumers"]
    ]
    return sorted(consumer_orphans), sorted(emitter_orphans)


# --------------------------------------------------------------- coverage matrix --
def _row_matches(tokens, text):
    tl = text.lower()
    return any(t in tl for t in tokens)


def _health_cell(toks, heal_lower, compose_services):
    if any(t in heal_lower for t in toks):
        return "✅"
    for svc in compose_services:
        if svc["healthcheck"] and any(t in svc["container"].lower() for t in toks):
            return "✅"
    return "❌"


def _logs_cell(toks, streams):
    for st in streams:
        hay = f"{st['path']} {st['tag']}".lower()
        if any(t in hay for t in toks):
            return "✅"
    return "❌"


def _coverage_matrix(repo_root, signals, heal_components, compose_services, streams, headings):
    heal_lower = {h.lower() for h in heal_components}
    rows = {}
    for row in component_rows():
        toks = _tokens(row)
        matched = [s for s in signals if _row_matches(toks, s["key"])]
        metrics = "✅" if any(s["type"] == "sample" for s in matched) else "❌"
        dash_ok = any(any(c.startswith("dashboard:") for c in s["consumers"]) for s in matched)
        alert_ok = any(any(c.startswith("alert:") for c in s["consumers"]) for s in matched)
        health = _health_cell(toks, heal_lower, compose_services)
        logs = _logs_cell(toks, streams)
        runbook = "✅" if any(row.lower() in h.lower() for h in headings) else "❌"
        rows[row] = {
            "health": health,
            "structured logs": logs,
            "metrics": metrics,
            "traces": "❌",
            "dashboard": "✅" if dash_ok else "❌",
            "alerts": "✅" if alert_ok else "❌",
            "SLO": "❌",
            "runbook": runbook,
        }
    return rows


# --------------------------------------------------------------------- diff --
def _section(text, heading):
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _diff_section(prev_text, new_signals_md, new_orphans_md):
    if prev_text is None:
        return "first run - no previous note to diff against."
    parts = []
    for heading, new_body in (("Signals", new_signals_md), ("Orphans", new_orphans_md)):
        old_body = _section(prev_text, heading)
        diff = list(difflib.unified_diff(
            old_body.splitlines(), new_body.splitlines(),
            fromfile=f"previous {heading}", tofile=f"current {heading}", lineterm=""))
        if diff:
            capped = diff[:60]
            more = "" if len(diff) <= 60 else f"\n... ({len(diff) - 60} more diff lines)"
            parts.append(f"### {heading}\n```diff\n" + "\n".join(capped) + more + "\n```")
    return "\n\n".join(parts) if parts else "no change in Signals or Orphans since the previous note."


# ------------------------------------------------------------------- render --
def _md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def render(repo_root, vault_root, now):
    repo_root = Path(repo_root)
    vault_root = Path(vault_root)

    signals, dash_panels, rules, kinds, names = _build_signals(repo_root)
    consumer_orphans, emitter_orphans = _orphans(signals, dash_panels, rules, kinds, names)
    components = _components(repo_root, now)
    heal_components = _heal_components(repo_root)
    streams = _log_streams(repo_root)
    headings = _runbook_headings(repo_root)
    matrix = _coverage_matrix(repo_root, signals, heal_components, components, streams, headings)
    contract_version = _contract_version(repo_root)

    as_of = now.strftime("%Y-%m-%d")

    # -- Components --
    comp_rows = [[c["container"], c["image"], c["version"], c["port"], c["data_dir"], c["retention"]]
                 for c in sorted(components, key=lambda c: c["container"])]
    components_md = (f"## Components (as of {as_of})\n\n"
                      + _md_table(["container", "image", "version", "port", "data dir", "retention"],
                                  comp_rows or [["(none found)", "", "", "", "", ""]]))

    # -- Signals --
    sig_rows = []
    for s in sorted(signals, key=lambda s: (s["type"], s["key"])):
        consuming = "; ".join(s["consumers"]) if s["consumers"] else "— (orphan, see below)"
        sig_rows.append([s["key"], s["type"], s["emitter"], s["store"], s["retention"], consuming])
    signals_md_body = _md_table(["kind/name", "type", "emitter (file:line)", "store", "retention",
                                  "consuming panel(s)"], sig_rows or [["(none found)", "", "", "", "", ""]])
    signals_md = "## Signals\n\n" + signals_md_body

    # -- Coverage matrix --
    matrix_rows = [[row] + [matrix[row][c] for c in MATRIX_COLUMNS] for row in component_rows()]
    matrix_md = "## Coverage matrix\n\n" + _md_table(["component"] + MATRIX_COLUMNS, matrix_rows)

    # -- Orphans --
    orphans_lines = []
    orphans_lines.append("**Panels/alerts referencing a signal nobody emits:**")
    orphans_lines.append("\n".join(f"- {o}" for o in consumer_orphans) if consumer_orphans
                          else "- none found")
    orphans_lines.append("\n**Emitted signals no panel or alert reads:**")
    orphans_lines.append("\n".join(f"- {o}" for o in emitter_orphans) if emitter_orphans
                          else "- none found")
    orphans_md_body = "\n".join(orphans_lines)
    orphans_md = "## Orphans\n\n" + orphans_md_body

    # -- Diff against the previous note --
    note_path = vault_root / NOTE_REL
    prev_text = None
    if note_path.exists():
        try:
            prev_text = note_path.read_text(encoding="utf-8")
        except OSError:
            prev_text = None
    diff_md = "## Diff since last run\n\n" + _diff_section(prev_text, signals_md_body, orphans_md_body)

    # -- frontmatter + preamble --
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    frontmatter = (
        "---\n"
        "type: architecture\n"
        "generated_by: obs_inventory.py\n"
        f"generated_at: {generated_at}\n"
        f"contract_version: {contract_version}\n"
        "ai-first: true\n"
        "---\n"
    )
    preamble = (
        "\n## For future agent\n\n"
        "This note is generated by `maintenance/obs_inventory.py`, deterministically "
        "(no model call) - weekly (Sunday 02:40, job `obs-inventory`) and once at every "
        "`deploy.sh apply`. It is rewritten in full each run, never appended to; edit the "
        "generator, not this file. Read `## Diff since last run` for what changed since "
        "the last run, and `## Orphans` for real rot - a panel or alert reading a signal "
        "nobody emits any more, or a signal nobody's dashboard or alert reads.\n\n"
        f"Coverage matrix cell rules: {CELL_RULES}\n"
    )

    return (frontmatter + preamble + "\n"
            + components_md + "\n\n"
            + signals_md + "\n\n"
            + matrix_md + "\n\n"
            + orphans_md + "\n\n"
            + diff_md + "\n")


# --------------------------------------------------------------------- main --
def main(argv=None):
    ap = argparse.ArgumentParser(description="Regenerate the Observability Inventory vault note.")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    if not (repo_root / "maintenance").is_dir():
        print(f"obs_inventory: repo not readable at {repo_root}", file=sys.stderr)
        return 2

    _refuse_default_vault_under_test(args.vault)
    vault_root = Path(args.vault).resolve()
    now = datetime.datetime.now(datetime.timezone.utc)

    if args.dry_run:
        print(render(repo_root, vault_root, now))
        return 0

    # Order matters: pull FIRST, so the `## Diff since last run` section
    # renders against the note as the remote has it (the phone, or another machine, may
    # have pushed one since our last run) rather than a stale local copy - then render,
    # write, push.
    vault_git.pull(vault_root)
    text = render(repo_root, vault_root, now)
    note_path = vault_root / NOTE_REL
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(text, encoding="utf-8")
    if not vault_git.push(vault_root, [NOTE_REL], "observability: inventory"):
        print("obs_inventory: vault push failed - the note is written locally but not "
              "pushed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
