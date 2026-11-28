"""Row 459: does a pure-addition symbol, rather than a weak test, explain a
reverted run's failures? `mutation_check._resolve_import_chain` asks this
before keep-and-rerun, which would erase an independent failure's evidence.

Known gaps (adversarial-only): a runtime-built name (`'A' + 'B'`, `''.join`,
`eval`), a new METHOD reached only via a string `hasattr`/`getattr` beside an
unrelated module-level addition, and a non-test helper named `test*` (binned
as a test, so never in `outside`)."""
import ast
import re

# Card t_a7491776: a pure-addition module can add MORE than one brand-new top-level
# name at once (`paths.resolve_config` plus a second addition, the real repro). The
# import/attribute error that triggers a keep only ever names ONE of them - seeding
# `known_symbols` with just that one name left every OTHER addition's own result block
# reading as an "unknown symbol" to `addition_explains_failures`, which aborts outright
# on the very first one it sees - even with a genuinely independent failure elsewhere in
# the same run, the whole module then falls through to keep-and-rerun, which SUCCEEDS
# (every addition is restored) and reads as a plain pass - "did not fail (BAD)" for a
# file that was, in fact, discriminating. `known_names` seeds the full set instead: once
# a module is judged a pure addition and about to be kept in full, every name IT adds is
# equally "explained", not just the one the regex happened to match. Review round 1, N1:
# an earlier cut duplicated `mutation_check._top_level_names` here (to dodge the import
# cycle at MODULE level - `mutation_check` imports this module at collection time) and
# the duplicate had already drifted (missed names bound inside a top-level `if`/`try`).
# The import only cycles at module-load time; by the time `known_names` is actually
# CALLED, `mutation_check` is fully loaded, so a function-local import reuses the real
# function instead.
def known_names(kept, symbol, base_source, head_source):
    """`known_symbols` for the CURRENT module being judged: every symbol already kept
    for an earlier module in this chain, `symbol` itself (the one the regex matched),
    and every OTHER brand-new top-level name this module's own base/head diff adds -
    all equally "explained" once the whole module is restored."""
    from mutation_check import _top_level_names
    added = _top_level_names(head_source) - _top_level_names(base_source)
    return {s for _, s in kept} | {symbol} | added

RESULT_BLOCK_RE = re.compile(
    r"^(ERROR|FAIL): (\S+).*?\n-{5,}\n(.*?)(?=\n={5,}\n|\n-{5,}\nRan )",
    re.MULTILINE | re.DOTALL,
)
_INSTANCE_ATTR_ERROR_RE = re.compile(r"AttributeError: '\w+' object has no attribute '(\w+)'")


def _mentions(nodes):
    names = {n.id for n in nodes if isinstance(n, ast.Name)}
    names |= {n.attr for n in nodes if isinstance(n, ast.Attribute)}
    strs = [n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    names |= set(strs) | {p for v in strs for p in v.split(".")}  # 'mod.X' patch targets
    return names


def reachable_support(test_src, changed, read):
    """`(test_src, srcs)`: the texts of the changed test-support files the test imports
    directly or through another reachable support file. `read(path)` is a file's text at
    head. A test directory sits on sys.path, so any dotted suffix of the path matches
    (`from fakes import x` reaches tests/fakes.py)."""
    from mutation_check import _import_names as imports_of  # function-local: see known_names
    from mutation_check import is_test_support_file
    support_paths = [f for f in changed if is_test_support_file(f)]

    def matches(imports, path):
        parts = path[:-3].replace("/__init__", "").split("/")
        return any(".".join(parts[i:]) in imports[0] or parts[-1] in imports[1]
                   for i in range(len(parts)))
    seen, todo = set(), [imports_of(test_src or "")]
    while todo:
        imports = todo.pop()
        for path in support_paths:
            if path not in seen and matches(imports, path):
                seen.add(path)
                todo.append(imports_of(read(path) or ""))
    return test_src, [read(path) for path in sorted(seen)]


def collect(test_src, support_srcs):
    """`(referenced_by_test, outside)`: what each `test*` function mentions, and
    what everything else mentions - the rest of the test file plus each changed
    test-support file (never reverted, so it may still name the symbol)."""
    test_tree = ast.parse(test_src or "")
    referenced_by_test, skip = {}, set()
    for node in ast.walk(test_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            sub = list(ast.walk(node))
            skip.update(id(n) for n in sub)
            referenced_by_test.setdefault(node.name, set()).update(_mentions(sub))
    outside = _mentions([n for n in ast.walk(test_tree) if id(n) not in skip])
    for src in support_srcs:
        try:
            outside |= _mentions(list(ast.walk(ast.parse(src or ""))))
        except SyntaxError:  # an unparseable module cannot be imported, so it mentions nothing
            continue
    return referenced_by_test, outside


def addition_explains_failures(out, known_symbols, context, symbol_res):
    """Each result block names a `known_symbols` name (`saw_known`, matched by
    `symbol_res` or an instance AttributeError) or names nothing AND neither
    its test nor `outside` references one (`saw_other`). An unknown symbol
    aborts outright. Needs both."""
    referenced_by_test, outside = context
    saw_known = saw_other = False
    for m in RESULT_BLOCK_RE.finditer(out or ""):
        test_name, body = m.group(2), m.group(3)
        sm = next(filter(None, (r.search(body) for r in (*symbol_res, _INSTANCE_ATTR_ERROR_RE))), None)
        if sm:
            if sm.group(sm.lastindex) not in known_symbols:
                return False
            saw_known = True
        elif not ((referenced_by_test.get(test_name, set()) | outside) & known_symbols):
            saw_other = True
    return saw_known and saw_other
