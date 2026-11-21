"""Compose observed consumers of independent data and leaf-test edits."""

import ast
from pathlib import PurePosixPath

from .file_effects import materialize
from .test_scope import import_stop_only, trailing_comment_only


def _ids(values):
    mask = 0
    for number in values:
        mask |= 1 << number
    return mask


def _leaf_reads(selector, edit, edits):
    row = selector._test_sources.get(edit.old)
    if not row or not row["leaf"] or row["raw"] is None or row["foreign"] is None:
        return None
    guards = _ids(row["raw"]) | _ids(row["foreign"])
    if trailing_comment_only(edit.before, edit.after):
        return guards
    owned = selector._tests_by_file.get(edit.old)
    if not owned:
        return None
    if import_stop_only(edit.before, edit.after):
        return guards | owned
    if edit.after.startswith(edit.before) and edit.before.endswith("\n"):
        tail = ast.parse(edit.after[len(edit.before):]).body
        if len(tail) == 1 and isinstance(tail[0], ast.ImportFrom):
            stmt = tail[0]
            # A fresh private binding to a sibling test module that now stops at
            # import. Both leaf scopes are retained, including prior importers.
            if stmt.level == 0 and stmt.module == "tests" and len(stmt.names) == 1:
                alias = stmt.names[0]
                target = "tests/" + alias.name + ".py"
                if alias.asname and alias.asname.startswith("_") and any(
                    other.old == other.new == target and import_stop_only(other.before, other.after)
                    for other in edits
                ) and alias.asname not in edit.before:
                    return guards | owned
    # A literal file-read assertion introduced in an existing test method may
    # observe another edit, even if the pristine method never opened that file.
    if _adds_read_assertion(edit):
        return guards | owned
    return None


def _path_expr(node):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path":
        return (len(node.args) == 1 and not node.keywords and
                (type(node.args[0]) is ast.Constant and type(node.args[0].value) is str
                 or isinstance(node.args[0], ast.Name) and node.args[0].id == "__file__"))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "resolve":
        return not node.args and not node.keywords and _path_expr(node.func.value)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
        return (isinstance(node.slice, ast.Constant) and type(node.slice.value) is int
                and _path_expr(node.value.value))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _path_expr(node.left) and isinstance(node.right, ast.Constant) and type(node.right.value) is str
    return False


def _read_args(left, right):
    return (isinstance(left, ast.Constant) and type(left.value) is str and
            isinstance(right, ast.Call) and not right.args and not right.keywords and
            isinstance(right.func, ast.Attribute) and right.func.attr == "read_text"
            and _path_expr(right.func.value))


def _adds_read_assertion(edit):
    original = ast.parse(edit.before, type_comments=True)
    patched = ast.parse(edit.after, type_comments=True)
    matches = 0
    for cls in patched.body:
        if (not isinstance(cls, ast.ClassDef) or cls.decorator_list or cls.keywords
                or len(cls.bases) != 1 or ast.unparse(cls.bases[0]) != "unittest.TestCase"):
            continue
        overrides = {fn.name for fn in cls.body if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if overrides & {"__getattribute__", "__getattr__", "assertIn", "assertNotIn"}:
            continue
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test_") or len(fn.body) < 3:
                continue
            imp, check = fn.body[:2]
            if not (isinstance(imp, ast.ImportFrom) and imp.level == 0 and imp.module == "pathlib"
                    and len(imp.names) == 1 and imp.names[0].name == "Path" and not imp.names[0].asname):
                continue
            if isinstance(check, ast.Assert) and not check.msg and isinstance(check.test, ast.Compare):
                test = check.test
                read = (len(test.ops) == 1 and isinstance(test.ops[0], (ast.In, ast.NotIn))
                        and _read_args(test.left, test.comparators[0]))
            else:
                call = check.value if isinstance(check, ast.Expr) else None
                read = (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == "self"
                        and call.func.attr in {"assertIn", "assertNotIn"} and len(call.args) == 2
                        and not call.keywords and _read_args(*call.args))
            if read:
                del fn.body[:2]
                matches += 1
    return matches == 1 and ast.dump(original) == ast.dump(patched)


def _inert_module(source):
    tree = ast.parse(source, type_comments=True)
    # No body is evaluated until a consumer imports and calls the new function.
    # Exclude annotations, defaults, decorators and other definition-time work.
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and type(node.value.value) is str:
            continue
        if not isinstance(node, ast.FunctionDef) or node.decorator_list or node.returns or getattr(node, "type_params", ()):
            return False
        if node.args.defaults or any(node.args.kw_defaults):
            return False
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs, node.args.vararg, node.args.kwarg]
        if any(arg and arg.annotation for arg in args):
            return False
    compile(source, "<added module>", "exec", dont_inherit=True)
    return True


