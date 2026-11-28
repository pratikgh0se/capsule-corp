"""Hollow-test detector (decision D85; D44: warn-only).

The mutation check is Python-only, so Swift and web tests had no check that they can fail. This
reads each changed test file (Python, Swift XCTest / Swift Testing, TS/JS it()/test()) with simple
patterns and flags tests that check nothing: no assertion, only constant assertions or
`is not None` on a just-constructed object, an empty / pass / skip body, snapshot-only. A call to
a same-file helper that really asserts counts (one level). WARN ONLY: `bundles.reviewer` shows
the line to the reviewer; nothing raises, and a file too big or unparseable is skipped. Scans are
linear: brackets are paired in one pass and a body that never closes is skipped."""
import ast
import re
import warnings

MAX_FILES = 40      # a card that changes more test files than this still gets the first 40 read
MAX_BYTES = 256 * 1024     # a bigger file is skipped, never scanned
SHOW = 4            # findings named on the line; the rest is "+N more"

_PY_TEST = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")
_SWIFT_TEST = re.compile(r"(^|/)([^/]*Tests?\.swift|Tests/.*\.swift)$")
_JS_TEST = re.compile(r"(^|/)[^/]*\.(test|spec)\.(js|jsx|mjs|cjs|ts|tsx)$")


def _kind(path):
    p = str(path).replace("\\", "/")
    for kind, rx in (("py", _PY_TEST), ("swift", _SWIFT_TEST), ("js", _JS_TEST)):
        if rx.search(p):
            return kind
    return None


def _verdict(kinds):
    if not kinds:
        return "no assertion"
    if "real" in kinds:
        return None
    return "snapshot-only" if "snapshot" in kinds else "constant assertions only"


_PY_PREFIX = ("assert", "check", "verify", "expect")
_PY_EXACT = ("raises", "warns", "fail", "deprecated_call")


def _call_name(call):
    """Lower-cased callee name without leading underscores (`_assert_x` reads as `assert_x`)."""
    f = call.func
    return (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")).lstrip("_").lower()


def _const(node):
    return isinstance(node, ast.Constant)


def _constructed(value):
    """True for `Thing(...)` / `mod.Thing(...)`: a constructor call, not any function call."""
    if not isinstance(value, ast.Call):
        return False
    f = value.func
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
    return name[:1].isupper()


def _py_trivial_assert(node, created):
    t = node.test
    if _const(t):
        return True
    if not (isinstance(t, ast.Compare) and len(t.ops) == 1):
        return False
    if _const(t.left) and _const(t.comparators[0]):
        return True
    return (isinstance(t.ops[0], ast.IsNot) and isinstance(t.left, ast.Name)
            and t.left.id in created and _const(t.comparators[0])
            and t.comparators[0].value is None)


def _py_trivial_call(call, created):
    name, args = _call_name(call), call.args
    if name in ("asserttrue", "assertfalse"):
        return bool(args) and _const(args[0])
    if name in ("assertequal", "assertis"):
        return len(args) >= 2 and _const(args[0]) and _const(args[1])
    return (name == "assertisnotnone" and bool(args)
            and isinstance(args[0], ast.Name) and args[0].id in created)


def _py_call_kind(n, created, helpers):
    name = _call_name(n)
    if "snapshot" in ast.unparse(n.func).lower():
        return "snapshot"
    if name in helpers:
        return "real"
    if name.startswith(_PY_PREFIX) or name in _PY_EXACT:
        return "trivial" if _py_trivial_call(n, created) else "real"
    return None


def _py_kinds(fn, helpers=()):
    created = {t.id for a in ast.walk(fn)
               if isinstance(a, ast.Assign) and _constructed(a.value)
               for t in a.targets if isinstance(t, ast.Name)}
    kinds = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            kinds.append("trivial" if _py_trivial_assert(n, created) else "real")
        elif isinstance(n, ast.Call):
            kinds.append(_py_call_kind(n, created, helpers))
    return [k for k in kinds if k]


def _py_helpers(tree):
    """Names of same-file non-test functions whose own body holds a real assertion."""
    return {fn.name.lstrip("_").lower() for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not fn.name.startswith("test") and "real" in _py_kinds(fn)}


def _py_pass_like(s):
    return isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and _const(s.value))


