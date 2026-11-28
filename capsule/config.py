"""Board config: routing table, caps, re-read per tick (spec §6.5, row 377)."""
import copy
import datetime
import json
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from capsule import rhythm, shards, slack

CAPS_KEYS = ("max_running", "claim_ttl", "failure_limit", "checkpoint_chain", "tick_seconds")
# Optional wall-clock caps per run, in seconds.
CAP_DEFAULTS = {"max_runtime_seconds": 3600, "review_seconds": 1800,
                "max_review": 2, "max_heavy": 3,
                "external_heavy": 0}   # heavy seats an outside chat uses
ROUTING_ROLES = ("implementer", "reviewer", "validator", "guide", "blind-tester")
# R30: `blind-tester` is routed like every role, but its entry is optional so every existing
# board config stays valid: without it the tick dispatches no tester and says so.
BLIND_TESTER = "blind-tester"
ROUTE_FIELDS = ("provider", "account", "model", "turn_budget")
# row 128: the optional route key `vault_read` (missing = none) says which vault notes dispatch
# may push into a role's brief. Every value arrives by dispatch push; no role mounts the vault.
# Not the seatbelt's `vault_read` (seatbelt.render: extra read-only filesystem paths).
# `notes` = D33's top work notes.
VAULT_READ = ("none", "decisions", "notes")
# row 456: Korin runs only when routed AND `korin.enabled`; M155: `implementer_restart` is the
# Opus restart route (`restart.py`), required once `redesign.escalation` is on
OPTIONAL_ROUTING_ROLES = ("korin", "implementer_restart", "security", "brainstorm")
KORIN_KEYS = {"enabled": bool, "vault": str, "main_checkout": str, "decisions_note": str, "test_timeout": int,
              "max_running": int, "vault_push": bool, "fix_epic": str, "fix_assignee": str,
              "watch_days": int, "since": str,   # since: ISO date (UTC)
              "blind_tests": str}   # path of the private blind tests repo (D161)
AUTONOMY_DEFAULTS = {"auto_amend": False, "amend_targets": ["card"], "amend_max_lines": 6, "touch_cards": False,
                     "touch_board": "capsule-corp", "touch_epic": None, "touch_assignee": None}
AMEND_TARGETS = ("card", "spec_section")
REDESIGN_FLAGS = ("deps_ready", "lanes", "check_gate", "escalation", "flake_reruns",
                  "night_runner", "interrupts", "memory_rules", "advisor_cards_only",
                  "light_runner",   # ENG-12: the seatbelt light tier (rows 373, 378)
                  "mac_runner",     # D305: the Mac lane (Xcode builds under seatbelt)
                  "security_review")   # BLD-02: the fourth blocking review, needs routing.security
# `image_loop` is not a switch: "record" (the default, and the only value) runs the image built
# here, as recorded in $CAPSULE_HOME/images.lock (runners/docker.py worker_image).
IMAGE_RECORD = "record"
REDESIGN_MODES = {"allocator": ("off", "report", "live"),
                  "vault_context": ("off", "report", "live"),
                  "blind_tests": ("off", "report", "gate")}
REDESIGN_DEFAULTS = {**{k: False for k in REDESIGN_FLAGS}, **{k: "off" for k in REDESIGN_MODES},
                     "image_loop": IMAGE_RECORD}
# Blind spec 2 / R13: the board-side `blind_tests` block (not capsule.toml: the implementer can
# edit that file and must not control the blind adapter). `redesign.blind_tests` stays the mode.
BLIND_ADAPTERS = ("unittest", "pytest", "node-test")
BLIND_KEYS = {"repo": str, "adapter": str, "tester_may_read": list, "interface_commands": list,
              "also_walled": list}     # also_walled: extra paths the host wall hides (host_wall.py)
