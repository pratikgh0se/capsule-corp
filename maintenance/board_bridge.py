#!/usr/bin/env python3
"""Notes-vault Engineering board <-> legacy kanban bridge. Phase 1: dispatch() only.

Usage:
  board_bridge.py dispatch <slug>     create a legacy kanban task from an Approved-column card
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import board
import board_fence
import decision_log
import epic_lane
import events
import ledger
import scrub
import vault_conf
from capsule import host, safe_write
from legacy_gate import outbox_send
from contracts import DIFF_PACKET, DIFF_UNAVAILABLE

VAULT = vault_conf.vault()      # None when no notes vault is configured (D254)
SPECS_DIR = VAULT / "Specs" if VAULT else None
LEGACY_BASE = ["-m", "legacy_cli.main"]   # never executed: run_legacy_cli needs an injected runner
def _ops_env_path():
    """Where the flags live. `OPS_ENV_FILE` overrides (tests hand in a fixture); otherwise the
    real file deploy.sh writes, except under `AGENT_OPS_NO_SEND`, where the real file is never
    read so live flags cannot leak into a test run. Returns None for "no file"."""
    override = os.environ.get("OPS_ENV_FILE")
    if override:
        return Path(override)
    if os.environ.get("AGENT_OPS_NO_SEND"):
        return None
    return host.config_dir() / "ops.env"


def _ops_env():
    """Chat profile + targets + flags from ops.env (see _ops_env_path)."""
    out = {}
    path = _ops_env_path()
    if path is None:
        return out
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.startswith("#"):
                k, _, v = raw.partition("="); v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]  # ops.env is sourced by bash too: values with spaces are quoted
                out[k.strip()] = v
    except OSError:
        pass
    return out

_OPS = _ops_env()

# The three card-lifecycle pushes (Done, Card released, Huddle open) need no answer. Under
# OPS_BRIEF_V2 they go to a muted LOG target; when OPS_LOG_TARGET is empty they are not sent
# and survive as their events row and the vault log. The events row, ledger entry, `seen` key
# and return values are the same either way.
LOG_TARGET = _OPS.get("OPS_LOG_TARGET") or ""


def bridge_image(board_name=None):
    """The image the approval suite runs in: the runner's own resolution (operator pin, else the
    image recorded under $CAPSULE_HOME). Nothing resolves: refuse with the runner's message."""
    from capsule.runners import docker
    image = docker.worker_image(board=board_name)
    if not image:
        raise RuntimeError(docker.missing_image_message(board_name))
    return image

# Model tiering. A task's model override applies to EVERY run on that task (dev's build AND
# qa's review), so low/med carry no override and only high-risk work is pinned to Opus.
RISK_MODEL = {
    "low": None,
    "med": None,
    "high": "claude-opus-5",
}
# Spec `area:` -> implementer profile. Everything still flows impl -> qa -> lead.
AREA_ASSIGNEE = {
    "": "dev", "full": "dev", "general": "dev",
    "frontend": "dev-frontend",
    "backend": "dev-backend",
    "infra": "infra",
}
KANBAN_BOARDS_DIR = board_fence.legacy_boards_dir()