def _production_module(path):
    return (path.parts[0] == "sqlglot" and path.suffix == ".py"
            and all(part.isidentifier() for part in (*path.parts[:-1], path.stem))
            and path.stem not in {"__init__", "__main__"})


def _structural(selector, edit):
    paths = [PurePosixPath(p) for p in (edit.old, edit.new) if p]
    old_module = False
    active_destination = False
    if all(p.suffix == ".md" and (len(p.parts) == 1 or p.parts[0] == "posts") for p in paths):
        pass
    elif all(p.parts[:2] == ("tests", "fixtures") and p.suffix in {
        ".sql", ".csv", ".json", ".txt", ".yaml", ".yml", ".tsv"
    } for p in paths):
        pass
    elif all(_production_module(p) for p in paths):
        old_module = bool(edit.old)
        if edit.new and not _inert_module(edit.after):
            # Importing a new body can mutate other modules. Even an unchanged
            # relocated body runs again if imported under the destination name.
            active_destination = True
    else:
        return None
    try:
        from .path_observations import path_readers
    except ImportError:
        return None
    observed = path_readers(selector, edit.old, edit.new)
    if observed is None or active_destination and observed:
        return None
    if edit.new and _production_module(PurePosixPath(edit.new)):
        try:
            from .path_observations import new_module_unimported
        except ImportError:
            return None
        if not new_module_unimported(selector, edit.new):
            return None
    if not old_module:
        return observed
    from .module_cold import cold_mask
    cold = cold_mask(selector, edit.old)
    warm = selector._reader_index
    if cold is None or warm is None:
        return None
    if selector._source(edit.old) != edit.before:
        return None
    return (observed | cold | warm.guard | selector._files.get(edit.old, 0)
            | selector._reads.get(edit.old, 0) | selector._collection.get(edit.old, 0))


def file_selection(selector, patch):
    # An ordinary production edit belongs to the existing source analyzer. Skip
    # reading and hashing large modules when this composition cannot accept it.
    for block in patch.split("\ndiff --git "):
        header, _, rest = block.removeprefix("diff --git ").partition("\n")
        old, _, new = header.partition(" ")
        if old.startswith("a/sqlglot/") and old[2:] == new[2:] and not rest.startswith(
            ("new file mode ", "deleted file mode ")
        ):
            return None
    edits = materialize(selector.source_root, patch)
    mask = selector._unmapped
    for edit in edits:
        if edit.old != edit.new:
            bound = _structural(selector, edit)
        elif edit.old in selector._root_documents:
            bound = selector._root_documents[edit.old]
        elif edit.old in selector._test_sources:
            bound = _leaf_reads(selector, edit, edits)
        elif edit.old.startswith("tests/fixtures/") and edit.old in selector._reads:
            if PurePosixPath(edit.old).suffix not in {".sql", ".csv", ".json", ".txt", ".yaml", ".yml", ".tsv"}:
                return None
            bound = selector._reads[edit.old]
        else:
            return None
        if bound is None:
            return None
        mask |= bound
    if len(edits) > 1 and any(edit.old != edit.new for edit in edits):
        from .passive_kernel import passive_prefix_guards

        for edit in edits:
            mask |= passive_prefix_guards(selector, edit)
    return mask
