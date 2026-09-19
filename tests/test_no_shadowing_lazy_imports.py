"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]



SCAN_ROOTS = (REPO / "exitmgr",)
SCAN_ROOT_FILES = sorted(p for p in REPO.glob("*.py"))



_OPTIONAL_STMT = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Match)



def module_level_names(tree: ast.Module) -> set[str]:
    """Public API contract; production-derived narrative omitted."""
    names: set[str] = set()

    def collect(stmts):
        for node in stmts:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name != "*":
                        names.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    names.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                names.update(n.id for n in ast.walk(node.target) if isinstance(n, ast.Name))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                names.update(n.id for n in ast.walk(node.target) if isinstance(n, ast.Name))
                collect(node.body)
                collect(node.orelse)
            elif isinstance(node, (ast.If, ast.While)):
                collect(node.body)
                collect(node.orelse)
            elif isinstance(node, ast.Try):
                collect(node.body)
                collect(node.orelse)
                collect(node.finalbody)
                for h in node.handlers:
                    if h.name:
                        names.add(h.name)
                    collect(h.body)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        names.update(n.id for n in ast.walk(item.optional_vars)
                                     if isinstance(n, ast.Name))
                collect(node.body)

    collect(tree.body)
    return names


def bound_names(imp) -> list[str]:
    """Public API contract; production-derived narrative omitted."""
    return [a.asname or a.name.split(".")[0] for a in imp.names if a.name != "*"]