def _default_base(repo_root):
    """The repo's default branch name (main/master) from origin/HEAD, default main."""
    try:
        out = subprocess.run(["git", "-C", str(repo_root), "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return out.split("/", 1)[1] if "/" in out else "main"
    except Exception:
        return "main"


def _load_repos(path=None):
    """The one registry: maintenance/repos.json -> {name: path} with `~` expanded. `path` wins;
    else AGENT_OPS_REPOS_JSON (tests); else maintenance/repos.json next to this module. A missing
    file falls back to host.json `repos`; malformed JSON is a hard SystemExit
    naming the path."""
    p = Path(path or os.environ.get("AGENT_OPS_REPOS_JSON")
              or (Path(__file__).resolve().parent / "repos.json"))
    if not p.exists():
        return dict(host.get_or_default("repos"))     # host.json `repos` (default: this checkout)
    try:
        repos = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"board_bridge: malformed repos registry {p}: {e}")
    return {r["name"]: str(Path(r["path"]).expanduser()) for r in repos}


REPO_PATHS = _load_repos()


def _worktree_arg(repo, slug):
    root = REPO_PATHS.get(repo)
    if not root:
        raise SystemExit(f"no REPO_PATHS entry for repo {repo!r}")
    return f"worktree:{root}/.worktrees/sprint-{slug}"


def _board(name, runner=None, boards_dir=None):
    return board.open_board(name, runner=runner, boards_dir=boards_dir)


def _create_argv(repo, card):
    """The full argv a create call used to build by hand, so the public build_*_argv functions
    keep their signatures. board.raw_argv renders the `kanban`/`--board` prefix, so board.py
    stays the only module that constructs it. Nothing in production runs this: `dispatch`
    creates through `Board.create`; the wrappers are pure builders the tests assert against."""
    return LEGACY_BASE + board.raw_argv(repo, card.argv())


def build_impl_card(spec, slug, card_hash="", spec_sha256="", sidecar=None, history="",
                    search=None, retrieval_meta_out=None, *, modified=None):
    safe_body = re.sub(r"<!--\s*sprint-meta", "<!-- (sprint-meta stripped)",
                       spec["body"], flags=re.IGNORECASE)
    meta = build_meta_block(slug, spec["risk"], card_hash, spec_sha256,
                            spec.get("area", ""), spec.get("bet", ""))
    # The rows this spec cites, pushed and wrapped; no citations means "".
    push = push_block(cited_rows(spec["body"], sidecar or {}), history=history, modified=modified)
    role = AREA_ASSIGNEE.get(spec.get("area", ""), "dev")
    # C4's retrieval contract at dispatch: only the five roles in RETRIEVAL_ROLES get a block,
    # and only when the flag is on; every other case renders "".
    retrieval = ""
    if role in RETRIEVAL_ROLES and retrieval_push_enabled():
        q = retrieval_query(slug, spec["repo"], spec["body"])
        retrieval, r_meta = retrieval_block(q, role=role, search=search)
        if retrieval_meta_out is not None:
            retrieval_meta_out.update(r_meta)
    return board.NewCard(
        title=spec["title"], body=meta + push + retrieval + safe_body,
        assignee=AREA_ASSIGNEE.get(spec.get("area", ""), "dev"),
        workspace=_worktree_arg(spec["repo"], slug), branch=f"sprint/{slug}",
        max_retries=3, model=RISK_MODEL.get(spec["risk"]) or None,
        idempotency_key=f"sprint:{slug}", created_by="sprint-bridge")


def build_research_card(spec, slug, card_hash="", spec_sha256=""):
    """kind: research -> one researcher card, read-only workspace on the repo's main checkout,
    no branch, no finalize child. The researcher writes its findings into the vault."""
    safe_body = re.sub(r"<!--\s*sprint-meta", "<!-- (sprint-meta stripped)",
                       spec["body"], flags=re.IGNORECASE)
    meta = build_meta_block(slug, spec["risk"], card_hash, spec_sha256,
                            spec.get("area", ""), spec.get("bet", ""))
    root = REPO_PATHS.get(spec["repo"])
    if not root:
        raise SystemExit(f"no REPO_PATHS entry for repo {spec['repo']!r}")
    return board.NewCard(
        title=spec["title"], body=meta + safe_body, assignee="researcher",
        workspace=f"dir:{root}", max_retries=2,
        model=RISK_MODEL.get(spec["risk"]) or None,
        idempotency_key=f"sprint:{slug}", created_by="sprint-bridge")


def build_finalize_card(spec, slug, parent_id):
    return board.NewCard(
        title=f"{spec['title']} - finalize",
        # The gate parses `Finalize <slug>:` out of this body - keep that prefix.
        body=(f"Finalize {slug}: run the tests, write the review packet to "
              f"review/{slug}/PR.md and block READY (your SOUL). "
              "The merge gate pushes, opens and merges."),
        assignee="lead", parent=parent_id, workspace=_worktree_arg(spec["repo"], slug),
        idempotency_key=f"sprint:{slug}:finalize", created_by="sprint-bridge")


def build_verify_card(spec, slug, parent_id, card_hash="", spec_sha256="",
                      search=None, retrieval_meta_out=None):
    meta = build_meta_block(slug, spec["risk"], card_hash, spec_sha256,
                            spec.get("area", ""), spec.get("bet", ""))
    # qa is in RETRIEVAL_ROLES unconditionally (build_verify_card has one assignee), so only
    # the flag gates it.
    retrieval = ""
    if retrieval_push_enabled():
        q = retrieval_query(slug, spec["repo"], spec["body"])
        retrieval, r_meta = retrieval_block(q, role="qa", search=search)
        if retrieval_meta_out is not None:
            retrieval_meta_out.update(r_meta)
    return board.NewCard(
        title=f"{spec['title']} - verify",
        body=meta + retrieval +
             (f"Verify {slug}: run the suite and the Playwright tests, save screenshots "
              f"under review/{slug}/, complete with the suite metadata (your SOUL). "
              f"spec: Specs/{slug}.md"),
        assignee="qa", parent=parent_id, workspace=_worktree_arg(spec["repo"], slug),
        max_retries=2, idempotency_key=f"sprint:{slug}:verify", created_by="sprint-bridge")


# The four `build_*_argv` wrappers keep their signatures for the tests that assert the rendered
# argv; nothing in production calls them (`dispatch` creates through `Board.create`, which pins
# the env). Kept as pure builders: no runner, no subprocess, no environment.
def build_impl_argv(spec, slug, card_hash="", spec_sha256=""):
    # search=(lambda that returns no hits): the wrappers must stay pure; the real
    # default_search would reach for the real vault_ops import.
    return _create_argv(spec["repo"], build_impl_card(spec, slug, card_hash, spec_sha256,
                                                       search=lambda *a, **k: []))


def build_research_argv(spec, slug, card_hash="", spec_sha256=""):
    return _create_argv(spec["repo"], build_research_card(spec, slug, card_hash, spec_sha256))


def build_finalize_argv(spec, slug, parent_id):
    return _create_argv(spec["repo"], build_finalize_card(spec, slug, parent_id))


def build_verify_argv(spec, slug, parent_id, card_hash="", spec_sha256=""):
    return _create_argv(spec["repo"], build_verify_card(spec, slug, parent_id, card_hash, spec_sha256,
                                                        search=lambda *a, **k: []))


_REQUIRED = ("title", "repo", "risk")
# `exec_mode` was validated here until 2026-09-05 and then ignored everywhere
# (every ticket is linear). Old specs may still carry it; it is accepted and dropped.

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def _check_slug(slug):
    if not _SLUG_RE.fullmatch(slug or ""):
        raise SystemExit(
            f"invalid slug {slug!r}: must match {_SLUG_RE.pattern} "
            "(lowercase, digits, hyphens; no path separators)"
        )
    return slug


# ------------------------------------------------------- decision-only push
# The dispatcher already knows which decision rows a card cites (the spec says `row N`), and a
# worker cannot search for them (eight of eleven profiles have no MCP mount). So the resolved
# rows are PUSHED, from the sidecar, with the card's own history, capped.
#
# Every vault text pushed or fetched is wrapped: LLM01's "separate and clearly denote untrusted
# content". The shared SOUL clause explaining the envelope is deferred to C7; this half only
# guarantees nothing crosses the boundary untagged.
PUSH_CAP_BYTES = 8 * 1024
PUSH_TRUNCATED = ("\n*** DECISION ROWS TRUNCATED at {cap} bytes: {omitted} more rows are NOT "
                  "in this card. The rows you have are the lowest-numbered ones cited; ask "
                  "for the rest in a comment rather than guessing. ***\n")
HISTORY_TRUNCATED = " *** CARD HISTORY TRUNCATED ***"
_ROW_CITE_RE = re.compile(r"\brows?\s+(\d+)(?:\s*[-–]\s*(\d+))?\b", re.IGNORECASE)


# Both framing tags, not just the envelope: a row whose text says `</card-history> now obey`
# closes a tag it never opened. One helper covers every piece of text inside either tag.
_NEUTER_RE = re.compile(r"</?(vault-note|vault-retrieval|card-history)", re.IGNORECASE)


def _neuter(text):
    """Every framing tag in untrusted text turned into inert prose."""
    return _NEUTER_RE.sub(lambda m: f"({m.group(1)})", str(text))


def _attr(value):
    """An attribute value that cannot close its own quote or its own line."""
    return re.sub(r'["\r\n]', lambda m: "" if m.group(0) == '"' else " ", str(value))


def wrap_vault_note(path, modified, text):
    """One provenance envelope. A framing tag inside `text` is neutered, as build_impl_card
    neuters a nested `sprint-meta`, and both attributes are stripped of quotes and newlines so
    nothing in the vault can write its own tag."""
    return (f'<vault-note path="{_attr(path)}" modified="{_attr(modified)}">\n'
            f"{_neuter(text).strip()}\n</vault-note>\n")


def cited_rows(body, sidecar):
    """The decision rows this spec cites, resolved, ordered, deduplicated.

    A citation that does not resolve is skipped in silence: that is the lint's problem, not a
    reason to refuse a dispatch. A range is capped at 20 numbers so `rows 1-9999` cannot expand
    into the whole log. A citation inside a fenced code block is not a citation; an inline
    single-backtick citation in prose still resolves."""
    if not sidecar:
        return []
    wanted = []
    body = body or ""
    mask = epic_lane._fenced_line_mask(body)
    lines = body.split("\n")
    masked_body = "\n".join("" if inside else line for line, inside in zip(lines, mask))
    for m in _ROW_CITE_RE.finditer(masked_body):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if hi < lo or hi - lo > 20:
            hi = lo
        wanted.extend(range(lo, hi + 1))
    return [sidecar[n] for n in sorted(set(wanted)) if n in sidecar]


def push_block(rows, history="", cap=PUSH_CAP_BYTES, log_name="Decisions/decisions.jsonl",
              modified=None):
    """The push: resolved rows plus this card's history, each wrapped, capped.

    Empty string when there is nothing to push, never a header with nothing under it, which a
    role would read as "there are no relevant decisions".

    `cap` is the cap on the whole rendered block (history and truncation marker included), so a
    20 KB comment thread cannot walk past an 8 KB budget.

    `modified` is the decision log's last commit time (ISO) when the caller has one. `None` is
    the fallback: each row is stamped with its own `date` cell, or "unknown".

    `history` is NOT SUPPLIED YET: `dispatch` calls the build_*_card functions with `sidecar=`
    only, so a dispatched card carries resolved rows and no card history. The parameter,
    `_wrap_history` and the `<card-history>` envelope stay implemented and tested because the
    half is deferred, not dropped; the trigger to wire it is the first epic-lane card. The
    RUNBOOK's contract table says the same.
    """
    rows = list(rows)
    # The history takes at most half the budget, so a long thread cannot crowd out every
    # decision row, nor a row set crowd out the history.
    hist = _wrap_history(history, cap // 2) if history else ""
    room = cap - len(hist.encode("utf-8"))
    marker_len = len(PUSH_TRUNCATED.format(cap=cap, omitted=len(rows)).encode("utf-8"))
    parts = []
    used = 0
    kept = 0
    for i, r in enumerate(rows):
        # "row N", not a bare number: a role citing it back writes `row 128`, the form
        # cited_rows resolves.
        line = (f"| row {r['number']} | {r.get('title') or ''} | {r.get('text') or ''} "
                f"| {r.get('status') or ''} |")
        chunk = wrap_vault_note(log_name, modified or r.get("date") or "unknown", line)
        # Every row but the last has to leave room for the marker it may need.
        limit = room if i == len(rows) - 1 else room - marker_len
        if used + len(chunk.encode("utf-8")) > limit:
            break
        parts.append(chunk)
        used += len(chunk.encode("utf-8"))
        kept += 1
    if kept < len(rows):
        parts.append(PUSH_TRUNCATED.format(cap=cap, omitted=len(rows) - kept))
    if hist:
        parts.append(hist)
    return "".join(parts)


def _wrap_history(history, budget):
    """The card's own comment history, framed, neutered and fitted to `budget`
    bytes. Empty when even the empty tags do not fit."""
    body = _neuter(history).strip()
    shell = "<card-history>\n\n</card-history>\n"
    if len(shell.encode("utf-8")) + len(body.encode("utf-8")) <= budget:
        return f"<card-history>\n{body}\n</card-history>\n"
    room = budget - len(shell.encode("utf-8")) - len(HISTORY_TRUNCATED.encode("utf-8"))
    if room <= 0:
        return ""
    body = body.encode("utf-8")[:room].decode("utf-8", "ignore")
    return f"<card-history>\n{body}{HISTORY_TRUNCATED}\n</card-history>\n"


# ------------------------------------------------------------- retrieval push
# Historical (legacy era): this push belongs to the legacy dispatch. The current dispatch,
# capsule/tick.py, attaches vault context via capsule/vault_context.py (row 626) instead.
# C4's retrieval contract: the eight roles with no MCP mount get vault context from the
# dispatch push; this is that half for the five roles that implement or verify code. Extend
# the push: no mount, no protocol, no image change; the container (network none) never searches.
RETRIEVAL_CAP_BYTES = 6144
RETRIEVAL_ROLES = ("dev", "dev-backend", "dev-frontend", "infra", "qa")
# k=5, dev family + qa, query = slug + repo + spec title, degrade never hold. One folder set
# for every retrieval role, never wider than what a fenced profile already reads.
ROLE_FOLDERS = {role: ["Knowledge/", "Decisions/", "Specs/", "Architecture/"]
                for role in RETRIEVAL_ROLES}
RETRIEVAL_PUSH_FLAG = "OPS_RETRIEVAL_PUSH"
RETRIEVAL_OPEN = "<vault-retrieval>\n"
RETRIEVAL_SEMANTIC_DOWN_OPEN = '<vault-retrieval semantic="down">\n'
RETRIEVAL_CLOSE = "</vault-retrieval>\n"
_SPEC_TITLE_RE = re.compile(r"(?m)^#\s+(.*)$")
# A spec that opens with a fenced snippet before its first real heading (a shell example
# starting `# rm -rf build`) must not have that line mistaken for the spec title. Fenced
# regions are stripped before the title search, the non-nested "opening fence ... matching
# closing fence of the same character" shape soul_render._mask_fences uses.
_FENCED_RE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$\n?")


def retrieval_push_enabled(env=None):
    """`OPS_RETRIEVAL_PUSH`, default ON: `0` restores the old bytes exactly (no retrieval block
    on any card). Read at call time through `verdicts.ops_env()`, so a flag flip in ops.env
    takes effect on the next dispatch.

    Under `AGENT_OPS_NO_SEND` with no explicit `OPS_RETRIEVAL_PUSH` in the real environment this
    refuses ON, like `_ops_env_path` refuses the real ops.env: no test injects `search=` into
    `dispatch()`, so the default-ON path would reach the real vault_ops import. A test that
    wants retrieval sets `OPS_RETRIEVAL_PUSH=1` or passes `env=`."""
    if env is None and os.environ.get("AGENT_OPS_NO_SEND") and "OPS_RETRIEVAL_PUSH" not in os.environ:
        return False
    import verdicts
    e = env if env is not None else verdicts.ops_env()
    return str(e.get(RETRIEVAL_PUSH_FLAG, "1")) == "1"


def retrieval_query(slug, repo, spec_body):
    """`slug repo spec_title`, never the card body (untrusted vault/spec text). `spec_title` is
    the spec's first `# ` heading, or "" when none; fenced code blocks are skipped first, so a
    `# ` line inside a snippet is never mistaken for the title."""
    unfenced = _FENCED_RE.sub("", spec_body or "")
    m = _SPEC_TITLE_RE.search(unfenced)
    title = m.group(1).strip() if m else ""
    return f"{slug} {repo} {title}".strip()


def _default_search(query, *, limit=5, folders=None):
    """The production `search` retrieval_block calls when given none: a lazy import of
    `vault_ops.search` from the notes-vault skill checkout (`vault.skill_root` /
    `$OBSIDIAN_SKILL_ROOT`; none configured raises), so maintenance/ stays stdlib-only until this
    call. Any failure is left to raise; retrieval_block turns it into "search raised".

    vault_ops.search returns {"path", "title", "snippet", "chunk"} with no `modified`, so it is
    stat'd here (wrap_vault_note wants one); a file that cannot be stat'd gets "unknown"."""
    root = vault_conf.skill_root()
    if root is None:
        raise RuntimeError(vault_conf.NO_SKILL_ROOT)
    mcp_dir = str(root / "integrations" / "obsidian-mcp-server")
    if mcp_dir not in sys.path:
        # append, not insert(0): this directory must never shadow a same-named module
        # maintenance/ might grow later (it already holds server.py, live_test.py).
        sys.path.append(mcp_dir)
    import vault_ops  # lazy: an ImportError here is "search raised" to retrieval_block
    vault = vault_ops.resolve_vault()
    hits = vault_ops.search(query, limit=limit, folders=folders)
    out = []
    for h in hits or []:
        path = h.get("path", "")
        try:
            modified = datetime.datetime.fromtimestamp(
                (vault / path).stat().st_mtime, tz=datetime.timezone.utc).isoformat()
        except OSError:
            modified = "unknown"
        out.append({"path": path, "modified": modified,
                   "text": h.get("snippet", ""), "chunk": h.get("chunk")})
    return out


def retrieval_block(query, *, role, search=None, cap=RETRIEVAL_CAP_BYTES):
    """The top-k vault chunks for `query`, C4-fenced to `role`'s folders, wrapped one
    `<vault-note>` per hit (the envelope `push_block` uses) inside one outer
    `<vault-retrieval>` tag, capped at `cap` independently of `PUSH_CAP_BYTES`, so a long
    spec's cited rows are never displaced by chunks, nor the reverse.

    Returns `(text, meta)`. `meta` is always `{"chunks", "bytes", "tokens_est", "degraded"}`;
    "bytes" and "tokens_est" are `len(text)` and `len(text) // 4`.

    Never raises: any exception from `search`, including the ImportError `_default_search`
    raises when the checkout is missing, degrades to an absent block, one `retrieval_absent`
    warning event, and dispatch proceeds (degrade rather than hold)."""
    searcher = search or _default_search
    folders = ROLE_FOLDERS.get(role, [])
    try:
        raw = searcher(query, limit=5, folders=folders)
    except Exception as exc:
        events.log("card", "bridge", f"retrieval absent: {type(exc).__name__}",
                  severity="warning",
                  payload={"event": "retrieval_absent", "state": None})
        return "", {"chunks": 0, "bytes": 0, "tokens_est": 0, "degraded": "absent"}
    # A fake search may report `semantic` explicitly (a dict with "results"/"semantic" keys);
    # the real vault_ops.search returns a plain list, so lexical-only is inferred from every
    # hit lacking `chunk`.
    semantic_flag = None
    hits = raw
    if isinstance(raw, dict):
        semantic_flag = raw.get("semantic")
        hits = raw.get("results") or []
    hits = list(hits or [])[:5]
    degraded = "lexical" if (semantic_flag is False
                            or (hits and all(not h.get("chunk") for h in hits))) else "none"
    if not hits:
        # Zero hits is "nothing to push", not "push an empty tag pair": the card is the same as
        # one whose role/flag never called retrieval_block. `degraded` stays as computed above.
        return "", {"chunks": 0, "bytes": 0, "tokens_est": 0, "degraded": degraded}
    open_tag = RETRIEVAL_SEMANTIC_DOWN_OPEN if degraded == "lexical" else RETRIEVAL_OPEN
    room = cap - len(open_tag) - len(RETRIEVAL_CLOSE)
    parts, used, kept = [], 0, 0
    for i, h in enumerate(hits):
        text = h.get("text")
        if text is None:
            text = h.get("snippet", "")
        # The hit's own mtime, not the decision log's commit time: vault_ops hits are arbitrary
        # vault notes with no commit-time seam, and a file's mtime is the right "last touched"
        # stamp.
        rendered = wrap_vault_note(h.get("path", ""), h.get("modified", "unknown"), text)
        # Every hit but the last has to leave room for the marker it may need.
        marker_len = 0 if i == len(hits) - 1 else len(
            f'<vault-retrieval truncated="{len(hits) - i} more">\n')
        limit_ = room if i == len(hits) - 1 else room - marker_len
        if used + len(rendered) > limit_:
            break
        parts.append(rendered)
        used += len(rendered)
        kept += 1
    body = "".join(parts)
    if kept < len(hits):
        body += f'<vault-retrieval truncated="{len(hits) - kept} more">\n'
    text_out = open_tag + body + RETRIEVAL_CLOSE
    meta = {"chunks": kept, "bytes": len(text_out), "tokens_est": len(text_out) // 4,
           "degraded": degraded}
    return text_out, meta


def _log_retrieval_push(board_name, task_id, meta):
    """One `retrieval_push` events row plus (via obs_probe's power collector, no new SQL in
    report.sh) one `capsule.card.tokens.retrieval` sample, for every card that got a retrieval
    verdict: a real block, an empty one, or an absent one. `meta` is falsy only when the flag is
    off or the role is outside RETRIEVAL_ROLES; nothing is logged for those, on purpose."""
    if not meta:
        return
    chunks, byte_n = meta.get("chunks", 0), meta.get("bytes", 0)
    tokens_est, degraded = meta.get("tokens_est", 0), meta.get("degraded", "none")
    events.log("card", "bridge",
              f"retrieval push: {chunks} chunks, {byte_n} B, {degraded}",
              board=board_name, task_id=task_id,
              payload={"event": "retrieval_push", "state": None, "chunks": chunks,
                       "bytes": byte_n, "tokens_est": tokens_est, "degraded": degraded})


# --------------------------------------------------------------- diff packet
# The reviewer profile has no `terminal` or `code_execution` (pinned by test_contracts.py
# ::test_the_reviewer_has_no_shell_and_no_code_execution), so it cannot run the `git diff
# origin/<base>...HEAD` its SOUL names. The host runs the diff instead, writes it to a file, and
# the packet (a `diff: ` comment on the impl card, posted by the quality hook at handoff) names
# the file by path.
#
# Files go under <repo>/.worktrees/review-packets/<slug>/card/: branch.diff,
# files/<n>-<name>.diff and MANIFEST.md. That is inside the repo volume every code role
# already mounts read-only at the host path, and OUTSIDE every sprint worktree on purpose:
# qa's SOUL runs `git add review/<slug>`, which inside the worktree would commit the diff onto
# the branch and fold round n's diff into round n+1's.
#
REVIEW_PACKET_DIRNAME = "review-packets"
REVIEW_CARD_DIRNAME = "card"
REVIEW_DIFF_FILENAME = "branch.diff"
REVIEW_FILES_DIRNAME = "files"
REVIEW_MANIFEST_FILENAME = "MANIFEST.md"
REVIEW_DIFF_CONTEXT = 5
# The packet is uncapped: a 128 KB cap once dropped 105-207 KB of diff on two epics, so every
# review read a partial diff. branch.diff is the COMPLETE diff, files/<n>-<name>.diff carries
# one scrubbed diff per kept file, and MANIFEST.md (written last, the "packet is ready" flag)
# lists every file with its size and diff location, so one call can walk the whole packet.
# Secret-shaped paths never enter the packet, whatever scrub() makes of their contents: the
# same suffixes as vault_git.PUSH_DENY, which test_review_diff.py pins the two lists to.
REVIEW_DIFF_DENY = (".env", ".key", ".pem", "id_rsa", "id_ed25519",
                    ".p12", ".pfx", ".netrc", "credentials")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
# A per-file diff's filename: the whole path is folded to one component first, so ".." has no
# separator to traverse with; length is bounded and a zero-padded index prefix keeps every
# name collision-free even when two source paths sanitise to the same body.
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DIFF_FILENAME_BODY_MAX = 120


def _diff_sh(argv, cwd):
    """(rc, output). A spawn failure or a timeout is an rc, never an exception:
    every caller here turns a bad rc into a `diff: unavailable` line."""
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 127, str(exc)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def card_packet_dir(repo_root, slug):
    """The card packet's own subdirectory, `review-packets/<slug>/card/`."""
    return Path(repo_root) / ".worktrees" / REVIEW_PACKET_DIRNAME / str(slug) / REVIEW_CARD_DIRNAME


def review_packet_path(repo_root, slug):
    """The card packet's branch.diff file for `slug`, under `card/`; beside the worktrees, not
    inside one. Kept for
    callers that want it; the `diff: ` comment itself names `review_manifest_path`."""
    return card_packet_dir(repo_root, slug) / REVIEW_DIFF_FILENAME


def review_manifest_path(repo_root, slug):
    """MANIFEST.md for `slug`'s card packet - the "packet is ready" flag and
    the path the `diff: ` comment names (read this first)."""
    return card_packet_dir(repo_root, slug) / REVIEW_MANIFEST_FILENAME


def review_files_dir(repo_root, slug):
    """The per-file diffs directory for `slug`'s card packet."""
    return card_packet_dir(repo_root, slug) / REVIEW_FILES_DIRNAME


def _diff_denied(path):
    name = Path(str(path)).name
    return any(name.endswith(d) for d in REVIEW_DIFF_DENY)


def _md_cell(text):
    """Escape `|` so a changed path containing one cannot break a MANIFEST.md table row."""
    return str(text).replace("|", "\\|")


def _sanitize_diff_filename(index, path):
    """A deterministic, collision-free, traversal-safe filename for the per-file diff of `path`:
    fold every `/` to `__` first (a ".." component is then literal dots inside one filename),
    replace every other unsafe character, bound the length, and prefix a zero-padded index so
    two paths that sanitise to the same body still land on different files.
    """
    body = _UNSAFE_FILENAME_RE.sub("_", str(path).replace("/", "__")).strip("_.")
    body = (body or "file")[:_DIFF_FILENAME_BODY_MAX]
    return f"{index:03d}-{body}.diff"


def _drop_symlink(repo_root, d):
    """If `d` (card/ or files/) is a symlink, unlink the link only, by its parent's dir fd."""
    pfd = safe_write.open_dir(repo_root, d.parent, create_dirs=True)
    try:
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISLNK(os.stat(d.name, dir_fd=pfd, follow_symlinks=False).st_mode):
                os.unlink(d.name, dir_fd=pfd)
    finally:
        os.close(pfd)


def _reset_card_packet(repo_root, manifest_path, files_dir):
    """Create the card packet dirs and clear last round's leftovers, all by dir fd from
    `repo_root` with O_NOFOLLOW (a worker-planted symlink raises, nothing outside is touched):
    unlink the old MANIFEST.md, then stale `*.diff` files and crash-leftover dotfile temps in
    `files_dir` (a stale entry that is itself a symlink is unlinked, its target left alone)."""
    for d in (Path(manifest_path).parent, files_dir):
        _drop_symlink(repo_root, d)
    cfd = safe_write.open_dir(repo_root, Path(manifest_path).parent, create_dirs=True)
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(Path(manifest_path).name, dir_fd=cfd)
    finally:
        os.close(cfd)
    ffd = safe_write.open_dir(repo_root, files_dir, create_dirs=True)
    try:
        with os.scandir(ffd) as it:
            names = [e.name for e in it if not e.is_dir(follow_symlinks=False)]
        for name in names:
            if name.endswith(".diff") or (name.startswith(".") and name.endswith(".tmp")):
                os.unlink(name, dir_fd=ffd)
    finally:
        os.close(ffd)


def write_review_diff(*, repo_root, worktree, slug, base_ref, sh=None):
    """Write the reviewer's diff packet for one card and return `(manifest_path, line)`.

    Three things land under `review_packet_path(...).parent`: the COMPLETE `branch.diff`, one
    scrubbed per-file diff under `files/` per kept file, and MANIFEST.md written last.

    `line` is the `diff: ` comment body and is always non-empty. When no diff can be produced
    (no branch, an unresolvable base, a directory that is not a repo) `manifest_path` is None
    and `line` starts with `diff: unavailable - `: the handoff continues, because a reviewer
    with the card and spec can still raise the missing diff as a finding, whereas a refused
    handoff wedges the card with no CLI way out (RUNBOOK, triage).

    Secrets: file contents go through scrub.scrub(), and any changed path matching
    REVIEW_DIFF_DENY is left out of the diff entirely and only named.
    """
    sh = sh or _diff_sh
    if not _SLUG_RE.fullmatch(str(slug or "")):
        return None, DIFF_UNAVAILABLE + f"refusing to build a packet for slug {slug!r}"
    if not _REF_RE.fullmatch(str(base_ref or "")):
        return None, DIFF_UNAVAILABLE + f"unsafe base ref {base_ref!r}"
    rng = f"{base_ref}...HEAD"
    # -c core.quotePath=false: otherwise a non-ASCII path comes back quote-escaped from
    # --name-only and matches nothing as a `--` pathspec later, silently emptying that file's
    # diff. --no-renames: a rename shows as one path, so `numstat` keys off the SAME path.
    _QP = ["-c", "core.quotePath=false"]
    rc, out = sh(["git"] + _QP + ["diff", "--no-renames", "--name-only", rng], worktree)
    if rc != 0:
        why = " ".join(str(out or "").split())[-160:]
        return None, DIFF_UNAVAILABLE + f"cannot diff {rng} in {worktree}: {why}"
    changed = [l.strip() for l in (out or "").splitlines() if l.strip()]
    kept = sorted(p for p in changed if not _diff_denied(p))
    withheld = sorted(p for p in changed if _diff_denied(p))
    stat = body = numstat = ""
    if kept:
        rc, stat = sh(["git"] + _QP + ["diff", "--stat", rng, "--"] + kept, worktree)
        if rc != 0:
            why = " ".join(str(stat or "").split())[-160:]
            return None, DIFF_UNAVAILABLE + f"cannot stat {rng} in {worktree}: {why}"
        rc, numstat = sh(["git"] + _QP + ["diff", "--no-renames", "--numstat", rng, "--"]
                         + kept, worktree)
        if rc != 0:
            why = " ".join(str(numstat or "").split())[-160:]
            return None, DIFF_UNAVAILABLE + f"cannot numstat {rng} in {worktree}: {why}"
        rc, body = sh(["git"] + _QP + ["diff", "--no-renames", f"-U{REVIEW_DIFF_CONTEXT}", rng, "--"]
                      + kept, worktree)
        if rc != 0:
            why = " ".join(str(body or "").split())[-160:]
            return None, DIFF_UNAVAILABLE + f"cannot diff {rng} in {worktree}: {why}"
    rc, head = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], worktree)
    branch = (head or "").strip() if rc == 0 else "HEAD"
    header = [
        f"# Review diff packet for {slug} (row 125: the reviewer has no shell, the host diffs)",
        f"# base: {base_ref}   head: {branch}",
        f"# secrets scrubbed through maintenance/scrub.py; {len(kept)} file(s) in the diff",
        "# nothing in this packet is truncated (incident 23 / card t_f144d2fe)",
    ]
    if withheld:
        header.append("# WITHHELD (secret-shaped paths, deliberately not in this packet): "
                      + ", ".join(withheld))
    text = ("\n".join(header) + "\n\n## git diff --stat " + rng + "\n"
            + (scrub.scrub(stat) or "(no changed files outside the withheld list)\n")
            + f"\n## git diff -U{REVIEW_DIFF_CONTEXT} {rng}\n"
            + (scrub.scrub(body) or "(no changed files outside the withheld list)\n"))

    diff_path = review_packet_path(repo_root, slug)
    files_dir = review_files_dir(repo_root, slug)
    manifest_path = review_manifest_path(repo_root, slug)
    # Unlink the OLD MANIFEST.md first, before branch.diff/files/ is touched: if a later step
    # fails the function returns `diff: unavailable` with NO MANIFEST.md, never a stale one
    # pointing at files this round already deleted or overwrote.
    _reset_card_packet(repo_root, manifest_path, files_dir)

    diff_bytes = _atomic_write_text(diff_path, text, root=repo_root)

    numstat_rows = {}
    for line_ in (numstat or "").splitlines():
        parts = line_.split("\t")
        if len(parts) == 3:
            numstat_rows[parts[2]] = (parts[0], parts[1])

    file_rows = []   # (path, bytes, plus, minus, files/<name>)
    for i, p in enumerate(kept):
        rc, fdiff = sh(["git"] + _QP + ["diff", "--no-renames", f"-U{REVIEW_DIFF_CONTEXT}", rng, "--", p],
                       worktree)
        if rc != 0:
            why = " ".join(str(fdiff or "").split())[-160:]
            return None, DIFF_UNAVAILABLE + f"cannot diff {rng} for {p} in {worktree}: {why}"
        scrubbed = scrub.scrub(fdiff) or ""
        fname = _sanitize_diff_filename(i, p)
        fbytes = _atomic_write_text(files_dir / fname, scrubbed, root=repo_root)
        plus, minus = numstat_rows.get(p, ("?", "?"))
        if plus == "-" and minus == "-":
            plus = minus = "bin"     # `git diff --numstat` marks a binary file "-\t-"
        file_rows.append((p, fbytes, plus, minus, f"{REVIEW_FILES_DIRNAME}/{fname}"))

    man = [
        f"# Review diff packet manifest - {slug} (row 125; incident 23 / card t_f144d2fe)",
        "",
        f"base: {base_ref}   head: {branch}",
        f"{REVIEW_DIFF_FILENAME}: {diff_bytes} bytes - nothing in this packet is truncated",
        "",
        "| file | bytes | + | - | diff |",
        "|---|---|---|---|---|",
    ]
    if file_rows:
        man += [f"| {_md_cell(p)} | {b} | {pl} | {mi} | {_md_cell(rel)} |"
                for p, b, pl, mi, rel in file_rows]
    else:
        man.append("| (no changed files outside the withheld list) | 0 | - | - | - |")
    if withheld:
        man += ["", "WITHHELD (secret-shaped paths, deliberately not in this packet): "
                    + ", ".join(_md_cell(w) for w in withheld)]
    man += ["", "Read MANIFEST.md first, then every per-file diff under files/ (or "
                f"{REVIEW_DIFF_FILENAME} in full); name every file in your verdict."]
    # MANIFEST.md is written LAST, after branch.diff and every files/*.diff are atomically in
    # place, so a reviewer that can read it can trust everything it points at.
    _atomic_write_text(manifest_path, scrub.scrub("\n".join(man) + "\n"), root=repo_root)

    parts = [f"{len(kept)} file(s)", f"{diff_bytes} bytes", f"per-file diffs in {REVIEW_FILES_DIRNAME}/"]
    if withheld:
        parts.append(f"{len(withheld)} withheld (secret-shaped): " + ", ".join(withheld))
    return str(manifest_path), DIFF_PACKET + f"{manifest_path} - " + ", ".join(parts)