MAX_LINES_CEILING = 12   # spec 2.6: the bound `autonomy.amend_max_lines` validates against
# M189: no default zone; the example leaves it "" to force a choice (host clock: Asia/Bangkok, +07)
QUIET_DEFAULTS = {"enabled": False, "start": "22:00", "end": "08:30", "timezone": None}
SYSTEM_ZONE = "system"   # the one timezone value that follows the OS zone (outbox._quiet_zone)
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# the stall supervisor (capsule/stall.py); `silent_*`, `rulings_per_day` and `disk_floor_gb`
# are read by the later units. Seconds unless a name says otherwise.
STALL_DEFAULTS = {"grace_s": 300, "repage_s": 1800, "slack_repage_s": 7200, "l2_deadline_s": 1200,
                  "disk_floor_gb": 15, "disk_pause_gb": 8, "silent_s": 1200, "silent_kill": True,
                  "rulings_per_day": 6}


# `unattended` (stall-supervisor-unattended 9.2.4): continues are capped per board per 24 h; the
# per-card cap (2) is in capsule/stall_continue.py. `review_retries`: the failed contract jobs and
# review retries a card may have since its last implementer run before it is held `review_infra`.
# The owner-wait keys (capsule/stall_owner.py;
# D339): decide alone after 1 h (also 1 h when nothing can run), at most 10 times per board per
# week, an unseen first screen passes after 24 h (`None`: never), an idle lane may start 3 Korin
# fix cards a day, a stall older than 24 h pages each 6 h (0: no slow rule).
UNATTENDED_DEFAULTS = {"continues_per_day": 30, "decide_after_s": 3600, "decide_idle_s": 3600,
                       "first_screen_s": 86400, "decisions_per_week": 10,
                       "fix_release_per_day": 0, "slow_repage_s": 21600,
                       "irreversible_paths": [], "review_retries": 3,
                       "deadman_url": None}


# `patrol` (stall-supervisor-unattended 9.4, capsule/patrol.py). `agent_cmd` is an argv template
# (a list or one string; `{model}`, `{packet_file}`, `{role_file}`, `{max_turns}` are filled) or
# null for "no model" mode. Seconds unless a name says otherwise.
PATROL_DEFAULTS = {
    "agent_cmd": ["claude", "-p", "--append-system-prompt-file", "{role_file}",
                  "--model", "{model}", "--tools", "Read,Grep,Glob", "--max-budget-usd", "1",
                  "Read the packet file {packet_file}; answer with one patrol.v1 JSON document."],
    "model": "sonnet", "strong_model": "opus", "calls_per_day": 8, "strong_per_day": 2,
    "min_age_s": 1800, "timeout_s": 600}


def patrol(cfg):
    """The optional `patrol` block with defaults (every key optional)."""
    return {**PATROL_DEFAULTS, **((cfg or {}).get("patrol") or {})}


# `diligence` (the ask gate, capsule/ask_gate.py; design 2026-11-23 section 8). `mode`: `shadow`
# computes and logs a verdict and changes nothing. Unit 1 reads mode, answer_ttl_days,
# repeat_limit, repeat_days and owner_topics; the other keys are the panel's (unit 2 and later).
DILIGENCE_MODES = ("off", "shadow", "on")
DILIGENCE_DEFAULTS = {
    "mode": "shadow",
    "seats": [{"seat": "precedent", "model": "sonnet"}, {"seat": "engineering", "model": "sonnet"}],
    "calls_per_day": 12, "timeout_s": 600, "apply_wait_s": 0, "answer_ttl_days": 30,
    "repeat_limit": 2, "repeat_days": 30, "panel_down_s": 21600, "asks_per_day": 8,
    "veto_trip": 2,
    "owner_topics": ["price", "paid", "payment", "license", "licence", "publish", "public release",
                     "app store", "secret", "delete user data", "new service", "rename", "brand"]}


def diligence(cfg):
    """The optional `diligence` block with defaults (every key optional)."""
    return {**DILIGENCE_DEFAULTS, **((cfg or {}).get("diligence") or {})}


