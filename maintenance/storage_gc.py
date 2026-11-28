#!/usr/bin/env python3
"""One preview/apply for Docker and temp storage (vault decision row 643: common maintenance is
scripted, never improvised). No flag = dry run that prints every item chosen or kept and why;
`--apply` removes. Exit 0 on success, 1 when a section failed or an item would not go, with a
one-line reason on stderr. Every --apply item is logged as one JSON line in
<events.state_root()>/storage_gc.jsonl (what, id, before-state, result).

Touches only what Capsule made and can prove it made; the rest prints "kept: <reason>". Never
forced: no -f, no prune. Each removal is one id, re-checked just before and verified gone after.
- Containers: label capsule=1 (runners/docker.py run_args), exited or created, over 1 hour old,
  never capsule.kind=image (a container inherits its image's labels), not the compose stack
  (capsule-corp-* names, compose project label): `docker rm <id>`.
- Volumes: dangling, no container (running or stopped) mounts it, labelled capsule=1, over 2 days
  old (over 1 day when capsule.kind is test or run; never when it is image), not a compose volume
  (capsule-corp_*, compose project label). An anonymous (64 hex) volume without the label is not
  proven Capsule's: kept, one summary line (ids with --verbose), except the one-time legacy sweep
  (_legacy_volume): such a volume with no label except Docker's own anonymous-volume marker,
  made before image_gc.LEGACY_BEFORE and over 1 day old, unmounted, goes (shown "[legacy]").
- Images: maintenance/image_gc.py (dry run here, OPS_IMAGE_GC_APPLY=1 only under --apply).
- Temp dirs: cleanup.sh's rules, ported: top level of CLEANUP_TMPDIR/TMPDIR//tmp only, our uid,
  a real directory (never a link) on TMPDIR's device, older than 1 day with nothing inside
  modified within the day or on another device, names tmp.?????????? tmp???????? capsule-suite-*
  mutation-check-* lander-box-*. TMPDIR must resolve under /private/var/folders/ or /private/tmp
  and never be / or $HOME or a parent of it, else the section is refused (exit 1).
Each removal is wrapped: an item gone meanwhile is "skipped: gone", any other error "FAILED: ...".
A time Docker gives that cannot be read keeps the item.
"""
import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events   # noqa: E402  docker_run() and state_root(), the doors every maintenance job uses
from image_gc import AGE_KINDS, AGE_LIMIT, LEGACY_BEFORE, when   # noqa: E402  when(): UTC time
# of a Docker stamp, unreadable = far future; the others are the shared age rules (image_gc.py)

CONTAINER_AGE, VOLUME_AGE, TMP_AGE = timedelta(hours=1), timedelta(days=2), 86400
TMP_PATTERNS = ("tmp.??????????", "tmp????????", "capsule-suite-*", "mutation-check-*",
                "lander-box-*")
