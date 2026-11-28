"""`python3 -m capsule publish [--capsule-home H]` (LAND-10; Lookout spec D4 section 5.7, row 478).

Puts each epic on GitHub as one draft pull request with one comment per card; merging stays
the owner's. Off unless the capsule home's `config.json` has `{"lookout": {"publish": true}}`: when
off, it exits 0 and runs no gh and no git. Otherwise every `H/lookout/publish/<slug>.want.json`
is processed once (no retry loop, one handler per want) and `<slug>.result.json` is written
beside it (temp file, then rename). gh is `$CAPSULE_GH_BIN` (else `gh`), called through
`daily_pr._gh`. Nothing here merges, forces or pushes to main: the epic branch must start with
`epic/` and may not be main, master or the config's lander branch.

Want file fields: epic_branch (str), clone (path of the clone that has the epic checked out),
repo_url (push target), repo ("owner/name", optional when repo_url is a github.com url), spec
(epic spec path), rows (decision row ints), cards (list of {task_id, title, sha, validator,
reviewer, guide, review_rounds}; the outcome fields are optional and read "n/a"). A value that
starts with "-" is refused.

Result file fields: state ("published" or "refused"), reason (refused only: gh_auth,
repo_unknown, diverged, secret_found, push_failed, pr_failed, undo_failed, bad_want,
internal), detail (refused only: the failed command, never its output), pr_url, pr_number,
head (the pushed sha; a refusal keeps these once the PR is known), commented (card ids
commented this run).

Slice 2: gitleaks (`$CAPSULE_GITLEAKS_BIN`) scans the commits a push adds before it. A want with
`all_done: true` and `<slug>.review.md` beside it gets the review posted once (secrets and home
paths scrubbed), then `gh pr ready`; the result gains reviewed, reviewed_head, reviewed_sha256
and ready. Ready is bound to the head (M043), decided from GitHub: the review marker is
`capsule-review:<n>:<head12>:<sha256[:12] of review.md>`. A review.md whose hash is already on
the PR reviewed an older head: nothing is posted, the PR stays draft, `review_stale` is set.
Before any push, a PR that is not a draft and has no marker for the head about to be
pushed goes back to draft (`gh pr ready --undo`; failure refuses before the push
as `undo_failed` with `ready_unreviewed`). Every publish rewrites the PR body with the current
cards. `--poll` sets `ready_unreviewed` when a ready PR has no marker for its head.
`publish --poll` stores each unmerged PR's `gh pr view` under `pr` with `polled_at` (every
300 s), fetching main once after a merge; a failure adds `poll_error`. It never pushes,
comments, reviews or merges.

Exit codes: 0 done or off, 1 config or lock-file error, 75 (EX_TEMPFAIL) another publish or poll
holds the lock (try again later).
"""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from capsule import config, daily_pr, paths

sys.path.insert(0, str(paths.REPO / "maintenance"))
import scrub  # noqa: E402  (the one secret scrubber, as lookout_ship uses it)

REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")
BRANCH = re.compile(r"epic/[A-Za-z0-9._/-]+")
CARD_ID = re.compile(r"t_[0-9a-f]{8}")
SHA = re.compile(r"[0-9a-f]{7,40}")
PROTOCOLS = "file:https:ssh"      # no ext:: transport: it runs a command
HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s]+")
EXTRA_SECRETS = (       # shapes scrub.py lacks: (pattern, scrub.py-style marker)
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "<redacted:aws-key>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "<redacted:slack-token>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)?"),
     "<redacted:private-key>"),
)
VIEW = "number,url,state,isDraft,mergedAt,mergeCommit,statusCheckRollup,reviewDecision,headRefOid"
POLL_EVERY = 300


def _git(*args):
    env = {**os.environ, "GIT_ALLOW_PROTOCOL": PROTOCOLS}
    done = subprocess.run(["git", *args], capture_output=True, text=True, timeout=300, env=env)
    return done.returncode, done.stdout.strip()


def _fail(reason, detail, **more):
    return RuntimeError({"state": "refused", "reason": reason, "detail": detail, **more})


def _body(want):
    """The PR packet: spec, decision rows and the current card list."""
    sums = "\n".join(f"- [ ] {c['task_id']}: {c['title']}" for c in want["cards"])
    return (f"Spec: {want['spec']}\nDecision rows: {', '.join(map(str, want['rows']))}\n\n"
            f"Cards:\n{sums}\n")