def _validate_diligence(block):
    """Refuse a bad `diligence` block; `{}` (or no block) is valid and means the defaults."""
    if not isinstance(block, dict):
        raise ValueError("diligence: not an object")
    for key, val in block.items():
        want = DILIGENCE_DEFAULTS.get(key)
        if want is None:
            raise ValueError(f"diligence.{key}: unknown key")
        if key == "mode":
            ok = val in DILIGENCE_MODES
        elif key == "owner_topics":
            ok = isinstance(val, list) and all(isinstance(v, str) and v.strip() for v in val)
        elif key == "seats":
            ok = isinstance(val, list) and all(
                isinstance(v, dict) and set(v) == {"seat", "model"}
                and all(isinstance(x, str) and x for x in v.values()) for v in val)
        else:
            ok = isinstance(val, int) and not isinstance(val, bool) and val >= 0
        if not ok:
            raise ValueError(f"diligence.{key}: bad value (mode: one of {DILIGENCE_MODES};"
                             " owner_topics: a list of words; seats: a list of"
                             " {seat, model}; the rest: an int >= 0)")


def _validate_patrol(block):
    """Refuse a bad `patrol` block; `{}` (or no block) is valid and means the defaults."""
    if not isinstance(block, dict):
        raise ValueError("patrol: not an object")
    for key, val in block.items():
        if key not in PATROL_DEFAULTS:
            raise ValueError(f"patrol.{key}: unknown key")
        if key == "agent_cmd":
            if not (val is None or (isinstance(val, str) and val.strip()) or (
                    isinstance(val, list) and val and all(isinstance(a, str) for a in val))):
                raise ValueError("patrol.agent_cmd: must be null, a string or a list of strings")
        elif key in ("model", "strong_model"):
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"patrol.{key}: must be a non-empty string")
        elif not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise ValueError(f"patrol.{key}: must be an int >= 0")


def autonomy(cfg):
    """The optional `autonomy` block (factory-autonomy spec 2.5) with defaults; every
    switch OFF."""
    return copy.deepcopy({**AUTONOMY_DEFAULTS, **((cfg or {}).get("autonomy") or {})})


def legacy_image_loop(val):
    """True for a value the retired on/off switch could hold (Ruling 3.1); it reads as record."""
    return isinstance(val, bool) or val in ("on", "off")


def redesign(cfg):
    """The optional `redesign` block (D17, unit R01): every redesign switch, all OFF; a missing
    block or key reads as off. The only place the switch table lives. A legacy `image_loop`
    value reads as record."""
    out = copy.deepcopy({**REDESIGN_DEFAULTS, **((cfg or {}).get("redesign") or {})})
    if legacy_image_loop(out["image_loop"]):
        out["image_loop"] = IMAGE_RECORD
    return out


def blind_tests(cfg):
    """The board's `blind_tests` block with defaults, or None when the board has none (no block =
    blind tests unavailable, never a silent pass). The only place its defaults live."""
    block = (cfg or {}).get("blind_tests")
    if not block:
        return None
    return copy.deepcopy({"tester_may_read": [], "interface_commands": [], **block})


def quiet_hours(cfg):
    """The optional `quiet_hours` block (M189) with defaults; off when absent."""
    return {**QUIET_DEFAULTS, **((cfg or {}).get("quiet_hours") or {})}


def stall(cfg):
    """The optional `stall` block with defaults (every key optional)."""
    return {**STALL_DEFAULTS, **((cfg or {}).get("stall") or {})}


def _validate_stall(block):
    """Refuse a bad `stall` block; `{}` (or no block) is valid and means the defaults."""
    if not isinstance(block, dict):
        raise ValueError("stall: not an object")
    for key, val in block.items():
        want = STALL_DEFAULTS.get(key)
        if want is None:
            raise ValueError(f"stall.{key}: unknown key")
        flag = isinstance(want, bool)
        ok = isinstance(val, bool) if flag else (
            isinstance(val, int) and not isinstance(val, bool) and val >= 0)
        if not ok:
            raise ValueError(f"stall.{key}: must be {'a bool' if flag else 'an int >= 0'}")


def unattended(cfg):
    """The optional `unattended` block with defaults (every key optional)."""
    return {**UNATTENDED_DEFAULTS, **((cfg or {}).get("unattended") or {})}