def _py_skip_like(s):
    if _py_pass_like(s):
        return True
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
        return _call_name(s.value) in ("skiptest", "skip")
    return isinstance(s, ast.Raise) and "skip" in ast.unparse(s).lower()


def _py_skip_deco(d):
    head = ast.unparse(d).lower().split("(")[0]
    return head == "skip" or head.endswith(".skip")


def _py_reason(fn, helpers):
    if any(_py_skip_deco(d) for d in fn.decorator_list):
        return "skipped"
    if all(_py_pass_like(s) for s in fn.body):
        return "empty body"
    if all(_py_skip_like(s) for s in fn.body):
        return "skipped"
    return _verdict(_py_kinds(fn, helpers))


def _scan_py(text):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")      # invalid-escape SyntaxWarnings in a test's source
        tree = ast.parse(text)
    helpers, out = _py_helpers(tree), []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
            reason = _py_reason(fn, helpers)
            if reason:
                out.append((fn.name, reason))
    return out


_COMMENT = r"//[^\n]*|/\*.*?(?:\*/|\Z)"      # an unclosed /* runs to the end, once
_STRING = r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'"
_SWIFT_TOKEN = re.compile(r"\"\"\".*?\"\"\"|" + _COMMENT + "|" + _STRING, re.S)
_JS_TOKEN = re.compile(
    _COMMENT + "|" + _STRING + r"|`(?:\\.|[^`\\])*`"
    r"|(?P<re>(?<!<)/(?![*/>])(?:\\.|\[(?:\\.|[^\]\\\n]){0,200}\]|[^/\\\n\[]){1,300}/[a-z]*)",
    re.S)
_CONST = r"(?:true|false|null|nil|undefined|\d+(?:\.\d+)?|\"\s*\"|'\s*')"


def _is_division(s, i):
    i -= 1
    while i >= 0 and s[i] in " \t\n":
        i -= 1
    return i >= 0 and (s[i].isalnum() or s[i] in "_$)]")


def _blank(m):
    s = m.group()
    if m.groupdict().get("re") and _is_division(m.string, m.start()):
        return s
    inner = re.sub(r"[^\n]", " ", s)
    if s[0] == "/" or s.startswith('"""'):
        return inner
    return s[0] + inner[1:-1] + s[-1]


def _strip(text, js):
    """Comments and string contents (Swift triple-quoted blocks, JS template strings and regex
    literals too) blanked, same length, so offsets map back onto `text`."""
    return (_JS_TOKEN if js else _SWIFT_TOKEN).sub(_blank, text)


def _pairs(src):
    """{open index: close index} for `{}` and `()`, one pass; an unclosed bracket is absent."""
    out, stacks = {}, {"{": [], "(": []}
    closer = {"}": "{", ")": "("}
    for m in re.finditer(r"[{}()]", src):
        c = m.group()
        if c in stacks:
            stacks[c].append(m.start())
        elif stacks[closer[c]]:
            out[stacks[closer[c]].pop()] = m.start()
    return out


def _created(name, body):
    pat = (r"\b%s\s*(?::[^=\n]+)?=\s*(?:await\s+|try\s+)?"
           r"(?:new\s+[\w.]+\(|(?:[\w.]*\.)?[A-Z]\w*\()")
    return re.search(pat % re.escape(name), body)


def _trivial_count(body, rules):
    n = 0
    for rx, needs_created in rules:
        n += sum(1 for m in rx.finditer(body) if not needs_created or _created(m.group(1), body))
    return n


def _helper_rx(names):
    if not names:
        return None
    return re.compile(r"(?<![\w$])(?:%s)\s*\(" % "|".join(re.escape(n) for n in sorted(names)))


def _text_kinds(body, rules, helpers=None):
    """Assertion kinds of a Swift/JS body; snapshot and trivial matches are subsets of the
    assertion matches; a call to a same-file asserting helper adds one real assertion."""
    assert_re, snap_re, trivial = rules
    n_assert, n_snap = len(assert_re.findall(body)), len(snap_re.findall(body))
    n_triv = min(_trivial_count(body, trivial), max(n_assert - n_snap, 0))
    kinds = (["snapshot"] * n_snap + ["trivial"] * n_triv
             + ["real"] * max(n_assert - n_snap - n_triv, 0))
    return kinds + (["real"] if helpers and helpers.search(body) else [])