def _existing(hub, branch):
    """The branch's open PR ({number, url}) or None."""
    rc, out = daily_pr._gh(["pr", "list", "--head", branch, "--json", "number,url"], hub)
    if rc:
        raise _fail("pr_failed", "gh pr list")
    found = json.loads(out or "[]")
    return found[0] if found else None


def _create(hub, branch, want):
    """(number, url) of a new draft PR."""
    rc, out = daily_pr._gh(["pr", "create", "--draft", "--base", "main", "--head", branch,
                            "--title", f"Epic {branch}", "--body-file", "-"], hub, _body(want))
    number = re.search(r"https://\S+/pull/(\d+)\s*$", out)
    if rc or not number:
        raise _fail("pr_failed", "gh pr create")
    return int(number.group(1)), number.group(0).strip()


REVIEWED = re.compile(r"<!-- capsule-review:(\d+):([0-9a-f]{12})(?::([0-9a-f]{12}))? -->")


# GitHub's rule: 1-39 alphanumerics or single hyphens, never leading or trailing.
LOGIN = re.compile(r"(?!.{40})[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


def _login():
    """Login of the authenticated gh user, the one author whose markers count; "" if unknown."""
    rc, out = daily_pr._gh(["api", "user", "--jq", ".login"], ".")
    return out.strip() if not rc and LOGIN.fullmatch(out.strip()) \
        and out.strip() != "null" else ""


def _heads(number, reviews):
    """{head12: review hash12 ("" when the marker has none)} that PR `number` carries, from
    reviews authored by the authenticated gh user only: a marker from anyone else is ignored.
    RuntimeError when markers exist but that user is unknown: never read as "no review"."""
    found = [(r, m) for r in reviews if isinstance(r, dict)
             for m in REVIEWED.finditer(str(r.get("body", ""))) if m.group(1) == str(number)]
    me = _login() if found else ""
    if found and not me:
        raise RuntimeError("gh api user")
    return {m.group(2): m.group(3) or "" for r, m in found
            if isinstance(r.get("author"), dict) and r["author"].get("login") == me}


def _seen(hub, number, doc=None, undo=False):
    """(is_draft, reviewed heads) of the PR as GitHub shows it now."""
    rc, out = daily_pr._gh(["pr", "view", str(number), "--json", "isDraft,reviews"], hub)
    try:
        found = json.loads(out or "null")
        if rc or not isinstance(found["reviews"], list):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise _fail("pr_failed", "gh pr view", **(doc or {})) from None
    try:
        return found.get("isDraft") is True, _heads(number, found["reviews"])
    except RuntimeError:
        if undo and found.get("isDraft") is not True:   # unknown reviewer: back to draft first
            if daily_pr._gh(["pr", "ready", "--undo", str(number)], hub)[0]:
                raise _fail("undo_failed", "gh pr ready --undo", ready_unreviewed=True,
                            **(doc or {})) from None
        raise _fail("gh_auth", "gh api user", **(doc or {})) from None


def _plain(value):
    """Card text safe in a comment: `<` and `>` become entities, so no `<!--` or `-->`."""
    return str(value).replace("<", "&lt;").replace(">", "&gt;")


def _marker(card):
    return f"<!-- capsule-card:{card['task_id']}:{card['sha']} -->"


def _comment(card):
    outcome = {k: _plain(card.get(k) or "n/a") for k in ("validator", "reviewer", "guide")}
    return (f"{_plain(card['task_id'])}: {_plain(card['title'])}\n\nsha {_plain(card['sha'][:8])}\n"
            f"validator: {outcome['validator']}\nreviewer: {outcome['reviewer']}\n"
            f"guide: {outcome['guide']}\nreview rounds: "
            f"{_plain(card.get('review_rounds', 'n/a'))}\n\n{_marker(card)}")


def _post_cards(hub, number, cards):
    """Comment each card whose marker is not yet on the PR; the ids commented."""
    rc, out = daily_pr._gh(["pr", "view", str(number), "--json", "comments"], hub)
    if rc:
        raise _fail("pr_failed", "gh pr view")
    doc = json.loads(out or "null")
    if not isinstance(doc, dict) or not isinstance(doc.get("comments"), list):
        raise _fail("pr_failed", "gh pr view")
    seen = {(str(c.get("body", "")).strip().splitlines() or [""])[-1].strip()
            for c in doc["comments"] if isinstance(c, dict)}    # the marker is the last line
    done = []
    for card in cards:
        if _marker(card) in seen:
            continue
        if daily_pr._gh(["pr", "comment", str(number), "--body-file", "-"], hub,
                        _comment(card))[0]:
            raise _fail("pr_failed", "gh pr comment", commented=done)
        done.append(card["task_id"])
    return done


def _secret_scan(clone, rng, base):
    """None when clean, else the refusal reason: secret_found (gitleaks exit 3) or
    secret_scan_failed (any other failure, or no binary): an unchecked push is refused. The
    config is the one at the remote `base` commit (a scanned commit cannot allowlist itself),
    else gitleaks defaults. Its output is never kept."""
    cmd = [os.environ.get("CAPSULE_GITLEAKS_BIN") or "gitleaks", "git", clone, "--no-banner",
           "--redact", "--exit-code", "3", "--log-opts", rng]
    code, text = _git("-C", clone, "show", f"{base}:.gitleaks.toml") if base else (1, "")
    with tempfile.TemporaryDirectory(prefix="gitleaks-") as tmp:
        if not code:
            Path(tmp, "config.toml").write_text(text + "\n")
            cmd += ["--config", str(Path(tmp, "config.toml"))]
        try:
            rc = subprocess.run(cmd, capture_output=True, timeout=300).returncode
        except (OSError, subprocess.TimeoutExpired):
            rc = -1
    return None if rc == 0 else "secret_found" if rc == 3 else "secret_scan_failed"


def _review(hub, number, review, draft, heads, doc):
    """Post the scrubbed whole-branch review once per head, then mark a draft PR ready; `doc`
    gains the flags. GitHub's markers and draft flag say what is already done. A review file
    whose content hash is already on the PR reviewed an older head: nothing is posted, the PR
    stays a draft and `doc` gets `review_stale`."""
    head, digest = doc["head"], hashlib.sha256(review.encode()).hexdigest()
    if head[:12] not in heads:
        old = next((h for h, d in heads.items() if d == digest[:12]), None)
        if old:
            doc["review_stale"] = True
            doc["review_stale_reason"] = (f"review.md unchanged since head {old}; "
                                          f"a new review is needed for {head[:12]}")
            return
        text = HOME_PATH.sub("<redacted:personal-path>", scrub.scrub(review))
        for pattern, marker in EXTRA_SECRETS:
            text = pattern.sub(marker, text)
        mark = f"<!-- capsule-review:{number}:{head[:12]}:{digest[:12]} -->"
        if daily_pr._gh(["pr", "review", str(number), "--comment", "--body-file", "-"], hub,
                        f"{text.rstrip()}\n\n{mark}\n")[0]:
            raise _fail("pr_failed", "gh pr review", **doc)
    doc["reviewed"], doc["reviewed_head"] = True, head
    doc["reviewed_sha256"] = digest
    if draft and daily_pr._gh(["pr", "ready", str(number)], hub)[0]:
        raise _fail("pr_failed", "gh pr ready", **doc)
    doc["ready"] = True


def _publish(want, repo, review):
    """The result document for one valid want; the only place gh and git push. A refusal keeps
    the PR it knew (pr_number, pr_url, head), so a later run and the poll still find it."""
    ctx = {}
    try:
        out = _push(want, repo, review, ctx)
    except RuntimeError as exc:
        if not (exc.args and isinstance(exc.args[0], dict)):
            raise
        out = exc.args[0]
    return {**ctx, **out}


def _draft_first(hub, found, head, ctx):
    """(is_draft, reviewed heads) after making sure a PR not reviewed at `head` is a draft."""
    ctx.update(pr_number=found["number"], pr_url=found["url"], head=head)
    draft, heads = _seen(hub, found["number"], ctx, undo=True)
    if not draft and head[:12] not in heads:    # ready, but not reviewed at this head
        if daily_pr._gh(["pr", "ready", "--undo", str(found["number"])], hub)[0]:
            raise _fail("undo_failed", "gh pr ready --undo", ready_unreviewed=True, **ctx)
        draft = True
    return draft, heads


def _push(want, repo, review, ctx):
    clone, branch, url = want["clone"], want["epic_branch"], want["repo_url"]
    hub = f"https://github.com/{repo}"
    if daily_pr._gh(["repo", "view", "--", repo], clone)[0]:
        return {"state": "refused", "reason": "repo_unknown", "detail": "gh repo view"}
    code, head = _git("-C", clone, "rev-parse", "HEAD")
    code2, remote = _git("-C", clone, "ls-remote", "--", url, f"refs/heads/{branch}")
    if code or code2:
        return {"state": "refused", "reason": "push_failed", "detail": "git ls-remote"}
    remote = remote.split()
    if remote and remote[0] != head and _git("-C", clone, "merge-base", "--is-ancestor",
                                              remote[0], head)[0]:
        return {"state": "refused", "reason": "diverged", "detail": "git merge-base"}
    if remote[:1] != [head]:
        why = _secret_scan(clone, f"{remote[0]}..{head}" if remote else head,
                           remote[0] if remote else None)
        if why:
            return {"state": "refused", "reason": why, "detail": "gitleaks git"}
    found = _existing(hub, branch)
    draft, heads = _draft_first(hub, found, head, ctx) if found else (True, {})
    if _git("-C", clone, "push", "--", url, f"{head}:refs/heads/{branch}")[0]:
        return {"state": "refused", "reason": "push_failed", "detail": "git push"}
    if found:
        number, pr_url = found["number"], found["url"]
        if daily_pr._gh(["pr", "edit", str(number), "--body-file", "-"], hub, _body(want))[0]:
            raise _fail("pr_failed", "gh pr edit", **ctx)
    else:
        number, pr_url = _create(hub, branch, want)
    doc = {"pr_url": pr_url, "pr_number": number, "head": head,
           "commented": _post_cards(hub, number, want["cards"])}
    if want.get("all_done") is True and review is not None:
        _review(hub, number, review, draft, heads, doc)
    return {"state": "published", **doc}


def _valid(want, banned):
    """(repo, None) of a well-formed want, else (None, why)."""
    if not isinstance(want, dict):
        return None, "not an object"
    strs = [want.get(k) for k in ("epic_branch", "clone", "repo_url", "spec")]
    strs.append(want.get("repo", ""))
    cards = want.get("cards")
    if not all(isinstance(s, str) and s for s in strs[:4]) or not isinstance(strs[4], str):
        return None, "field types"
    if not isinstance(cards, list) or not all(
            isinstance(c, dict) and all(isinstance(c.get(k), str) and c[k]
                                        for k in ("task_id", "title", "sha")) for c in cards):
        return None, "cards"
    rows = want.get("rows")
    if not isinstance(rows, list) or not all(type(r) is int for r in rows):
        return None, "rows"
    if any(s.startswith("-") for s in strs):
        return None, "leading dash"
    if any("::" in s for s in strs):          # every git remote-helper transport (ext::, fd::...)
        return None, "ext transport"
    if not all(CARD_ID.fullmatch(c["task_id"]) and SHA.fullmatch(c["sha"]) for c in cards):
        return None, "card id or sha"
    branch = want["epic_branch"]
    if not BRANCH.fullmatch(branch) or ".." in branch or branch in banned:
        return None, "epic_branch"
    match = daily_pr.GITHUB.fullmatch(want["repo_url"])
    repo = want.get("repo") or (match.group(1) if match else "")
    return (repo, None) if REPO.fullmatch(repo) else (None, "repo")


def _write(path, doc):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(doc, indent=1))
    os.replace(name, path)