def _validate_unattended(block):
    """Refuse a bad `unattended` block; `{}` (or no block) is valid and means the defaults."""
    if not isinstance(block, dict):
        raise ValueError("unattended: not an object")
    for key, val in block.items():
        if key not in UNATTENDED_DEFAULTS:
            raise ValueError(f"unattended.{key}: unknown key")
        if key == "deadman_url":
            if val is not None and not (
                    isinstance(val, str) and val.startswith(("https://", "http://"))):
                raise ValueError("unattended.deadman_url: must be an http(s) URL or null")
        elif key == "irreversible_paths":
            if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
                raise ValueError("unattended.irreversible_paths: must be a list of strings")
        elif val is None and key == "first_screen_s":
            continue
        elif not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise ValueError(f"unattended.{key}: must be an int >= 0")


def _zone_known(name):
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        return False
    return True


def _validate_quiet(block):
    """Refuse a bad block; `{}` (or no block) is valid and means off."""
    if not isinstance(block, dict):
        raise ValueError("quiet_hours: not an object")
    extra = set(block) - set(QUIET_DEFAULTS)
    if extra:
        raise ValueError(f"quiet_hours.{sorted(extra)[0]}: unknown key")
    cur = quiet_hours({"quiet_hours": block})
    if not isinstance(cur["enabled"], bool):
        raise ValueError("quiet_hours.enabled: must be a bool")
    if not all(isinstance(cur[k], str) and HHMM.match(cur[k]) for k in ("start", "end")):
        raise ValueError("quiet_hours.start/end: must be HH:MM (24 h)")
    if cur["start"] == cur["end"]:
        raise ValueError("quiet_hours: start and end must differ")
    if cur["enabled"] and not cur["timezone"]:
        raise ValueError("quiet_hours.timezone: required when enabled")
    if cur["timezone"] not in (None, "", SYSTEM_ZONE) and not _zone_known(cur["timezone"]):
        raise ValueError("quiet_hours.timezone: not a known IANA zone")


def validate_lookout(block):
    """The optional `lookout` block: `publish` (bool, default off) lets `capsule publish` run."""
    if not isinstance(block, dict) or set(block) - {"ship", "publish"}:
        raise ValueError("lookout: an object with the keys ship and publish")
    if not isinstance(block.get("publish", False), bool):
        raise ValueError("lookout.publish: must be a bool")


def _validate_quiet_lookout(cfg):
    _validate_quiet(cfg.get("quiet_hours", {}))
    validate_lookout(cfg.get("lookout", {}))
    _validate_stall(cfg.get("stall", {}))
    _validate_unattended(cfg.get("unattended", {}))
    _validate_patrol(cfg.get("patrol", {}))
    _validate_diligence(cfg.get("diligence", {}))


def _validate_redesign(block, routing=None):
    if not isinstance(block, dict):
        raise ValueError("redesign: not an object")
    for key, val in block.items():
        if key not in REDESIGN_DEFAULTS:
            raise ValueError(f"redesign.{key}: unknown switch")
        if key in REDESIGN_FLAGS and not isinstance(val, bool):
            raise ValueError(f"redesign.{key}: must be a bool")
        if key == "image_loop" and val != IMAGE_RECORD and not legacy_image_loop(val):
            raise ValueError(f'redesign.image_loop: must be "{IMAGE_RECORD}"')
        if key in REDESIGN_MODES and val not in REDESIGN_MODES[key]:
            raise ValueError(f"redesign.{key}: one of {REDESIGN_MODES[key]}")
    if block.get("security_review") and routing is not None and "security" not in routing:
        raise ValueError("redesign.security_review needs routing.security")   # BLD-02
    if block.get("escalation") and routing is not None and "implementer_restart" not in routing:
        raise ValueError("redesign.escalation needs routing.implementer_restart")   # M155
    if block.get("escalation") and routing is not None:
        # `restart.apply` and `restart.step` tell the Opus runs from the Sonnet ones by model
        sonnet = (routing.get("implementer") or {}).get("model")
        if routing["implementer_restart"].get("model") == sonnet:
            raise ValueError("routing.implementer_restart.model must differ from"
                             " routing.implementer.model")