TMP_ROOTS = ("/private/var/folders/", "/private/tmp/")    # realpath prefixes of macOS temp dirs
COMPOSE = "com.docker.compose.project"
ANONYMOUS = "com.docker.volume.anonymous"      # the one label Docker puts on an anonymous volume
UNITS = {"B": 1, "kB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}


def _try(*args):
    return events.docker_run(list(args), capture_output=True, text=True, timeout=120)


def docker(*args):
    r = _try(*args)
    if r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed")
    return r.stdout


def _container_keep(d, now):
    """(keep reason or None, stamp) for one `container inspect` entry; used to list and again
    right before the removal, so both ask the same question."""
    state, name = d["State"]["Status"], d["Name"].lstrip("/")
    labels = (d.get("Config") or {}).get("Labels") or {}
    stamp = d["Created"] if state == "created" else d["State"].get("FinishedAt", "")
    keep = (f"state {state}" if state not in ("exited", "created") else
            "compose stack" if name.startswith("capsule-corp-") or COMPOSE in labels else
            "no capsule=1 label" if labels.get("capsule") != "1" else
            "kind image is never removed by age" if labels.get("capsule.kind") == "image" else
            "younger than 1 hour" if when(stamp) > now - CONTAINER_AGE else None)
    return keep, stamp


def _containers(now):
    ids = docker("ps", "-a", "-q", "--no-trunc", "--filter", "label=capsule=1").split()
    out = []
    for d in json.loads(docker("container", "inspect", "--size", *ids)) if ids else []:
        keep, stamp = _container_keep(d, now)
        name = d["Name"].lstrip("/")
        out.append({"id": d["Id"], "label": f"{d['Id'][:12]} {name}", "keep": keep,
                    "bytes": d.get("SizeRw") or 0,
                    "before": {"name": name, "state": d["State"]["Status"], "stamp": stamp}})
    return out


def _mounted_volumes():
    ids = docker("ps", "-a", "-q", "--no-trunc").split()
    found = json.loads(docker("container", "inspect", *ids)) if ids else []
    return {m.get("Name") for d in found for m in d.get("Mounts") or [] if m.get("Name")}


def _volume_sizes():
    """{volume name: bytes} from the plain `docker system df -v` table (best effort, 0 if odd)."""
    sizes, on = {}, False
    for line in docker("system", "df", "-v").splitlines():
        on = on or line.startswith("Local Volumes")
        parts = line.split()
        m = re.fullmatch(r"([\d.]+)([kMGT]?B)", parts[2]) if on and len(parts) == 3 else None
        if m and parts[1].isdigit():
            sizes[parts[0]] = int(float(m.group(1)) * UNITS[m.group(2)])
    return sizes


def _legacy_volume(d, now):
    """ONE-TIME legacy sweep (rule B): an anonymous (64 hex) volume with no label except Docker's
    own anonymous marker, made before LEGACY_BEFORE and over AGE_LIMIT old. Delete this function
    (and its call in _volume_keep) once a run shows 0 legacy items; after LEGACY_BEFORE it
    selects nothing."""
    created = when(d.get("CreatedAt", ""))
    own = set(d.get("Labels") or {}) - {ANONYMOUS}      # Docker itself labels anonymous volumes
    return bool(re.fullmatch(r"[0-9a-f]{64}", d["Name"]) and not own
                and created < LEGACY_BEFORE and created <= now - AGE_LIMIT)


def _volume_keep(d, used, now):
    """Why a volume stays, or None to remove it; used to list and again right before the rm.
    Rule A: capsule=1 with kind test or run goes at AGE_LIMIT; any other capsule=1 volume at
    VOLUME_AGE (as before); kind=image never. The caller has already listed it as unused."""
    name, labels = d["Name"], d.get("Labels") or {}
    anon = bool(re.fullmatch(r"[0-9a-f]{64}", name))
    age, text = ((AGE_LIMIT, "1 day") if labels.get("capsule.kind") in AGE_KINDS
                 else (VOLUME_AGE, "2 days"))
    return ("a container mounts it" if name in used else
            "compose volume" if name.startswith("capsule-corp_") or COMPOSE in labels else
            None if _legacy_volume(d, now) else
            "anonymous, not proven Capsule's" if anon and labels.get("capsule") != "1" else
            "no capsule=1 label" if labels.get("capsule") != "1" else
            "kind image is never removed by age" if labels.get("capsule.kind") == "image" else
            f"younger than {text}" if when(d.get("CreatedAt", "")) > now - age else None)


def _volumes(now):
    names = docker("volume", "ls", "-q", "--filter", "dangling=true").split()
    used, out = _mounted_volumes(), []
    for d in json.loads(docker("volume", "inspect", *names)) if names else []:
        name, labels = d["Name"], d.get("Labels") or {}
        keep = _volume_keep(d, used, now)
        legacy = " [legacy]" if not keep and _legacy_volume(d, now) else ""
        out.append({"id": name, "label": name[:24] + legacy, "keep": keep, "bytes": 0,
                    "quiet": keep == "anonymous, not proven Capsule's",
                    "before": {"created": d.get("CreatedAt"), "labels": labels}})
    sizes = _volume_sizes() if out else {}
    for v in out:
        v["bytes"] = sizes.get(v["id"], 0)
    return out


def _scan(path, now, dev):
    """(keep reason or None, disk bytes): keep when anything inside was modified within the day
    (find -mmin) or sits on another device (a mount point inside the directory)."""
    def fail(e):
        raise e
    size = os.lstat(path).st_blocks * 512
    for here, dirs, files in os.walk(path, onerror=fail):
        for n in dirs + files:
            s = os.lstat(os.path.join(here, n))
            if s.st_dev != dev:
                return "mount point inside (different device)", 0
            if now - s.st_mtime < TMP_AGE:
                return "modified inside within 1 day", 0
            size += s.st_blocks * 512
    return None, size


def _tmp_verdict(path, now, uid, dev):
    """(keep reason or None, bytes) for one top-level entry, from a fresh lstat every time."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        return "not a real directory (links are never followed)", 0
    if st.st_dev != dev:
        return "different device", 0
    if st.st_uid != uid:
        return "other owner", 0
    if now - st.st_mtime <= TMP_AGE:
        return "younger than 1 day", 0
    try:
        return _scan(path, now, dev)
    except OSError as e:
        return f"unreadable ({e.__class__.__name__})", 0


def _tmp_root(raw):
    """The real path of the temp dir, or RuntimeError saying why the temp section is refused:
    only macOS's own temp roots, never / or $HOME or a parent of it."""
    real, home = os.path.realpath(raw), os.path.realpath(os.path.expanduser("~"))
    if not os.path.isabs(raw):
        raise RuntimeError(f"temp dir {raw!r} is not an absolute path")
    if real == "/" or home == real or home.startswith(real + "/"):
        raise RuntimeError(f"temp dir {raw!r} resolves to {real}, / or $HOME or its parent")
    if not (real.startswith(TMP_ROOTS) or real == "/private/tmp") or not os.path.isdir(real):
        raise RuntimeError(f"temp dir {raw!r} resolves to {real}, not a macOS temp root")
    return real


def _tmp_items(tmpdir, now):
    root = _tmp_root(tmpdir)
    uid, dev, out = os.getuid(), os.stat(root).st_dev, []
    for e in sorted(os.scandir(root), key=lambda e: e.name):
        if any(fnmatch.fnmatchcase(e.name, p) for p in TMP_PATTERNS):
            try:
                keep, size = _tmp_verdict(e.path, now, uid, dev)
            except FileNotFoundError:
                continue        # a live suite removed it while we looked
            out.append({"id": e.path, "label": e.path, "keep": keep, "bytes": size,
                        "quiet": bool(keep), "before": {"bytes": size}})
    return out


def _inspect(kind, ident):
    """The parsed `docker <kind> inspect`, or None when Docker says the object does not exist.
    Any other failure (daemon down, timeout) raises, so it is a FAILED item, never a 'gone'."""
    r = _try(kind, "inspect", ident)
    if r.returncode == 0:
        return json.loads(r.stdout)
    if "no such" in r.stderr.lower():
        return None
    raise RuntimeError(f"docker {kind} inspect failed: {(r.stderr.strip() or '?').splitlines()[0]}")


def _rm_container(item, now):
    found = _inspect("container", item["id"])
    if not found:
        return "skipped: gone"
    keep, _ = _container_keep(found[0], datetime.now(timezone.utc))
    if keep:
        return f"skipped: {keep}"
    if _try("rm", item["id"]).returncode != 0:
        return "FAILED: docker rm"
    return "FAILED: still there" if _inspect("container", item["id"]) else "removed"


def _rm_volume(item, now):
    found = _inspect("volume", item["id"])
    if not found:
        return "skipped: gone"
    used = {item["id"]} if docker("ps", "-a", "-q", "--filter",
                                  f"volume={item['id']}").split() else set()
    if _volume_keep(found[0], used, datetime.now(timezone.utc)):
        return "kept: changed since the check"
    if _try("volume", "rm", item["id"]).returncode != 0:
        return "FAILED: docker volume rm"
    return "FAILED: still there" if _inspect("volume", item["id"]) else "removed"


def _rm_tmp(item, now):
    """A vanished directory is 'gone' only when the top dir is gone; a half-removed one fails."""
    try:
        return _rm_tmp_dir(item["id"])
    except FileNotFoundError:
        return "FAILED: partly removed" if os.path.lexists(item["id"]) else "skipped: gone"


def _rm_tmp_dir(path):
    now = time.time()
    dev = os.lstat(os.path.dirname(path)).st_dev
    keep, _ = _tmp_verdict(path, now, os.getuid(), dev)
    if keep or not any(fnmatch.fnmatchcase(os.path.basename(path), p) for p in TMP_PATTERNS):
        return f"skipped: {keep or 'name no longer matches'}"
    if not _chmod_dir(path):
        return "skipped: not a real directory (links are never followed)"
    for here, dirs, _ in os.walk(path):
        for d in dirs:
            _chmod_dir(os.path.join(here, d))
    # accepted residual: a bind mount appearing between check and rmtree needs root on macOS
    shutil.rmtree(path, ignore_errors=True)
    return "FAILED: still there" if os.path.lexists(path) else "removed"


def _chmod_dir(path):
    """0700 on a real directory so a suite's read-only dirs cannot stop the removal (cleanup.sh
    does the same). Re-lstat first; never through a final-component link (an ancestor swapped
    mid-walk by a same-user process is not covered either way)."""
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        return False
    if os.chmod in os.supports_follow_symlinks:     # macOS: works on a mode-000 dir too
        os.chmod(path, 0o700, follow_symlinks=False)
        return True
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)   # a link fails the open
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return True