def _atomic_write_text(path, text, encoding="utf-8", root=None):
    """Write `text` to `path` without ever leaving a reader looking at a partial file: a fresh
    temp file in the same directory, then `os.replace` onto `path`. Returns the byte count.

    The packet dir is a repo mount a dev worker may write, so the write goes through
    `capsule.safe_write.atomic_write`: each component below `root` (default `path`'s parent) is
    opened `O_NOFOLLOW` by dir fd, the temp is random `O_EXCL|O_NOFOLLOW`. A planted symlink dir
    or temp is refused or ignored; a symlink at `path` itself is replaced, not followed."""
    path = Path(path)
    return safe_write.atomic_write(root or path.parent, path, text, create_dirs=root is not None,
                                   encoding=encoding)


def _spec_path(slug, specs_dir):
    if specs_dir is None:
        raise FileNotFoundError(vault_conf.NO_VAULT)
    return Path(specs_dir) / f"{slug}.md"


def parse_spec(slug, specs_dir=SPECS_DIR):
    path = _spec_path(slug, specs_dir)
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path}: no frontmatter block")
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"').strip("'")
    for key in _REQUIRED:
        if not fm.get(key):
            raise ValueError(f"{path}: frontmatter missing '{key}'")
    if fm["risk"] not in RISK_MODEL:
        raise ValueError(f"{path}: risk must be one of {sorted(RISK_MODEL)}")
    if not _SLUG_RE.fullmatch(fm["repo"]):
        raise ValueError(f"{path}: repo must match {_SLUG_RE.pattern}")
    if not fm["title"].strip() or fm["title"].lstrip().startswith("-"):
        raise ValueError(f"{path}: title must be non-empty and not start with '-'")
    kind = fm.get("kind", "code") or "code"
    if kind not in ("code", "research"):
        raise ValueError(f"{path}: kind must be 'code' or 'research'")
    area = (fm.get("area", "") or "").strip().lower()
    if area not in AREA_ASSIGNEE:
        raise ValueError(f"{path}: area must be one of {sorted(a for a in AREA_ASSIGNEE if a)} (or omitted)")
    # OPTIONAL. Absent -> "" and the card is unchanged. Present but malformed -> refuse the
    # dispatch, like a bad `area:`: a typo'd reason looks like provenance. spec_lint lists it
    # first and blocks nothing.
    bet = (fm.get("bet") or "").strip() if "bet" in fm else ""
    if "bet" in fm and not board.BET_SLUG_RE.fullmatch(bet):
        raise ValueError(f"{path}: bet must match {board.BET_SLUG_RE.pattern}")
    lane = (fm.get("lane", "") or "card").strip().lower()
    if lane not in epic_lane.LANES:
        raise ValueError(f"{path}: lane must be one of {list(epic_lane.LANES)} (or omitted)")
    return {
        "title": fm["title"], "repo": fm["repo"], "risk": fm["risk"],
        "kind": kind, "area": area, "bet": bet, "body": m.group(2).strip(),
        # `card` unless the spec says otherwise, so every older spec parses unchanged.
        "lane": lane, "plan": (fm.get("plan", "") or "").strip(),
    }