def validate_blind_tests(block):
    """Refuse a bad block. Shape only; whether the repo exists and sits outside every mount is
    `capsule.blind.preflight`'s question, asked per run."""
    if not isinstance(block, dict):
        raise ValueError("blind_tests: not an object")
    for key, val in block.items():
        want = BLIND_KEYS.get(key)
        if want is None:
            raise ValueError(f"blind_tests.{key}: unknown key")
        if not isinstance(val, want):
            kind = "a string" if want is str else "a list of strings"
            raise ValueError(f"blind_tests.{key}: must be {kind}")
        if want is list and not all(isinstance(x, str) and x for x in val):
            raise ValueError(f"blind_tests.{key}: must be a list of non-empty strings")
    if not block.get("repo"):
        raise ValueError("blind_tests.repo: missing")
    if block.get("adapter") not in BLIND_ADAPTERS:
        raise ValueError(f"blind_tests.adapter: one of {BLIND_ADAPTERS}")


BAD_REF = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//|^[-/]|[/.]$|\.lock(/|$)|(^|/)\.")


def validate_lander_branch(lander):
    """`lander.branch` (LAND-03): a valid git branch name, and `main` only as plain `main`."""
    branch = lander.get("branch", "main") if isinstance(lander, dict) else "main"
    if (not isinstance(branch, str) or not branch or branch in ("@", "HEAD")
            or BAD_REF.search(branch)
            or branch.startswith(("refs/", "heads/", "remotes/", "origin/"))):
        raise ValueError("lander.branch: must be a valid git branch name (not HEAD, no refs/, "
                         "heads/, remotes/ or origin/ prefix; main only as `main`)")


def _validate_git_identity(block):
    """`git_identity` (t_734ff813): optional `{name, email}` strings, the operator's commit
    identity inside a container; absent keeps the image's neutral worker."""
    if block is None:
        return
    if (not isinstance(block, dict) or set(block) != {"name", "email"}
            or not all(isinstance(v, str) and v for v in block.values())):
        raise ValueError("git_identity: must be {name, email}, both non-empty strings")


def _validate_runaway(block):
    """The optional stop-the-line limits (runaway.DEFAULTS); 0 turns same_class_rounds off."""
    from capsule import runaway
    if not isinstance(block, dict) or set(block) - set(runaway.DEFAULTS):
        raise ValueError(f"runaway: an object with keys from {sorted(runaway.DEFAULTS)}")
    for key, val in block.items():
        lowest = 0 if key == "same_class_rounds" else 1
        factor = key.endswith("_factor")
        kinds = (int, float) if factor else int
        if isinstance(val, bool) or not isinstance(val, kinds) or val < lowest:
            what = "number" if factor else "whole number"
            raise ValueError(f"runaway.{key}: must be a {what} >= {lowest}")
    ks = runaway.settings({"runaway": block})
    if ks["runs_warn_factor"] >= ks["runs_factor"]:
        raise ValueError("runaway.runs_warn_factor: must be below runs_factor")


def _validate_autonomy(block):
    if not isinstance(block, dict):
        raise ValueError("autonomy: not an object")
    for key, val in block.items():
        if key not in AUTONOMY_DEFAULTS:
            raise ValueError(f"autonomy.{key}: unknown key")
        if key in ("auto_amend", "touch_cards") and not isinstance(val, bool):
            raise ValueError(f"autonomy.{key}: must be a bool")
        if key == "amend_max_lines" and (not isinstance(val, int) or isinstance(val, bool)
                                          or not 1 <= val <= MAX_LINES_CEILING):
            raise ValueError(f"autonomy.amend_max_lines: an integer 1-{MAX_LINES_CEILING}")
        if key == "amend_targets" and (not isinstance(val, list) or not val or not set(val) <= set(AMEND_TARGETS)):
            raise ValueError(f"autonomy.amend_targets: a non-empty subset of {AMEND_TARGETS}")
        if key == "touch_board" and (not isinstance(val, str) or not val):
            raise ValueError("autonomy.touch_board: a board name")
        if key in ("touch_epic", "touch_assignee") and val is not None and not isinstance(val, str):
            raise ValueError(f"autonomy.{key}: a string or null")