def _safe(remove, item, now):
    """One removal that can never stop the section: gone is a skip, anything else a FAILED."""
    try:
        return remove(item, now)
    except Exception as e:   # noqa: BLE001  per-item resilience: report it, go on to the next
        return f"FAILED: {e.__class__.__name__}: {e}"[:200]


def _kept(name, items, verbose):
    """Print the kept items: a quiet one only as a per-reason summary line (ids with --verbose)."""
    quiet = {}
    for i in items:
        if i["keep"] and i.get("quiet"):
            quiet.setdefault(i["keep"], []).append(i)
        elif i["keep"]:
            print(f"kept: {name} {i['label']} ({i['keep']})")
    for reason, group in quiet.items():
        gb = sum(i["bytes"] for i in group) / 1e9
        print(f"kept: {reason}: {len(group)} {name}s" + (f", {gb:.2f} GB" if gb else ""))
        for i in group if verbose else ():
            print(f"kept: {name} {i['id']} ({reason})")


def _section(name, items, remove, apply, now, log, verbose=False):
    """Print every item, remove the chosen ones when applying; returns the failed count."""
    chosen = [i for i in items if not i["keep"]]
    _kept(name, items, verbose)
    size, failed = sum(i["bytes"] for i in chosen), 0
    for i in chosen:
        result = _safe(remove, i, now) if apply else "would remove"
        print(f"{'' if apply else 'would '}remove {name} {i['label']} ({i['bytes']} bytes)"
              + (f" -> {result}" if apply else ""))
        if apply:
            log({"what": name, "id": i["id"], "before": i["before"], "result": result})
            failed += result.startswith("FAILED")
    print(f"storage-gc: {name}: {len(chosen)} chosen ({size} bytes), "
          f"{len(items) - len(chosen)} kept")
    return failed