_SWIFT_FN = re.compile(
    r"(?m)^[ \t]*(?P<attrs>(?:@[\w.]+(?:\([^\n)]*\))?[ \t]+)*)"
    r"(?:(?:public|private|internal|fileprivate|override|final|static|async)[ \t]+)*"
    r"func[ \t]+(?P<name>\w+)[^{;\n]*\{")
_SWIFT_RULES = (
    re.compile(r"XCTAssert\w*|XCTFail|XCTUnwrap|XCTExpectFailure|#expect|#require"
               r"|Issue\.record|\bassert[A-Z]\w*\s*\("),
    re.compile(r"\b(?:assert|verify)\w*Snapshot\w*\s*\("),
    [(re.compile(r"XCTAssert(?:True)?\s*\(\s*true\s*\)|XCTAssertFalse\s*\(\s*false\s*\)"
                 r"|#expect\s*\(\s*(?:true|\d+)\s*\)"), False),
     (re.compile(r"XCTAssertEqual\s*\(\s*%s\s*,\s*%s\s*\)" % (_CONST, _CONST)), False),
     (re.compile(r"XCTAssertNotNil\s*\(\s*(\w+)\s*\)"), True),
     (re.compile(r"#expect\s*\(\s*(\w+)\s*!=\s*nil\s*\)"), True)])


def _swift_test_attr(src, m):
    """True when the func carries `@Test`, inline or on the attribute lines just above it."""
    if "@Test" in m.group("attrs"):
        return True
    start = m.start()
    while start > 0:
        prev_start = src.rfind("\n", 0, start - 1) + 1
        line = src[prev_start:start - 1].strip()
        if not line.startswith("@"):
            return False
        if line.startswith("@Test"):
            return True
        start = prev_start
    return False


def _swift_funcs(src, pairs):
    funcs = []
    for m in _SWIFT_FN.finditer(src):
        end = pairs.get(m.end() - 1)
        if end is None:
            continue
        name = m.group("name")
        funcs.append((name, name.startswith("test") or _swift_test_attr(src, m),
                      src[m.end():end]))
    return funcs


def _scan_swift(text):
    src = _strip(text, False)
    funcs = _swift_funcs(src, _pairs(src))
    helpers = _helper_rx({n for n, is_test, body in funcs
                          if not is_test and "real" in _text_kinds(body, _SWIFT_RULES)})
    out = []
    for name, is_test, body in funcs:
        if not is_test:
            continue
        reason = ("empty body" if not body.strip()
                  else _verdict(_text_kinds(body, _SWIFT_RULES, helpers)))
        if reason and "XCTSkip" in body:
            reason = "skipped"
        if reason:
            out.append((name, reason))
    return out


_JS_CALL = re.compile(
    r"(?<![\w$.])(?P<head>(?:it|test|specify|fit|xit|xtest)"
    r"(?:\.(?:only|skip|todo|concurrent|failing))?)\s*\(")
_JS_ASSERT = (r"(?<![\w$])expect\w*\s*\(|(?<![\w$])assert(?!ions)\w*(?:\.\w+)?\s*\("
              r"|\.should\s*\(")
_JS_SUBJECT = r"expect\s*\(\s*%s\s*\)\s*\.\s*"
_JS_RULES = (
    re.compile(_JS_ASSERT),
    re.compile(r"\.toMatch(?:Inline)?Snapshot\s*\("),
    [(re.compile((_JS_SUBJECT % _CONST) + r"(?:not\s*\.\s*)?(?:toBe|toEqual|toStrictEqual)"
                 r"\s*\(\s*%s\s*\)" % _CONST), False),
     (re.compile((_JS_SUBJECT % _CONST) + r"(?:toBeTruthy|toBeFalsy|toBeDefined)\s*\(\s*\)"),
      False),
     (re.compile((_JS_SUBJECT % r"(\w+)") + r"(?:toBeDefined|toBeTruthy|not\s*\.\s*toBeNull"
                 r"|not\s*\.\s*toBeUndefined)\s*\(\s*\)"), True)])