def validate_assignees(assignees):
    if not isinstance(assignees, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in assignees.items()):
        raise ValueError("assignees: must map assignee names to role strings")
    if "image-builder" in assignees:       # M137: that name is the tick's own image step, no model
        raise ValueError("assignees: image-builder is reserved for the image-request step")


def _validate_cap_defaults(caps):
    """The optional caps are whole numbers: positive, except `external_heavy` (0 or more)."""
    for key in CAP_DEFAULTS:
        val = caps.get(key, CAP_DEFAULTS[key])
        low = 0 if key == "external_heavy" else 1
        if not isinstance(val, int) or isinstance(val, bool) or val < low:
            kind = "a non-negative" if low == 0 else "a positive"
            raise ValueError(f"caps.{key}: not {kind} integer")


def _validate_routing(routing):
    required = tuple(r for r in ROUTING_ROLES if r != BLIND_TESTER)
    optional = (*OPTIONAL_ROUTING_ROLES, BLIND_TESTER)
    for role in required + tuple(r for r in optional if r in routing):
        entry = routing.get(role)
        if not isinstance(entry, dict):
            raise ValueError(f"routing.{role}: missing")
        for field in ROUTE_FIELDS:
            val = entry.get(field)
            if val is None or (field != "turn_budget" and not isinstance(val, str)):
                raise ValueError(f"routing.{role}.{field}: missing")
            if field == "turn_budget" and (not isinstance(val, int) or isinstance(val, bool)):
                raise ValueError(f"routing.{role}.{field}: missing")
    for role, entry in routing.items():   # every object entry, listed role or not
        if isinstance(entry, dict) and "reports" in entry:
            if (entry.get("adapter") or entry.get("provider")) != "stub":
                raise ValueError(f"routing.{role}.reports: only allowed with \"adapter\": \"stub\"")
            if not isinstance(entry["reports"], list):
                raise ValueError(f"routing.{role}.reports: must be a list")
        if isinstance(entry, dict) and entry.get("vault_read", "none") not in VAULT_READ:
            raise ValueError(f"routing.{role}.vault_read: one of {VAULT_READ}")