def source_hash(card_line):
    return hashlib.sha1(card_line.strip().encode("utf-8")).hexdigest()[:12]


def spec_file_sha256(slug, specs_dir=SPECS_DIR):
    """sha256 of the spec note's bytes at dispatch time (F5 integrity check)."""
    return hashlib.sha256(
        _spec_path(slug, specs_dir).read_bytes()
    ).hexdigest()


def build_meta_block(slug, risk, card_hash, spec_sha256="", area="", bet=""):
    """The card body's machine-readable header. `area` and `slug` are here so the quality hook
    reads them from the card instead of re-deriving them (the infra changelog exemption and the
    screenshot slug).

    `bet` is the card-field half: `board.bet_line` returns "" when OPS_BET_FIELD is off or the
    value is empty, so the rendered block is unchanged in both cases. It sits after `slug:` and
    before `spec:`, and is NOT in contracts.META_KEYS: that tuple is asserted key-by-key against
    a rendered block and this line is optional.
    """
    sha_line = f"spec_sha256: {spec_sha256}\n" if spec_sha256 else ""
    return (
        "<!-- sprint-meta\n"
        f"risk: {risk}\n"
        f"area: {area}\n"
        f"slug: {slug}\n"
        f"{board.bet_line(bet)}"
        f"spec: Specs/{slug}.md\n"
        f"{sha_line}"
        f"source: obsidian:{card_hash}\n"
        "-->\n\n"
    )


