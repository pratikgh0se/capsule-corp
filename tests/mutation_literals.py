"""Card t_bd08469e: two gate gaps for tests/mutation_check.py (frozen, row 514).

1. `mask_strings`: `apply_weakening` and friends regex-matched inside string literals, so a test
   that embeds test source as a string (the gate's own suite, codegen tests) could have its embedded
   text flipped instead of a real assertion: a false BAD. Matching now runs on a same-length copy
   of the text whose STRING tokens (f-string parts included) are blanked; edits still apply to the
   original text at the same offsets. If the text does not tokenize, it is returned unchanged, which
   is the old behaviour, so the mask can only remove false matches, never hide a real assertion.
2. `check_new_modules`: a test whose subject module is new in the range cannot be reverted (there is
   no earlier version), so it read `n/a (new module)` and no revert check ran. Now every top-level
   function and method of each new module is replaced by `return None` at head and the test must
   fail. A run that fails at collection (a module-level call broke) is inconclusive and stays n/a.
   (The caller first tries the new modules kept at head with the other product files reverted.)
3. `is_shell_product_file` / `reaches_change`: shell files under bin/ and maintenance/ are revert
   targets too, and a test reaches one when it names it.
"""
import ast
import io
import re
import tempfile
import tokenize
from pathlib import Path

_STRING_NAMES = ("STRING", "FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")


def mask_strings(text):
    """`text` with every character inside a string literal replaced by a space (newlines kept)."""
    starts = [0]
    for line in text.split("\n"):
        starts.append(starts[-1] + len(line) + 1)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return text
    out = list(text)
    for tok in toks:
        if tokenize.tok_name[tok.type] not in _STRING_NAMES:
            continue
        a = starts[tok.start[0] - 1] + tok.start[1]
        b = starts[tok.end[0] - 1] + tok.end[1]
        for i in range(a, b):
            if out[i] not in "\r\n":
                out[i] = " "
    return "".join(out)


def flip_eq(line, masked_line):
    """`line` with its first ` == ` outside a string literal turned into ` != `."""
    i = masked_line.find(" == ")
    return line if i < 0 else line[:i] + " != " + line[i + 4:]


def _stub_functions(source):
    """(new_source, count): each outermost non-dunder function/method body of `source` replaced by
    `return None`. Unparseable source and one-line defs are left alone."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 0
    lines = source.splitlines(keepends=True)
    spans = []
    scopes = [tree]
    for scope in scopes:
        for child in ast.iter_child_nodes(scope):
            if isinstance(child, ast.ClassDef):
                scopes.append(child)
            elif (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and not child.name.startswith("__") and child.body[0].lineno > child.lineno):
                spans.append(child)
    for node in sorted(spans, key=lambda n: n.body[0].lineno, reverse=True):
        first = node.body[0].lineno - 1
        indent = lines[first][:len(lines[first]) - len(lines[first].lstrip())]
        lines[first:node.end_lineno] = [indent + "return None\n"]
    return "".join(lines), len(spans)


def is_shell_product_file(rel_path):
    """A changed non-`.py` file under bin/ or maintenance/ (never under a tests/ directory): the
    shell side of the product, extensionless bin/ scripts included."""
    parts = str(rel_path).replace("\\", "/").split("/")
    return (parts[0] in ("bin", "maintenance") and "tests" not in parts
            and not parts[-1].endswith(".py"))


def reaches_change(mc, repo, head, head_source, prod_files, data_files, shell_files):
    """True when the test source reaches a changed file: a product module or intent data file as
    `mc` resolves them, or a shell file whose name appears as a whole token (so `lib_prefix.sh`
    does not match `mylib_prefix.sh`)."""
    dotted_names, bare_names = mc._import_names(head_source)
    test_imports = (mc.expand_via_init_reexports(repo, head, dotted_names), bare_names)
    reader = mc.alias.reader(repo, head, mc.show_file)
    return (any(mc.alias.reaches(test_imports, head_source, pf, reader, mc._import_names,
                                 mc.references_prod_file) for pf in prod_files)
            or any(mc.contractdata.references_data_file(head_source, df) for df in data_files)
            or any(re.search(rf"(?<![\w.-]){re.escape(Path(sf).name)}(?!\w)", head_source)
                   for sf in shell_files))


def check_new_modules(mc, repo, base, head, rwt, test_path, prod_files, names, weaken_good, out):
    """Revert check for a test whose subject module is new in the range (`mc` = the mutation_check
    module, `rwt` = the revert worktree, `out` = the output of its failed first run). First the new
    modules are kept at head in `rwt` (everything else stays reverted) and the import chain rerun,
    scored as the main loop scores it. Only when that is inconclusive, in a scratch worktree at
    head, every function of the new modules named in `out` is stubbed and `names` of `test_path`
    rerun. Returns (state, why): a state `mutation_check` renders, False (still passed: BAD) or
    "new_module" (inconclusive - nothing stubbable, or a collection error - n/a)."""
    new_prod_files = [pf for pf in prod_files if mc.file_status(repo, base, head, pf) == "A"]
    for pf in new_prod_files:
        (rwt / pf).write_text(mc.show_file(repo, head, pf), encoding="utf-8")
    try:
        state, extra, _o, chain = mc._resolve_import_chain(
            repo, base, head, rwt, test_path, prod_files, names, weaken_good)
    finally:
        for pf in new_prod_files:
            mc.revert_to_base(repo, base, rwt, pf, "A")
    label = "; ".join([f"new module kept at head: {', '.join(new_prod_files)}"]
                      + [f"{m} kept at head for import of {s}" for m, s in chain])
    if state == "resolved" and (extra != 0 or chain):
        if extra != 0:
            return "symbol_kept_ok", label
        return ("symbol_kept_info" if weaken_good else "symbol_kept_bad"), label
    if state == "addition_explained":
        return "symbol_addition_ok", extra
    if state == "napt":
        return "reverted_symbol_napt", extra
    with tempfile.TemporaryDirectory(prefix="mutation-check-stub-") as tmp:
        wt = mc.make_worktree(repo, head, Path(tmp))
        try:
            total = 0
            for path in (pf for pf in new_prod_files if mc.new_module_import_error(out, [pf])):
                stubbed, count = _stub_functions(mc.show_file(repo, head, path))
                total += count
                (wt / path).write_text(stubbed, encoding="utf-8")
            if not total:
                return "new_module", None
            test_dir = (wt / test_path).parent
            rc, out = mc.run_discover(wt, test_dir, Path(test_path).name, names)
        finally:
            mc.remove_worktree(repo, wt)
    if rc == 0:
        return False, out[-800:]
    if mc.is_collection_error(out):
        return "new_module", out[-800:]
    return "symbol_kept_ok", f"new module stubbed: {', '.join(new_prod_files)}"