def validate(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("config: not an object")
    if not isinstance(cfg.get("board"), str) or not cfg["board"]:
        raise ValueError("board: missing")
    if not isinstance(cfg.get("epics"), list) or not cfg["epics"]:
        raise ValueError("epics: missing")
    for i, epic in enumerate(cfg["epics"]):
        if isinstance(epic, dict):
            screens = epic.get("screens", [])
            if (not isinstance(epic.get("branch"), str) or not epic["branch"]
                    or not isinstance(epic.get("ui", False), bool)
                    or not isinstance(screens, list) or not all(isinstance(g, str) for g in screens)):
                raise ValueError(f"epics[{i}]: needs a branch, an optional ui bool and screens list of globs")
            if "spec" in epic and not isinstance(epic["spec"], str):
                raise ValueError("epics[].spec: must be a string path")
            if "spec_section" in epic and not isinstance(epic["spec_section"], str):
                raise ValueError("epics[].spec_section: must be a string heading")
            if "rows" in epic and (not isinstance(epic["rows"], list)
                                   or not all(isinstance(r, int) and not isinstance(r, bool) for r in epic["rows"])):
                raise ValueError("epics[].rows: must be a list of ints")
        elif not isinstance(epic, str) or not epic:
            raise ValueError(f"epics[{i}]: not a branch name")

    caps = cfg.get("caps")
    if not isinstance(caps, dict):
        raise ValueError("caps: missing")
    for key in CAPS_KEYS:
        val = caps.get(key)
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            raise ValueError(f"caps.{key}: missing")
    _validate_cap_defaults(caps)
    shards.validate(cfg)

    routing = cfg.get("routing")
    if not isinstance(routing, dict):
        raise ValueError("routing: missing")
    _validate_routing(routing)

    validate_assignees(cfg.get("assignees", {}))
    if "korin" in cfg:
        _validate_korin_block(cfg["korin"], routing)
    _validate_runaway(cfg.get("runaway", {}))
    # factory-autonomy spec 2.5: optional, every switch off
    _validate_autonomy(cfg.get("autonomy", {}))
    _validate_redesign(cfg.get("redesign", {}), routing)   # D17: every switch off by default
    _validate_quiet_lookout(cfg)    # M189 quiet_hours, LAND-10 lookout: optional, off by default
    rhythm.parse(cfg.get("rhythm"))               # COM-11: optional, off unless enabled
    if "blind_tests" in cfg:                      # R13: optional; absent = blind tests unavailable
        validate_blind_tests(cfg["blind_tests"])
    validate_lander_branch(cfg.get("lander"))     # LAND-03: optional, default main
    for key, dflt in (("deadman", True), ("design_briefs", False)):   # Task 11's dead-man; TEAM-23
        if not isinstance(cfg.get(key, dflt), bool):
            raise ValueError(f"{key}: must be a bool")
    if type(cfg.get("docker_down_s", 1)) is not int or cfg.get("docker_down_s", 1) < 1:  # no bool
        raise ValueError("docker_down_s: must be a positive integer (seconds)")
    _validate_git_identity(cfg.get("git_identity"))   # t_734ff813: optional, neutral if absent
    if "slack" in cfg:                       # rows 398, 425: class -> channel, optional
        slack.validate(cfg["slack"])
    return cfg


def _validate_korin_block(block, routing):
    """row 456: the incident loop, optional, off by default."""
    if not isinstance(block, dict):
        raise ValueError("korin: not an object")
    for key, val in block.items():
        want = KORIN_KEYS.get(key)
        if want is None:
            raise ValueError(f"korin.{key}: unknown key")
        if not isinstance(val, want) or (want is int and (isinstance(val, bool) or val <= 0)):
            raise ValueError(f"korin.{key}: must be {'a positive integer' if want is int else want.__name__}")
    if "since" in block:
        try:
            datetime.date.fromisoformat(block["since"])
        except ValueError:
            raise ValueError("korin.since: must be an ISO date (YYYY-MM-DD)") from None
    if block.get("enabled") and "korin" not in routing:
        raise ValueError("korin.enabled needs routing.korin")
    if block.get("enabled") and not block.get("since"):
        raise ValueError("korin.since is required when korin.enabled is true (an ISO date,"
                         " YYYY-MM-DD; earlier incidents are backlog, never dispatched)")


FAMILIES = ("haiku", "sonnet", "opus", "fable")
JUDGEMENT_FAMILIES = ("opus", "fable")   # Opus-or-stronger may run the judgement seats


def family(model):
    hits = [f for f in FAMILIES if f in (model or "")]
    return hits[0] if len(hits) == 1 else None


def policy_violations(cfg, known_only=False):
    """M150 pin: every way `cfg["routing"]` breaks it (opus-or-stronger judges, sonnet
    implements), by model family, as short strings. `known_only` skips a model whose family
    cannot be classified (a fake name in a test fixture): `load` warns that way, while
    `python3 -m capsule.config <config.json>` (main, below, for `deploy.sh --check`) reports all."""
    routing, bad = cfg["routing"], []

    def off(model, allowed):
        fam = family(model)
        return fam not in allowed and not (known_only and fam is None)

    for role in ("guide", "reviewer", "korin"):
        if role in routing and off(routing[role]["model"], JUDGEMENT_FAMILIES):
            bad.append(f"{role} must be opus or stronger, got {routing[role]['model']}")
    if off(routing["implementer"]["model"], ("sonnet",)):
        bad.append(f"implementer must be sonnet, got {routing['implementer']['model']}")
    val = routing["validator"]
    if off(val["model"], ("haiku", "sonnet")):
        bad.append(f"validator must be haiku or sonnet, got {val['model']}")
    elif family(val["model"]) == "haiku" and family(val.get("fallback")) != "sonnet":
        bad.append(f"a haiku validator needs a sonnet fallback, got {val.get('fallback')}")
    return bad


def load(path):
    cfg = json.loads(Path(path).read_text())
    validate(cfg)
    for line in policy_violations(cfg, known_only=True):
        print(f"capsule: routing off the M150 pin: {line}", file=sys.stderr)
    epics = cfg["epics"]
    cfg["epics"] = epic_branches_from_list(epics)
    cfg["ui_epics"] = {e["branch"]: list(e.get("screens", [])) for e in epics if isinstance(e, dict) and e.get("ui")}
    # row 364 guide citations survive the normalisation
    cfg["epic_entries"] = [e for e in epics if isinstance(e, dict)]
    return cfg


def epic_branches_from_list(epics):
    """Branch names from an `epics[]`-shaped list, tolerantly: a non-empty string entry
    as itself; an object entry's own `branch` field, only when that is itself a non-
    empty string; anything else (a malformed entry, or `epics` not a list at all)
    simply skipped, never raised."""
    if not isinstance(epics, list):
        return []
    out = []
    for e in epics:
        if isinstance(e, str) and e:
            out.append(e)
        elif isinstance(e, dict) and isinstance(e.get("branch"), str) and e["branch"]:
            out.append(e["branch"])
    return out


def epic_branches(cfg):
    """`epics[]` entries as plain branch-name strings, whichever form each was given in."""
    return epic_branches_from_list(cfg["epics"])


def epic_for_branch(cfg, branch):
    """The `epics[]` object entry for `branch` (its `spec`/`spec_section`/`rows` citations, row 364
    guide fallback), or `None` when the epic is a plain string or carries no such entry."""
    for e in cfg.get("epic_entries") or cfg["epics"]:
        if isinstance(e, dict) and e.get("branch") == branch:
            return e
    return None


def lane_meta(cfg, branch, **extra):
    """Run metadata: `extra` plus `lane` and `batch_id` for a card on `branch`. An epic card is
    lane `epic` and batches by its branch; any other card (a null branch too) is lane `card`."""
    epic = branch in epic_branches(cfg)
    return {**extra, "lane": "epic" if epic else "card", "batch_id": branch if epic else None}


def run_meta(cfg, branch, **extra):
    """`lane_meta` as the JSON text a run row's metadata column holds."""
    return json.dumps(lane_meta(cfg, branch, **extra))


def cap(cfg, key):
    """An optional cap (CAP_DEFAULTS) from the config, or its default."""
    return cfg["caps"].get(key, CAP_DEFAULTS[key])


def route(cfg, role):
    entry = cfg["routing"][role]
    return {
        "provider": entry["provider"],
        "account": entry["account"],
        "model": entry["model"],
        "fallback": entry.get("fallback"),
        "turn_budget": entry["turn_budget"],
        "adapter": entry.get("adapter", entry["provider"]),
        "reasoning_effort": entry.get("reasoning_effort"),
        **({"reports": entry["reports"]} if "reports" in entry else {}),
    }


class Loader:
    """Re-reads `path` on every `.get()`; a broken file keeps the last good config."""

    def __init__(self, path):
        self.path = Path(path)
        self._cfg = None
        self._last_err_mtime = object()  # never equals a real mtime or None

    def get(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError as exc:
            return self._report(None, exc)
        try:
            cfg = load(self.path)
        except Exception as exc:
            return self._report(mtime, exc)
        self._cfg = cfg
        return self._cfg, None

    def _report(self, mtime, exc):
        if mtime == self._last_err_mtime:
            return self._cfg, None
        self._last_err_mtime = mtime
        return self._cfg, str(exc)


def main(argv):
    """One line per M150 violation in the config at argv[0] (raw JSON, unvalidated); rc 1 if any
    or it cannot be read."""
    try:
        bad = policy_violations(json.loads(Path(argv[0]).read_text()))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        bad = [f"routing unreadable ({type(e).__name__}: {e})"]
    print("\n".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