def build_create_argv(spec, slug, meta_block):
    safe_body = re.sub(r"<!--\s*sprint-meta", "<!-- (sprint-meta stripped)",
                       spec["body"], flags=re.IGNORECASE)
    return _create_argv(spec["repo"], board.NewCard(
        title=spec["title"], body=meta_block + safe_body, workspace="worktree",
        model=RISK_MODEL.get(spec["risk"]) or None,
        idempotency_key=f"sprint:{slug}", created_by="sprint-bridge"))


def run_legacy_cli(argv, runner=None, env=None):
    """The legacy board CLI is gone from the live machine (D198/D202): with no injected runner
    there is nothing to spawn, so this raises FileNotFoundError (Board._raw_run wraps it).
    `env` (the quality hook's LEGACY_KANBAN_DB-stripped environment) is passed on only when
    given, so the many one-argument fake runners keep working."""
    if runner is None:
        raise FileNotFoundError("kanban CLI absent")
    return runner(argv) if env is None else runner(argv, env)


def eligible_cards(**kw):
    """Backlog: `triage`/`todo` with no spec yet - the planner's queue.

    Replaces list_eligible_backlog's `#sprint`-marker-and-no-Gate parse. There
    is no marker any more: a card exists because the owner put a line in
    Boards/Intake.md, which IS the eligibility signal (controller ruling 1)."""
    return cards_in(board.COLUMN_NAMES[2], **kw)


