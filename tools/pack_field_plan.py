"""Index class declarations and lexical consumers in the pristine Python source tree."""
import ast
import gzip
import json
from pathlib import Path
import sys


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted(node.value)
        return parent + "." + node.attr if parent else ""
    return ""


def imports(tree):
    result = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for item in node.names:
                result[item.asname or item.name] = node.module + "." + item.name
        elif isinstance(node, ast.Import):
            for item in node.names:
                result[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
    return result


def observe_shape(tree, relative, shape_sites, published):
    tables = {"FUNCTIONS", "FUNCTION_PARSERS", "NO_PAREN_FUNCTIONS", "TRANSFORMS"}
    parents = {node: parent for parent in ast.walk(tree) for node in ast.iter_child_nodes(parent)}
    for field in ast.walk(tree):
        if not isinstance(field, ast.Attribute) or field.attr not in tables or not isinstance(field.ctx, ast.Load):
            continue
        parent = parents.get(field)
        if isinstance(parent, ast.Attribute) and parent.value is field and parent.attr == "get":
            continue
        if isinstance(parent, ast.Subscript) and parent.value is field:
            continue
        # keys(), membership, len(), iteration, copies, passing and aliasing
        # all expose more of the container than a successful keyed lookup.
        exposes_shape = (
            isinstance(parent, (ast.Compare, ast.For, ast.comprehension, ast.Dict))
            or isinstance(parent, ast.Attribute) and parent.attr in
            {"keys", "items", "values", "copy", "popitem", "clear", "update"}
            or isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
            and parent.func.id in {"len", "iter", "reversed", "list", "tuple", "dict", "set", "sorted"}
        )
        if relative.startswith("tests/") or exposes_shape:
            shape_sites.setdefault(field.attr, {}).setdefault(relative, set()).add(field.lineno)
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            if any(isinstance(t, (ast.Subscript, ast.Attribute)) for t in targets):
                published.setdefault(field.attr, set()).add(relative)


def build(source):
    classes, lookups, factories, expressions, private = {}, {}, set(), {}, {}
    shape_sites, published = {}, {}
    roots = ("sqlglot", "tests")
    for folder in roots:
        for path in sorted((source / folder).rglob("*.py")):
            relative = path.relative_to(source).as_posix()
            tree = ast.parse(path.read_bytes())
            module = relative[:-3].replace("/", ".")
            observe_shape(tree, relative, shape_sites, published)
            if folder == "sqlglot":
                for node in tree.body:
                    if module.startswith("sqlglot.expressions.") and isinstance(node, ast.ClassDef):
                        expressions.setdefault(node.name, []).append(module)
                    if isinstance(node, ast.FunctionDef) and not node.decorator_list:
                        body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))]
                        if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Lambda):
                            lam = body[0].value
                            if not lam.args.defaults and not any(lam.args.kw_defaults):
                                factories.add(module + "." + node.name)
            supported = (module.startswith(("sqlglot.dialects.", "sqlglot.parsers.", "sqlglot.generators."))
                         or module in ("sqlglot.parser", "sqlglot.generator"))
            bindings = imports(tree) if supported else {}
            for cls in tree.body if supported else ():
                if not isinstance(cls, ast.ClassDef):
                    continue
                name = module + "." + cls.name
                parent = ""
                if len(cls.bases) == 1:
                    head, _, tail = dotted(cls.bases[0]).partition(".")
                    target = bindings.get(head, module + "." + head)
                    parent = target + ("." + tail if tail else "")
                values = {}
                for stmt in cls.body:
                    target = (stmt.target if isinstance(stmt, ast.AnnAssign) else
                              stmt.targets[0] if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 else None)
                    if isinstance(target, ast.Name) and target.id.isupper():
                        value = stmt.value
                        if isinstance(value, ast.Constant) and type(value.value) in (bool, int, str):
                            values[target.id] = [value.value]
                        else:
                            values[target.id] = []
                classes[name] = [parent, values]
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                    key = node.attr
                elif isinstance(node, ast.Constant) and type(node.value) is str:
                    key = node.value
                else:
                    continue
                if key.isidentifier() and key.startswith("_") and not key.startswith("__"):
                    private[key] = private.get(key, 0) + 1
                if key.isupper() and key.isidentifier() and len(key) < 112:
                    lookups.setdefault(key, {}).setdefault(relative, set()).add(node.lineno)
    scalar = {field for _, values in classes.values() for field, value in values.items() if value}
    return {"classes": classes, "factories": sorted(factories),
            "unique_reads": sorted(k for k,v in private.items() if v == 1),
            "shape": {k:{p:sorted(lines) for p,lines in paths.items()} for k,paths in shape_sites.items()},
            "published": {k:sorted(v) for k,v in published.items()},
            "expressions": sorted(k for k,v in expressions.items() if len(v) == 1),
            "readers": {k:{p:sorted(lines) for p,lines in paths.items()} for k, paths in lookups.items() if k in scalar}}


if __name__ == "__main__":
    data = build(Path(sys.argv[1]).resolve())
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    Path(sys.argv[2]).write_bytes(gzip.compress(raw, mtime=0))