def _doc(want_path, banned):
    """The result document for one want file; a refusal keeps the PR the last result knew."""
    prior = _load(_result_of(want_path))
    seed = {k: prior[k] for k in ("pr_number", "pr_url", "head") if k in prior}
    return {**seed, **_decide(want_path, banned)}


def _decide(want_path, banned):
    try:
        want = json.loads(want_path.read_text())
    except ValueError:
        want = None
    repo, why = _valid(want, banned)
    if not repo:
        return {"state": "refused", "reason": "bad_want", "detail": why}
    if daily_pr._gh(["auth", "status"], want["clone"])[0]:
        return {"state": "refused", "reason": "gh_auth", "detail": "gh auth status"}
    review = _load(want_path.with_name(want_path.name.replace(".want.json", ".review.md")), False)
    return _publish(want, repo, review)


def _result_of(want_path):
    return want_path.with_name(want_path.name.replace(".want.json", ".result.json"))


def _load(path, as_json=True):
    """The previous result (a dict, {} when missing or bad) or, as text, the review (None)."""
    try:
        text = path.read_text()
        doc = json.loads(text) if as_json else text
    except (OSError, ValueError):
        doc = None
    return doc if isinstance(doc, dict if as_json else str) else ({} if as_json else None)


def _polled(prior, result_path, banned, now):
    """The refreshed result document, None when this one is not due; RuntimeError names a step."""
    last = prior.get("polled_at")
    if not prior.get("pr_number") or (prior.get("pr") or {}).get("state") == "MERGED" or (
            isinstance(last, (int, float)) and now - last < POLL_EVERY):
        return None
    want_path = result_path.with_name(result_path.name.replace(".result.json", ".want.json"))
    want = json.loads(want_path.read_text())
    repo = _valid(want, banned)[0]
    if not repo:
        raise RuntimeError("want")
    rc, out = daily_pr._gh(["pr", "view", str(prior["pr_number"]), "--json", f"{VIEW},reviews"],
                           f"https://github.com/{repo}")
    pr = None if rc else json.loads(out)
    if not isinstance(pr, dict):
        raise RuntimeError("gh pr view")
    heads = _heads(prior["pr_number"], pr.pop("reviews", []))
    doc = {**prior, "pr": pr, "polled_at": int(now)}
    doc.pop("poll_error", None)
    doc.pop("ready_unreviewed", None)
    if pr.get("isDraft") is False and str(pr.get("headRefOid", ""))[:12] not in heads:
        doc["ready_unreviewed"] = True      # ready on GitHub with no review of its head
    if pr.get("state") == "MERGED":
        try:
            failed = _git("-C", want["clone"], "fetch", "--", want["repo_url"], "main")[0]
        except (OSError, subprocess.TimeoutExpired):
            failed = True
        if failed:
            raise RuntimeError("git fetch")
        doc["fetched_after_merge"] = True
    return doc