def _images(apply, log):
    env = {k: v for k, v in os.environ.items() if k != "OPS_IMAGE_GC_APPLY"}
    if apply:
        env["OPS_IMAGE_GC_APPLY"] = "1"
    r = subprocess.run([sys.executable, str(Path(__file__).with_name("image_gc.py"))], env=env,
                       capture_output=True, text=True, timeout=600)
    lines = r.stdout.splitlines()
    print("".join(f"images: {line}\n" for line in lines), end="")
    print("".join(f"images: stderr: {line}\n" for line in r.stderr.splitlines()), end="")
    failed = r.returncode != 0 or any(line.rstrip().endswith("FAILED") for line in lines)
    gone = sum(int(m.group(1)) for line in lines if not line.rstrip().endswith("FAILED")
               for m in [re.match(r"(?:would )?remove \S+ .*\((\d+) bytes\)", line)] if m)
    print(f"storage-gc: images: {gone} bytes by image_gc.py (rc {r.returncode})")
    if apply:
        log({"what": "images", "id": "image_gc.py", "before": {"bytes": gone},
             "result": "FAILED" if failed else "ran"})
    return failed


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    apply, verbose = "--apply" in args, "--verbose" in args
    now, tmpdir = datetime.now(timezone.utc), os.environ.get("CLEANUP_TMPDIR") or \
        os.environ.get("TMPDIR") or "/tmp"
    path = events.state_root() / "storage_gc.jsonl"

    def log(rec):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({"at": now.isoformat(), **rec}) + "\n")
    steps = [("container", _containers, _rm_container, now),
             ("volume", _volumes, _rm_volume, now),
             ("tmpdir", partial(_tmp_items, tmpdir), _rm_tmp, time.time())]
    bad = []
    for name, plan, remove, clock in steps:
        try:
            if _section(name, plan(clock), remove, apply, clock, log, verbose):
                bad.append(f"{name}: an item failed")
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as e:
            bad.append(f"{name}: {e}")
    try:
        if _images(apply, log):
            bad.append("images: image_gc.py failed")
    except (OSError, subprocess.SubprocessError) as e:
        bad.append(f"images: {e}")
    for reason in bad:
        print(f"storage-gc: FAILED {reason}", file=sys.stderr)
    print("storage-gc: " + ("applied" if apply else "dry-run, nothing changed; --apply removes"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