_JS_FN_DECL = re.compile(r"\bfunction\s+(\w+)\s*\(")
_JS_ARROW_DECL = re.compile(
    r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)\n]*\)|\w+)\s*=>\s*\{")


def _js_helpers(src, pairs):
    """Compiled call pattern for same-file helpers (function / const-arrow with a block body)
    whose body holds a real assertion."""
    decls = set()
    for m in _JS_FN_DECL.finditer(src):
        close = pairs.get(m.end() - 1)
        brace = src.find("{", close, close + 200) if close is not None else -1
        if brace in pairs:
            decls.add((m.group(1), brace))
    for m in _JS_ARROW_DECL.finditer(src):
        if m.end() - 1 in pairs:
            decls.add((m.group(1), m.end() - 1))
    return _helper_rx({n for n, b in decls
                       if "real" in _text_kinds(src[b + 1:pairs[b]], _JS_RULES)})


def _js_body(src, a, b, pairs):
    """The callback body inside it()/test() args src[a:b], or None when it is not inline."""
    arrow, fn = src.find("=>", a, b), src.find("function", a, b)
    if arrow != -1 and (fn == -1 or arrow < fn):
        k = arrow + 2
        while k < b and src[k] in " \t\n":
            k += 1
        if k < b and src[k] == "{":
            close = pairs.get(k)
            return None if close is None else src[k + 1:close]
        return src[arrow + 2:b]
    p = src.find("(", fn, b) if fn != -1 else -1
    close = pairs.get(p) if p != -1 else None
    brace = src.find("{", close, b) if close is not None else -1
    return src[brace + 1:pairs[brace]] if brace in pairs else None


def _js_reason(head, src, a, b, pairs, helpers):
    if "skip" in head or "todo" in head or head in ("xit", "xtest"):
        return "skipped"
    body = _js_body(src, a, b, pairs)
    if body is None:
        return None
    return ("empty body" if not body.strip()
            else _verdict(_text_kinds(body, _JS_RULES, helpers)))


def _scan_js(text):
    src, out = _strip(text, True), []
    pairs = _pairs(src)
    helpers = _js_helpers(src, pairs)
    for m in _JS_CALL.finditer(src):
        end = pairs.get(m.end() - 1)
        if end is None or not src[m.end():end].lstrip().startswith(('"', "'", "`")):
            continue
        reason = _js_reason(m.group("head"), src, m.end(), end, pairs, helpers)
        if reason:
            title = re.match(r"\s*([\"'`])(.*?)\1", text[m.end():end], re.S)
            out.append(((title.group(2) if title else "?").strip()[:80], reason))
    return out


_SCANNERS = {"py": _scan_py, "swift": _scan_swift, "js": _scan_js}


def hollow_test_lines(changed, read):
    """`[]` when no changed file is a test file, else one report line: `hollow-tests: ok` or
    `hollow-tests: advisory - ...`. `read(path)` returns the committed text (empty or None when
    unreadable). Warn only: never raises, and nothing here is a failure."""
    files = [p for p in changed if _kind(p)][:MAX_FILES]
    if not files:
        return []
    found = []
    for path in files:
        try:
            text = read(path) or ""
            hits = _SCANNERS[_kind(path)](text) if len(text) <= MAX_BYTES else []
        except Exception:
            continue     # a file the patterns cannot read is skipped, never a failure
        found += [(path, name, reason) for name, reason in hits]
    if not found:
        return ["hollow-tests: ok"]
    shown = "; ".join(f"`{p}::{n}` ({r})" for p, n, r in found[:SHOW])
    rest = f" (+{len(found) - SHOW} more)" if len(found) > SHOW else ""
    return [f"hollow-tests: advisory - {len(found)} test(s) check nothing: {shown}{rest}"]


if __name__ == "__main__":      # `python3 -m capsule.hollow_tests FILE...`: the same check by hand
    import sys
    print("\n".join(hollow_test_lines(sys.argv[1:], lambda p: open(p, encoding="utf-8").read())))