def _poll_one(result_path, banned, now):
    """Refresh one result file with its PR's state; never pushes, comments or touches the want."""
    prior = _load(result_path)
    try:
        doc = _polled(prior, result_path, banned, now)
    except Exception as exc:    # one bad result must never stop the next
        step = exc.args[0] if isinstance(exc, RuntimeError) else (
            "want" if isinstance(exc, OSError) else
            "gh pr view" if isinstance(exc, ValueError) else type(exc).__name__)
        doc = {**prior, "poll_error": step}
    if doc is None:
        return
    try:
        _write(result_path, doc)
    except OSError as exc:
        print(f"capsule publish: cannot write {result_path.name}: {exc}", file=sys.stderr)


def _one(want_path, banned):
    result = _result_of(want_path)
    try:
        doc = _doc(want_path, banned)
    except Exception as exc:    # one bad want must never stop the next
        doc = {"state": "refused", "reason": "internal", "detail": type(exc).__name__}
    try:
        _write(result, doc)
    except OSError as exc:
        print(f"capsule publish: cannot write {result.name}: {exc}", file=sys.stderr)


def add_parser(sub):
    p = sub.add_parser("publish", help="put each wanted epic on GitHub as one draft PR "
                                       "(exit 75: lock busy, try later)")
    p.add_argument("--capsule-home", default=None)
    p.add_argument("--poll", action="store_true",
                   help="refresh each open PR's state in its result file (never pushes)")