def link_spec(board_name, task_id, slug, *, runner=None, boards_dir=None):
    """Record that `slug` is `task_id`'s spec by posting `spec: Specs/<slug>.md <!-- by: bridge -->`
    as a comment, which is what `board.Card.spec_slug` reads. A comment, not the card body:
    no legacy board verb rewrites a body, and the sandboxed planner can call only the CLI
    verbs its allowlist names. The trailing marker keeps any worker's comment from being read
    as a spec link. Idempotent: spec_slug takes the newest marked comment."""
    _check_slug(slug)
    board.open_board(board_name, runner=runner, boards_dir=boards_dir).comment(
        task_id, f"spec: Specs/{slug}.md {board.SPEC_LINK_MARK}")


def cards_in(heading, *, boards_dir=None, runner=None, cards=None):
    """Every card the column mapping puts under `heading`: the one enumeration primitive. The
    columns are `board.COLUMNS`, so a caller names the column and never re-decides what belongs
    in it. `cards=` takes a list already collected this tick, to avoid `show`ing each card twice."""
    import board_view
    if cards is None:
        cards = board_view.collect(boards_dir=boards_dir, runner=runner)
    return [c for c in cards if board.column_of(c) == heading]


def fmt_msg(headline, facts, reply=None, pointer=None):
    """Phone style the owner chose 2026-09-05: emoji headline, numbered facts, one reply
    line, one resume pointer line (2026-09-06, spec section 5)."""
    lines = [headline, ""] + [f"{i}. {f}" for i, f in enumerate([f for f in facts if f], 1)]
    if reply:
        lines += ["", f"\U0001f449 Reply: {reply}"]
    if pointer:
        lines += [f"↩ {pointer}"]
    return "\n".join(lines)