def iter_functions(node, prefix=""):
    """Public API contract; production-derived narrative omitted."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qual = f"{prefix}{child.name}"
            yield qual, child
            yield from iter_functions(child, prefix=qual + ".")
        elif isinstance(child, ast.ClassDef):
            yield from iter_functions(child, prefix=f"{prefix}{child.name}.")
        else:
            yield from iter_functions(child, prefix=prefix)


def _parent_map(root):
    parents = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _ancestry(node, parents, stop):
    """Public API contract; production-derived narrative omitted."""
    chain, cur = [], node
    while cur is not stop and cur in parents:
        chain.append(cur)
        cur = parents[cur]
    return chain


def _enclosing_stmt(node, parents, func):
    cur = node
    while cur is not func:
        if isinstance(cur, ast.stmt):
            return cur
        cur = parents.get(cur)
        if cur is None:
            return None
    return None


def dominates(imp, read, parents, func) -> bool:
    """Public API contract; production-derived narrative omitted."""
    rstmt = _enclosing_stmt(read, parents, func)
    if rstmt is None:
        return False

    ichain = [n for n in _ancestry(imp, parents, func) if isinstance(n, ast.stmt)]
    rchain = [n for n in _ancestry(rstmt, parents, func) if isinstance(n, ast.stmt)]
    ichain_out = list(reversed(ichain))
    rchain_out = list(reversed(rchain))

    common = 0
    while (common < len(ichain_out) and common < len(rchain_out)
           and ichain_out[common] is rchain_out[common]):
        common += 1
    if common == len(ichain_out) or common == len(rchain_out):

        return False

    i_top, r_top = ichain_out[common], rchain_out[common]
    container = ichain_out[common - 1] if common else func

    shared = None
    for field in ("body", "orelse", "finalbody"):
        lst = getattr(container, field, None)
        if isinstance(lst, list) and any(x is i_top for x in lst) and any(x is r_top for x in lst):
            shared = lst
            break
    if shared is None:


        return False
    if shared.index(i_top) >= shared.index(r_top):
        return False





    for anc in ichain:
        if isinstance(anc, _OPTIONAL_STMT):
            return False
        if anc is i_top:
            break
    return True


def shadowing_sites(path: Path) -> list[dict]:
    """Public API contract; production-derived narrative omitted."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_names = module_level_names(tree)
    parents = _parent_map(tree)
    out: list[dict] = []

    for qual, func in iter_functions(tree):
        declared = set()
        for n in ast.walk(func):
            if isinstance(n, (ast.Global, ast.Nonlocal)):
                declared.update(n.names)

        imports, stack = [], list(func.body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                imports.append(n)
            stack.extend(ast.iter_child_nodes(n))

        for imp in imports:
            for name in bound_names(imp):
                if name not in module_names or name in declared:
                    continue
                reads = [n for n in ast.walk(func)
                         if isinstance(n, ast.Name) and n.id == name
                         and isinstance(n.ctx, ast.Load)]
                unsafe = sorted({r.lineno for r in reads
                                 if not dominates(imp, r, parents, func)})
                out.append({
                    "file": str(path.relative_to(REPO)),
                    "line": imp.lineno,
                    "func": qual,
                    "name": name,
                    "unsafe_read_lines": unsafe,
                })
    return out


def _production_files() -> list[Path]:
    files = list(SCAN_ROOT_FILES)
    for root in SCAN_ROOTS:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
            files.extend(Path(dirpath) / f for f in filenames if f.endswith(".py"))
    return sorted(set(files))


def _all_sites() -> list[dict]:
    sites = []
    for f in _production_files():
        sites.extend(shadowing_sites(f))
    return sites









KNOWN_SHADOWING_SITES = frozenset({
    ("exitmgr/exec_capture.py", "capture_external_fills_blocking", "asyncio"),
    ("exitmgr/manager.py", "ExitManager._post_exit_alert", "os"),
    ("exitmgr/manager.py", "ExitManager._post_reconcile_block_alert", "os"),
    ("exitmgr/manager.py", "ExitManager._post_reconcile_block_alert", "time"),
    ("exitmgr/rules.py", "days_to_expiry", "datetime"),
})

_REMEDY = (
    "\nFIX (either one, one line):\n"
    "  1. alias the local import so it cannot collide:  import x.y as _x_local\n"
    "  2. hoist the import to module level, unless it is lazy on purpose (import cycle,\n"
    "     heavy/optional dependency).\n"
    "See this module's docstring for the `_evcap` incident that motivated the rule."
)



def test_no_function_local_import_shadows_a_name_read_above_it():
    """Public API contract; production-derived narrative omitted."""
    reachable = [s for s in _all_sites() if s["unsafe_read_lines"]]
    assert not reachable, (
        "UnboundLocalError waiting to happen -- a function-local import rebinds a module-level\n"
        "name that the same function reads on a path that can run BEFORE the import:\n"
        + "\n".join(
            "  {file}:{line}  {func}()  rebinds `{name}`, which is read at line(s) {reads}"
            .format(reads=s["unsafe_read_lines"], **s) for s in reachable)
        + _REMEDY
    )



def test_the_set_of_shadowing_imports_does_not_grow():
    """Public API contract; production-derived narrative omitted."""
    found = {(s["file"], s["func"], s["name"]) for s in _all_sites()}
    added = found - KNOWN_SHADOWING_SITES
    assert not added, (
        "new function-local import(s) shadowing a module-level name:\n"
        + "\n".join(f"  {f}::{fn}()  rebinds `{n}`" for f, fn, n in sorted(added))
        + "\n\nThis is harmless ONLY as long as nothing above it reads the name. That is exactly\n"
          "how the exit assessor was silently switched off once already."
        + _REMEDY
    )


def test_the_inventory_has_no_stale_entries():
    """Public API contract; production-derived narrative omitted."""
    found = {(s["file"], s["func"], s["name"]) for s in _all_sites()}
    gone = KNOWN_SHADOWING_SITES - found
    assert not gone, (
        "these shadowing sites are fixed -- delete them from KNOWN_SHADOWING_SITES so the\n"
        "ratchet keeps ratcheting:\n"
        + "\n".join(f"  {f}::{fn}()  `{n}`" for f, fn, n in sorted(gone))
    )






def _verdicts(src: str) -> dict[tuple[str, str], list[int]]:
    """Public API contract; production-derived narrative omitted."""
    tree = ast.parse(src)
    module_names = module_level_names(tree)
    parents = _parent_map(tree)
    out = {}
    for qual, func in iter_functions(tree):
        declared = {n for x in ast.walk(func)
                    if isinstance(x, (ast.Global, ast.Nonlocal)) for n in x.names}
        imports, stack = [], list(func.body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                imports.append(n)
            stack.extend(ast.iter_child_nodes(n))
        for imp in imports:
            for name in bound_names(imp):
                if name not in module_names or name in declared:
                    continue
                reads = [n for n in ast.walk(func)
                         if isinstance(n, ast.Name) and n.id == name
                         and isinstance(n.ctx, ast.Load)]
                out[(qual, name)] = sorted({r.lineno for r in reads
                                            if not dominates(imp, r, parents, func)})
    return out


EVCAP_SHAPE = '''
import event_capture as _evcap

def run(self):
    _evcap.record("open")          # line 5 -- 300 lines earlier, in the real thing
    do_work()
    import event_capture as _evcap  # the "cleanup" that switched the assessor off
    _evcap.record("close")
'''

CASES = {
    "the historical _evcap shape is caught": (EVCAP_SHAPE, ("run", "_evcap"), [5]),
    "first statement is safe": ('''
import os

def f():
    import os
    return os.getcwd()
''', ("f", "os"), []),
    "docstring then import is safe": ('''
import os

def f():
    """doc."""
    import os
    return os.getcwd()
''', ("f", "os"), []),
    "read above the import is caught": ('''
import os

def f():
    x = os.sep
    import os
    return x
''', ("f", "os"), [5]),
    "import inside an if proves nothing later": ('''
import json

def f(flag):
    if flag:
        import json
    return json.dumps({})
''', ("f", "json"), [7]),
    "import in the try, read in the except": ('''
import json

def f():
    try:
        import json
        return json.dumps({})
    except Exception:
        return json.JSONDecodeError
''', ("f", "json"), [9]),
    "loop body: a read above the import breaks on iteration one": ('''
import time

def f(items):
    for it in items:
        t = time.time()
        import time
        del t
''', ("f", "time"), [6]),
    "read inside a nested def declared above the import": ('''
import os

def f():
    def inner():
        return os.sep
    import os
    return inner
''', ("f", "os"), [6]),
    "nested def below the import is fine": ('''
import os

def f():
    import os
    def inner():
        return os.sep
    return inner
''', ("f", "os"), []),
    "global declaration is not a shadow": ('''
import os

def f():
    global os
    y = os
    import os
    return y
''', ("f", "os"), None),
    "a distinct alias is not a shadow": ('''
import event_capture as _evcap

def f():
    _evcap.record("open")
    import event_capture as _evcap_local
    return _evcap_local
''', ("f", "_evcap_local"), None),
    "an import of a name the module never binds is not a shadow": ('''
def f():
    x = 1
    import os
    return os, x
''', ("f", "os"), None),
}


@pytest.mark.parametrize("label", sorted(CASES))
def test_the_analysis_itself(label):
    src, key, expected = CASES[label]
    verdicts = _verdicts(src)
    if expected is None:
        assert key not in verdicts, f"{label}: should not be reported as a shadowing site"
    else:
        assert key in verdicts, f"{label}: site was not detected at all"
        assert verdicts[key] == expected, f"{label}: wrong reachability verdict"


def test_gate_one_actually_fails_on_the_evcap_shape(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    bad = tmp_path / "poisoned.py"
    bad.write_text(EVCAP_SHAPE, encoding="utf-8")
    here = sys.modules[__name__]
    monkeypatch.setattr(here, "REPO", tmp_path)
    monkeypatch.setattr(here, "SCAN_ROOTS", ())
    monkeypatch.setattr(here, "SCAN_ROOT_FILES", [bad])
    with pytest.raises(AssertionError) as exc:
        test_no_function_local_import_shadows_a_name_read_above_it()
    assert "_evcap" in str(exc.value) and "poisoned.py" in str(exc.value)