def _load_config(home):
    """The validated config dict, or None after printing the one-line config error."""
    try:
        cfg = json.loads(paths.config_path(home).read_text())
    except OSError:
        cfg = {}
    except ValueError as exc:
        print(f"capsule publish: config error: {exc}", file=sys.stderr)
        return None
    if not isinstance(cfg, dict):
        print("capsule publish: config error: not an object", file=sys.stderr)
        return None
    try:
        config.validate_lookout(cfg.get("lookout", {}))
        config.validate_lander_branch(cfg.get("lander"))
    except ValueError as exc:
        print(f"capsule publish: config error: {exc}", file=sys.stderr)
        return None
    return cfg


def _run_locked(folder, banned, poll):
    """Poll or publish every file in `folder` while holding the shared lock; the exit code."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        lock = open(folder / ".publish.lock", "a")        # poll and publish share it
    except OSError as exc:
        print(f"capsule publish: cannot open the lock file: {exc}", file=sys.stderr)
        return 1
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("capsule publish: busy (another publish or poll holds the lock)",
                  file=sys.stderr)
            return 75
        if poll:
            for result_path in sorted(folder.glob("*.result.json")):
                _poll_one(result_path, banned, time.time())
            return 0
        for want_path in sorted(folder.glob("*.want.json")):
            _one(want_path, banned)
    return 0


def handle(args, parser):
    home = paths.home(args.capsule_home)
    cfg = _load_config(home)
    if cfg is None:
        return 1
    if not cfg.get("lookout", {}).get("publish", False):
        print("capsule publish: off (lookout.publish is false)")
        return 0
    banned = {"main", "master", (cfg.get("lander") or {}).get("branch", "main")}
    return _run_locked(Path(home, "lookout", "publish"), banned, args.poll)
