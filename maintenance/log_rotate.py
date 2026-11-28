"""log.md rotation (Capsule decision row 128, M078)."""
import datetime
import hashlib
import os
import re
from pathlib import Path

ENTRY_RE = re.compile(r"^##\s*\[(\d{4})-(\d{2})-(\d{2})\]\s*(.*)$")
ARCHIVED_HEADING = "## Archived"
INDEX_LINE_RE = re.compile(r"^- \[([^\]]+)\]")
INDEX_MAX_LINES = 20


def _quarter(year, month):
    return f"{year}-Q{(int(month) - 1) // 3 + 1}"


def _atomic_write(path, text):
    """Write `text` to `path` via a same-directory temp file + os.replace, so
    a crash mid-write never leaves a partial file in place."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _entry_text(entry):
    return "\n".join(entry["lines"]).rstrip("\n")


def _split(text):
    """(header, existing_index, entries)."""
    lines = text.splitlines()
    starts = [i for i, l in enumerate(lines) if ENTRY_RE.match(l)]
    entry_start = starts[0] if starts else len(lines)
    archived_at = next((i for i in range(entry_start)
                         if lines[i].strip() == ARCHIVED_HEADING), None)
    if archived_at is None:
        header = "\n".join(lines[:entry_start]).rstrip() + "\n"
        existing_index = []
    else:
        header = "\n".join(lines[:archived_at]).rstrip() + "\n"
        existing_index = [l for l in lines[archived_at + 1:entry_start]
                           if INDEX_LINE_RE.match(l)]
    entries = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        m = ENTRY_RE.match(lines[i])
        entries.append({"date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
                        "quarter": _quarter(m.group(1), m.group(2)),
                        "lines": lines[i:end]})
    return header, existing_index, entries


def _rollup(quarter, entries):
    titles = [ENTRY_RE.match(e["lines"][0]).group(4).strip() for e in entries]
    word = "entry" if len(entries) == 1 else "entries"
    return (f"---\ndate: {entries[-1]['date']}\ntype: rollup\n"
            f"tags: [rollup, operation-log, capsule-corp]\nai-first: true\n"
            f"status: historical\n---\n\n## For future agent\n\n"
            f"Quarterly rollup of the vault's operation log for **{quarter}**: "
            f"{len(entries)} {word}, the oldest {entries[0]['date']}, the newest "
            f"{entries[-1]['date']}. Written BEFORE the detail moved (Capsule "
            f"decision row 128). The full entries are at "
            f"`_archive/log-{quarter}.md`, which is in git and readable by path "
            f"but excluded from both retrieval arms. Known cost on record: a "
            f"rollup loses detail.\n\n## Entries\n\n"
            + "\n".join(f"- **{e['date']}** - {t}" for e, t in zip(entries, titles))
            + "\n")


def _write_rollups(vault, by_q):
    (vault / "Knowledge").mkdir(parents=True, exist_ok=True)
    for q, es in sorted(by_q.items()):
        _atomic_write(vault / "Knowledge" / f"Operation Log Rollup {q}.md",
                      _rollup(q, es))


def _write_archive(vault, by_q):
    """Append each quarter's moved entries to `_archive/log-<quarter>.md`."""
    (vault / "_archive").mkdir(parents=True, exist_ok=True)
    for q, es in sorted(by_q.items()):
        dest = vault / "_archive" / f"log-{q}.md"
        body = "\n\n".join(_entry_text(e) for e in es)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        marker = f"<!-- rotated {q} sha256:{digest} -->"
        prior = dest.read_text(encoding="utf-8") if dest.is_file() \
            else f"# Operation Log - {q}\n\n"
        prior_stripped = prior.rstrip("\n")
        if marker in prior or prior_stripped.endswith(body):
            continue
        _atomic_write(dest, prior_stripped + "\n\n" + marker + "\n" + body + "\n")


def _trim_log(vault, log, header, existing_index, by_q, keep):
    new_lines = [f"- [{q}] {len(es)} entries -> `_archive/log-{q}.md` "
                 f"(rollup: [[Knowledge/Operation Log Rollup {q}]])"
                 for q, es in sorted(by_q.items())]
    fresh_quarters = {INDEX_LINE_RE.match(l).group(1) for l in new_lines}
    merged = [l for l in existing_index
              if INDEX_LINE_RE.match(l).group(1) not in fresh_quarters]
    merged += new_lines
    merged = merged[-INDEX_MAX_LINES:]

    live = "\n\n".join(_entry_text(e) for e in keep)
    body = header.rstrip() + "\n\n" + ARCHIVED_HEADING + "\n\n" + "\n".join(merged)
    if live:
        body += "\n\n" + live
    _atomic_write(log, body + "\n")


def rotate(vault, before, *, apply=False):
    """Move log.md entries dated before `before` (ISO date) into
    `_archive/log-<quarter>.md`, after writing one rollup note per quarter."""
    vault = Path(vault)
    cutoff = datetime.date.fromisoformat(str(before))
    log = vault / "log.md"
    if not log.is_file():
        return {"entries_to_move": 0, "order": [], "quarters": []}
    header, existing_index, entries = _split(log.read_text(encoding="utf-8"))
    old = [e for e in entries if datetime.date.fromisoformat(e["date"]) < cutoff]
    keep = [e for e in entries if e not in old]
    by_q = {}
    for e in old:
        by_q.setdefault(e["quarter"], []).append(e)
    res = {"entries_to_move": len(old), "quarters": sorted(by_q), "order": []}
    if not old or not apply:
        return res

    # 1. ROLLUP FIRST.
    _write_rollups(vault, by_q)
    res["order"].append("rollup")

    # 2. Then the lines move.
    _write_archive(vault, by_q)
    res["order"].append("archive")

    # 3. Then log.md is trimmed to the live entries plus a bounded index.
    _trim_log(vault, log, header, existing_index, by_q, keep)
    res["order"].append("trim")
    return res
