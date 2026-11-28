"""M137 D7 edit: add one item to `[image].apt` (a Debian package) or `[image].smoke` (a
`command -v <bin>` line) in a project's own capsule.toml, and nothing else. Only a single-line
array is rewritten (the line's trailing comment is kept); a multi-line array, a missing `[image]`
or no `stacks` is refused, never guessed. The new text must re-parse (`tomllib`) to the old
document with exactly that one list grown: the second oracle against the line edit (Z2)."""
import copy
import json
import re
import tomllib

HEADER = re.compile(r"^\s*\[[^\[\]]+\]\s*(#.*)?$")
IMAGE = re.compile(r"^\s*\[image\]\s*(#.*)?$")


def add_item(text, key, item):
    """(new_text, None) or (None, why the architect must decide)."""
    try:
        old = tomllib.loads(text)
    except ValueError as exc:
        return None, f"capsule.toml does not parse: {exc}"
    image = old.get("image")
    if not isinstance(image, dict) or not image.get("stacks"):
        return None, "capsule.toml has no [image].stacks: the stack is the architect's call"
    vals = image.get(key, [])
    if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
        return None, f"[image].{key} is not a list of strings"
    vals = vals + ([item] if item not in vals else [])
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if IMAGE.match(ln)), None)
    if start is None:        # an inline `image = {...}` table or a spaced `[ image ]` header
        return None, "capsule.toml has no plain [image] header: edit it by hand"
    end = next((i for i in range(start + 1, len(lines)) if HEADER.match(lines[i])), len(lines))
    body = "[" + ", ".join(json.dumps(v) for v in vals) + "]"
    pat = re.compile(rf"^(\s*{key}\s*=\s*)\[[^\]\n]*\](\s*#.*)?$")
    hit = next((i for i in range(start + 1, end) if pat.match(lines[i])), None)
    if hit is not None:
        lines[hit] = pat.sub(lambda m: m.group(1) + body + (m.group(2) or ""), lines[hit])
    elif any(re.match(rf"^\s*{key}\s*=", lines[i]) for i in range(start + 1, end)):
        return None, f"[image].{key} is a multi-line array: edit it by hand"
    else:
        last = max(i for i in range(start, end) if lines[i].strip() or i == start)
        lines.insert(last + 1, f"{key} = {body}")
    new = "\n".join(lines)
    expected = copy.deepcopy(old)
    expected["image"][key] = vals
    try:
        ok = tomllib.loads(new) == expected
    except ValueError:
        ok = False
    return (new, None) if ok else (None, "the edit would change something besides the one list")