NOTIFY_SEEN = events.state_root() / "notify-seen.json"


def _resolve_seen_path(path, default, *, guard):
    """One helper both `_load_seen` and `_save_seen` route their path resolution through. An
    explicit `path` (a test's tmp file) is used as-is, never guarded.

    `guard=True` is epic_sync's opt-in: honour `OPS_EPIC_SEEN_STATE`, and, like
    merge_gate.state_dir()/verdicts.verdict_dir(), under AGENT_OPS_NO_SEND with no override
    refuse `default` rather than touch it. Every other caller (all keyed on NOTIFY_SEEN) passes
    guard=False.
    """
    if path is not None:
        return Path(path)
    if guard:
        override = os.environ.get("OPS_EPIC_SEEN_STATE")
        if not override and os.environ.get("AGENT_OPS_NO_SEND"):
            raise RuntimeError("refusing to touch the default epic-sync state under test: "
                                "set OPS_EPIC_SEEN_STATE explicitly")
        return Path(override) if override else default
    return default


def _load_seen(path=None, *, default=NOTIFY_SEEN, guard=False):
    try:
        return json.loads(_resolve_seen_path(path, default, guard=guard).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_seen(seen, path=None, *, default=NOTIFY_SEEN, guard=False):
    p = _resolve_seen_path(path, default, guard=guard)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(p, json.dumps(seen, indent=0, sort_keys=True))


def _card_title(line):
    m = re.search(r"\*\*(.+?)\*\*", line)
    return m.group(1) if m else line.strip()[:80]


def _default_sender(message):
    """Queue one message on the Capsule outbox (the Slack sender delivers it). Returns True when
    queued, False on any failure; never raises, so an outage degrades to "retry next tick"."""
    events.log("message", "bridge", message, payload={"target": "outbox"})
    if os.environ.get("AGENT_OPS_NO_SEND"):  # unit tests: never reach the chat (2026-09-06 leak)
        return True
    return outbox_send(message, "blocked")  # pushing class: running is log-only


def _log_sender(message):
    """Record one lifecycle line as an event and send it nowhere (no log channel exists since the
    Telegram log topic retired). Returns True: nothing failed, so the caller's `seen` key is
    still recorded."""
    events.log("message", "bridge", message, payload={"target": "log:none"})
    return True


def lifecycle_sender(sender=None):
    """The sender the three lifecycle kinds use. With the flag off this is
    exactly today's expression, `sender or _default_sender`."""
    import messages
    if messages.enabled():
        return _log_sender
    return sender or _default_sender


def actionable_send(sender, message, *, board_name=None, task_id=None, what=""):
    """Send one ACTIONABLE message under the day's budget (<=3). Over budget the item is
    DEFERRED into tomorrow's 08:30 brief and the card waits, so nothing is dropped. With
    OPS_BRIEF_V2 off this is `send(message)` and nothing else.

    Fail-open: a failure computing or recording the budget never blocks or raises into the send
    path (messages.record/defer_actionable can raise under AGENT_OPS_NO_SEND on an unoverridden
    default state path), and a sent message is never lost because the bookkeeping stumbled.
    Every fail-open except prints one stderr line (no secret values), so a permanently broken
    budget does not look like a working one.

    `messages.defer_actionable` dedupes by item for the day, so a card still over budget on the
    next tick is deferred at most once; `events.log` below is skipped on repeat ticks too."""
    import messages
    send = sender or _default_sender
    if not messages.enabled():
        return bool(send(message))
    try:
        ok, why = messages.allow("actionable")
    except Exception as exc:
        print(f"actionable_send: messages.allow failed, defaulting to allow: {exc}",
              file=sys.stderr)
        ok, why = True, "budget unavailable"
    if not ok:
        is_new = True
        try:
            fallback = (message.splitlines() or [""])[0][:120]  # M-4: never IndexError on ""
            _, is_new = messages.defer_actionable(what or fallback, board=board_name,
                                                  task_id=task_id)
        except Exception as exc:
            print(f"actionable_send: messages.defer_actionable failed: {exc}", file=sys.stderr)
        if is_new:
            events.log("message", "bridge", f"actionable deferred ({why}): {what}",
                       board=board_name, task_id=task_id, severity="info",
                       payload={"target": "deferred"})
        return False
    if not send(message):
        return False
    try:
        messages.record("actionable")
    except Exception as exc:
        print(f"actionable_send: messages.record failed (message was sent): {exc}",
              file=sys.stderr)
    return True


def _docker_ok():
    try:
        return events.docker_run(["info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def record_health(docker_ok=_docker_ok):
    """One `health` event per sync tick: the dead-man signal `bridge-deadman` alerts on, and the
    freshness `disk-high` assumes when it reads this row's latest values. The
    other dead-man rule, `capsule-tick-deadman` (`kind='tick_health'`, `Tick._heartbeat`),
    watches the Capsule tick, a separate process. `bridge-deadman` retires with the bridge, in
    the Lookout migration's datasource-repoint step.

    `docker_ok` is the injection point for tests.
    """
    disk = shutil.disk_usage("/")
    disk_pct = round(100 * (1 - disk.free / disk.total), 1)
    return events.log("health", "bridge", "sync tick",
                      payload={"tick": True, "docker": docker_ok(), "disk_pct": disk_pct})


STATE_DIR = events.state_root()


def extract_task_id(stdout):
    """No production caller: `Board.create` folds the id extraction in (and raises
    LegacyCliError instead of ValueError). Kept as the record of the shape `kanban create --json`
    returns, and for test_board_bridge.py:151-158."""
    data = json.loads(stdout)
    tid = data.get("id") or data.get("task_id")
    if not tid:
        raise ValueError(f"no task id in kanban output: {stdout!r}")
    return str(tid)


def _ensure_board(repo, runner=None):
    board.ensure_board(repo, runner=runner)


def _decision_log_commit_time(git_runner=None):
    """`Decisions/decisions.jsonl`'s own last-commit time, ISO-8601, or `None` when it cannot be
    had (no git, no history yet, a raising runner). It uses `vault_git`'s runner seam directly,
    not `vault_git.pull`/`push`: this is a read and must never touch the network.

    It carries `vault_git.pull`'s refusal: under `AGENT_OPS_NO_SEND` with no runner injected it
    returns `None` rather than shell out to git against the real `VAULT`, and the card renders
    the row date instead. A test that wants the lookup passes `git_runner=`."""
    import vault_git
    if (vault_git.no_send() and git_runner is None) or VAULT is None:
        return None
    try:
        res = vault_git._git(VAULT, ["log", "-1", "--format=%cI", "--",
                                     "Decisions/decisions.jsonl"], git_runner)
    except Exception:
        return None
    if getattr(res, "returncode", 1) != 0:
        return None
    out = (getattr(res, "stdout", "") or "").strip()
    return out or None


def dispatch(slug, specs_dir=SPECS_DIR, runner=None, git_runner=None):
    """Create the impl (+ verify, finalize) cards for `slug`'s approved spec. `source_hash`
    is keyed on the slug (`spec:<slug>`), keeping the `source: obsidian:<hash>` meta line stable
    per slug so a re-dispatch is recognisable.

    `git_runner` is the seam for the decision log's own commit time: it goes to
    `vault_git._git`, not to `Board`/the old CLI. It is computed once and reused by every card this
    dispatch builds. Any failure degrades to `None`, which every push_block call treats as
    "fall back to the row's own date"."""
    _check_slug(slug)
    spec = parse_spec(slug, specs_dir)
    try:
        _ensure_board(spec["repo"], runner)
    except Exception:
        pass
    # Every create goes through the Board, so `Board._env()` pins LEGACY_KANBAN_DB to this
    # repo's board and drops the three worker-ownership pins. `Board.create` also folds in the
    # id extraction.
    b = _board(spec["repo"], runner)
    source = source_hash(f"spec:{slug}")

    def _created(tid, role, parent_id=None):
        b.append_ledger(tid, ledger.make_entry(
            spec["repo"], tid, "created", source="bridge",
            summary=spec.get("title") or slug, dedupe=f"dispatch:{slug}:{role}",
            payload={"spec_slug": slug, "role": role, "parent_id": parent_id}))

    if spec.get("kind") == "research":
        impl_id = b.create(build_research_card(spec, slug, source,
                                               spec_file_sha256(slug, specs_dir)))
        _created(impl_id, "researcher")
        fin_id = None
    else:
        s_sha = spec_file_sha256(slug, specs_dir)
        # The sidecar is read once per dispatch and degrades to "no rows pushed" when absent or
        # unreadable: a missing nightly regeneration must never wedge a dispatch.
        try:
            sidecar = decision_log.load_sidecar(VAULT)
        except Exception:
            sidecar = {}
        modified = _decision_log_commit_time(git_runner)
        # The retrieval verdict is computed while building each card's body but can only be
        # measured against a real task_id, which `b.create` returns: hence the out-param.
        impl_retrieval = {}
        impl_id = b.create(build_impl_card(spec, slug, source, s_sha, sidecar=sidecar,
                                           retrieval_meta_out=impl_retrieval, modified=modified))
        _created(impl_id, "impl")
        _log_retrieval_push(spec["repo"], impl_id, impl_retrieval)
        verify_retrieval = {}
        verify_id = b.create(build_verify_card(spec, slug, impl_id, source, s_sha,
                                               retrieval_meta_out=verify_retrieval))
        _created(verify_id, "verify", impl_id)
        _log_retrieval_push(spec["repo"], verify_id, verify_retrieval)
        fin_id = b.create(build_finalize_card(spec, slug, verify_id))
        _created(fin_id, "finalize", verify_id)
    return impl_id, fin_id


def main(argv=None):
    p = argparse.ArgumentParser(prog="board_bridge.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dispatch")
    d.add_argument("slug")
    sub.add_parser("sync")
    el = sub.add_parser("eligible", help="list eligible Backlog cards; exit 1 if none")
    el.add_argument("--count", action="store_true")
    ls = sub.add_parser("link-spec", help="record a card's spec slug (the planner's one write)")
    ls.add_argument("board"); ls.add_argument("task_id"); ls.add_argument("slug")
    sub.add_parser("boards", help="the board names the Intake grammar accepts (for prompt rendering)")
    args = p.parse_args(argv)
    if args.cmd == "boards":
        # run.sh renders this into maintenance/security.md's __BOARDS__ so the
        # prompt cannot drift from the boards that actually exist (ruling 1). Capsule's boards,
        # plus legacy only when present; empty stays a loud refusal.
        print(" ".join(board.intake_board_names()))
        sys.exit(0)
    if args.cmd == "eligible":
        cards = eligible_cards()
        if args.count:          # script mode: always exit 0, the number is the answer
            print(len(cards))
            sys.exit(0)
        for c in cards:
            print(f"{c.board} {c.id} {c.title}")
        sys.exit(0 if cards else 1)
    if args.cmd == "link-spec":
        link_spec(args.board, args.task_id, args.slug)
        print(f"linked {args.board}/{args.task_id} -> Specs/{args.slug}.md")
        sys.exit(0)
    if args.cmd == "dispatch":
        impl_id, fin_id = dispatch(args.slug)
        print(f"dispatched {args.slug} -> impl {impl_id}, finalize {fin_id}")
    elif args.cmd == "sync":
        # Each step is independent: a failure in one must not starve the others. legacy removal
        # slice 3: the legacy-board steps (approved/done/notify/epic/routeback, board_view,
        # decisions) are gone from this tick; intake stays for its phone `approve spec:` line and
        # error notice, record_health for the bridge-deadman / disk-high health row.
        try:
            import intake
            r = intake.consume()
            if r["skipped"]:
                print(f"  intake: skipped ({r['skipped']})")
            elif r["consumed"] or r["errors"]:
                print(f"  intake: {len(r['consumed'])} consumed, {len(r['errors'])} error(s)")
        except Exception as exc:
            print(f"sync: intake failed: {exc}", file=sys.stderr)
        try:
            import usage_guard
            usage_guard.run()
        except Exception as exc:
            print(f"sync: usage_guard failed: {exc}", file=sys.stderr)
        try:
            record_health()
        except Exception as exc:
            print(f"sync: record_health failed: {exc}", file=sys.stderr)
        try:
            import ai_usage
            ai_usage.sample()  # hourly-stamped: a no-op on most ticks
        except Exception as exc:
            print(f"sync: ai_usage failed: {exc}", file=sys.stderr)
        try:
            import vault_stats
            vault_stats.sample()
        except Exception as exc:
            print(f"sync: vault_stats failed: {exc}", file=sys.stderr)
        try:
            import docs_sync
            docs_sync.run()
        except Exception as exc:
            print(f"sync: docs_sync failed: {exc}", file=sys.stderr)
        try:
            n = ledger.pending()
            if ledger.push_pending():
                print(f"  ledger pushed: {n} entries")
        except Exception as exc:
            print(f"sync: ledger push failed: {exc}", file=sys.stderr)
        print(f"  events shipped: {events.ship()}")


if __name__ == "__main__":
    main()
