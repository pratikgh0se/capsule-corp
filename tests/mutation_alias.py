"""Row 526 (card t_1758e0bd item 6): the mutation gate's revert phase follows a one-hop alias.

`tests/mutation_check.py` reverts every changed production file, but only runs the revert check for a
test file that *imports* one of them. A module that re-exports another's names (capsule/unblocked.py:
`dispatcher_state = dispatch_gate.state`) hides the real file: a test saying
`unblocked.dispatcher_state(...)` imports `unblocked`, never `dispatch_gate`, so the gate said "no
production file changed in this range is imported by this test file" and never reverted it.

`reaches` is true when the test imports the production file directly, or imports a module that
aliases something from it AND the test text uses one of those alias names (a test that imports the
facade for another reason stays not applicable, as before). One hop, by design: a transitive import
walk would pull every test of `capsule.tick` into every production change.

Cost: every read is memoised per (repo, rev, path) by `reader`, and `imports_of` per source text, so a
facade is `git show`n and parsed once however many production files or test files ask.

False-positive surface (it errs toward running the revert, never toward skipping it):
 - the alias-name check is `\\bname\\b` over the raw test text, so a name that only appears in a comment
   or a string counts as a use;
 - `from capsule import db` (or paths, config, ...) makes a facade of capsule/__init__.py, so any test
   that imports the package and mentions such a name reaches that file.
A wrongly reached test just gets a revert run; the gate cannot pass a tautology because of it.

Lives here, not in the frozen tests/mutation_check.py (row 514)."""
import ast
import re

_READS, _IMPORTS = {}, {}


def reader(repo, rev, show):
    """`read(path)` for `reaches`: `show(repo, rev, path)` once per (repo, rev, path), misses (None) too."""
    def read(path):
        key = (str(repo), rev, path)
        if key not in _READS:
            _READS[key] = show(repo, rev, path)
        return _READS[key]
    return read


def _dotted(path):
    p = str(path)
    p = p[: -len("/__init__.py")] if p.endswith("/__init__.py") else p[:-3]
    return p.replace("/", ".")


def alias_names(source, prod_dotted):
    """Names `source` binds at module level to something taken from module `prod_dotted`:
    `from prod import x` binds x; `import prod`/`from pkg import prod` binds the module, and
    a top-level `y = prod.attr...` binds y. Relative imports are skipped (too ambiguous here)."""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return set()
    parent, _, leaf = prod_dotted.rpartition(".")
    roots, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.asname for a in node.names if a.name == prod_dotted and a.asname)
            roots.update(a.name for a in node.names if a.name == prod_dotted and not a.asname and "." not in a.name)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            if node.module == prod_dotted:
                names.update(a.asname or a.name for a in node.names)
            elif node.module == parent:
                roots.update(a.asname or a.name for a in node.names if a.name == leaf)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            root = node.value
            while isinstance(root, (ast.Attribute, ast.Call, ast.Subscript)):
                root = root.value if not isinstance(root, ast.Call) else root.func
            if isinstance(root, ast.Name) and root.id in roots:
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _imports(imports_of, src):
    if (imports_of, src) not in _IMPORTS:
        _IMPORTS[(imports_of, src)] = imports_of(src)
    return _IMPORTS[(imports_of, src)]


def reaches(test_imports, test_source, prod_file, read, imports_of, matches):
    """True when the test reaches `prod_file` directly or through one aliasing module.
    `test_imports` is `imports_of(test_source)`; `read(path)` is a file's text at head or None;
    `matches(imports, prod_file)` is mutation_check.references_prod_file."""
    if matches(test_imports, prod_file):
        return True
    prod_dotted = _dotted(prod_file)
    for mod in sorted(test_imports[0]):
        src = read(mod.replace(".", "/") + ".py")
        if src is None or not matches(_imports(imports_of, src), prod_file):
            continue
        if any(re.search(rf"\b{re.escape(n)}\b", test_source) for n in alias_names(src, prod_dotted)):
            return True
    return False
