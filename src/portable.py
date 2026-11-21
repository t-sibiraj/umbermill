"""Static fallback using the pinned checkout, without a coverage map.

This deliberately does not guess dependencies from fixture names or patches.
Fixture consumers are discovered from actual test/helper source, propagating
literal arguments to file reads. Dynamic reads widen the result. The caller
must supply the unchanged pinned checkout (as produced by the Docker build).
Static evidence is less complete than recorded reads; runtime trace maps remain
the preferred selector.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path

from .patches import UnsupportedPatch, apply_change, parse_patch
from .selector import Selection


class _SourceReads:
    def __init__(self, root: Path):
        self.root = root
        self.modules = {}
        self.reads = set()
        self.uncertain = False
        self.visited = set()

    def module(self, path):
        if path not in self.modules:
            full = (self.root / path).resolve()
            if not full.is_relative_to(self.root):
                raise ValueError("source escaped pinned checkout")
            tree = ast.parse(full.read_text(encoding="utf-8"), filename=path)
            aliases = {}
            for node in tree.body:
                if isinstance(node, ast.Import):
                    aliases.update((item.asname or item.name, item.name) for item in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        package = path.removesuffix(".py").split("/")[:-node.level]
                        prefix = ".".join(package + ([node.module] if node.module else []))
                    else:
                        prefix = node.module or ""
                    aliases.update((item.asname or item.name, f"{prefix}.{item.name}") for item in node.names)
            self.modules[path] = (tree, aliases)
        return self.modules[path]

    def symbol(self, node, path):
        _, aliases = self.module(path)
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            prefix = self.symbol(node.value, path)
            return f"{prefix}.{node.attr}" if prefix else ""
        return ""

    def value(self, node, path, env, depth=0):
        if node is None or depth > 12:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            if node.id == "__file__":
                return str(self.root / path)
            tree, _ = self.module(path)
            for statement in tree.body:
                if isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id == node.id for t in statement.targets):
                    return self.value(statement.value, path, env, depth + 1)
            return None
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            value = self.value(node.value, path, env, depth + 1)
            return str(Path(value).parent) if value is not None else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
            left = self.value(node.left, path, env, depth + 1)
            right = self.value(node.right, path, env, depth + 1)
            if left is not None and right is not None:
                return str(Path(left) / right) if isinstance(node.op, ast.Div) else left + right
        if isinstance(node, ast.Call):
            name = self.symbol(node.func, path)
            args = [self.value(arg, path, env, depth + 1) for arg in node.args]
            if args and all(arg is not None for arg in args):
                if name in ("Path", "pathlib.Path", "str"):
                    return str(Path(*args)) if name != "str" else args[0]
                if name in ("os.path.join", "posixpath.join"):
                    return str(Path(*args))
                if name in ("os.path.dirname", "posixpath.dirname"):
                    return str(Path(args[0]).parent)
                if name in ("os.path.abspath", "os.path.realpath"):
                    return str(Path(args[0]).resolve())
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("resolve", "absolute"):
                value = self.value(node.func.value, path, env, depth + 1)
                return str(Path(value).resolve()) if value is not None else None
        return None

    def target(self, node, path, owner):
        name = self.symbol(node, path)
        if name.startswith("self.") and owner:
            tree, _ = self.module(path)
            for item in tree.body:
                if isinstance(item, ast.ClassDef) and item.name == owner:
                    for method in item.body:
                        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name == name[5:]:
                            return path, method, owner
        if "." not in name:
            target_path, function = path, name
        else:
            module, _, function = name.rpartition(".")
            target_path = module.replace(".", "/") + ".py"
        if not target_path.startswith("tests/") or not (self.root / target_path).is_file():
            return None
        tree, _ = self.module(target_path)
        for item in tree.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function:
                return target_path, item, None
        return None

    def record_read(self, value):
        if value is None:
            self.uncertain = True
            return
        candidate = Path(value)
        if not candidate.is_absolute():
            # open(relative) depends on the runtime cwd, not the helper file.
            self.uncertain = True
            return
        resolved = candidate.resolve()
        if resolved.is_relative_to(self.root):
            self.reads.add(resolved.relative_to(self.root).as_posix())

    def scan(self, body, path, env=None, owner=None, depth=0):
        env = dict(env or {})
        if depth > 20 or len(self.visited) > 1000:
            self.uncertain = True
            return
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        env[target.id] = self.value(statement.value, path, env)
            for node in ast.walk(statement):
                if not isinstance(node, ast.Call):
                    continue
                name = self.symbol(node.func, path)
                if name in ("open", "io.open", "builtins.open"):
                    argument = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "file"), None)
                    self.record_read(self.value(argument, path, env))
                elif isinstance(node.func, ast.Attribute) and node.func.attr in ("read_text", "read_bytes", "open"):
                    self.record_read(self.value(node.func.value, path, env))
                elif name in ("os.listdir", "os.walk", "glob.glob", "glob.iglob") or (isinstance(node.func, ast.Attribute) and node.func.attr in ("glob", "rglob", "iterdir")):
                    self.uncertain = True
                target = self.target(node.func, path, owner)
                if target:
                    target_path, function, target_owner = target
                    parameters = function.args.posonlyargs + function.args.args
                    if target_owner and parameters and parameters[0].arg == "self":
                        parameters = parameters[1:]
                    bound = {parameter.arg: self.value(value, path, env) for parameter, value in zip(parameters, node.args)}
                    bound.update((keyword.arg, self.value(keyword.value, path, env)) for keyword in node.keywords if keyword.arg)
                    key = (target_path, function.lineno, tuple(sorted(bound.items())))
                    if key not in self.visited:
                        self.visited.add(key)
                        self.scan(function.body, target_path, bound, target_owner, depth + 1)


class PortableSelector:
    """Source-backed fallback when trace maps are unavailable.

    Never uses practice labels. If pinned source is unavailable, every change
    runs every test. Changed test modules run all tests from that module;
    fixture reads use static helper argument propagation, with dynamic readers
    included conservatively.
    """

    def __init__(self, nodeids: dict[str, int], source_root: Path | None = None, *, static_fixtures: bool = False):
        self.nodeids = nodeids
        self.all_tests = sorted(nodeids.values())
        self.root = source_root.resolve() if source_root is not None else None
        # Literal-source tracing cannot establish the absence of dynamically
        # dispatched readers. Keep it opt-in until validated on the checkout.
        self.static_fixtures = static_fixtures
        self._readers = None
        self._dynamic = set()
        self._files = {}
        for nodeid, number in nodeids.items():
            self._files.setdefault(nodeid.split("::", 1)[0], set()).add(number)

    def _all(self, reason):
        return Selection(self.all_tests.copy(), reason, True)

    def _isolated_test_bodies(self, path, before, after):
        """Leave imports, shared helpers, definitions and decorators unchanged."""
        known = {tuple(part.split("[", 1)[0] for part in nodeid.split("::")[1:]) for nodeid in self.nodeids if nodeid.split("::", 1)[0] == path}

        def outline(source):
            tree = ast.parse(source)

            def visit(body, prefix=()):
                for node in body:
                    if isinstance(node, ast.ClassDef):
                        visit(node.body, prefix + (node.name,))
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and prefix + (node.name,) in known:
                        # Collection can inspect docstrings, annotations and
                        # defaults; retain all of those in the comparison.
                        docstring = node.body[:1] if ast.get_docstring(node, clean=False) is not None else []
                        node.body = copy.deepcopy(docstring)

            visit(tree.body)
            return ast.dump(tree)

        return outline(before) == outline(after)

    def _index_reads(self):
        readers = {}
        modules = {}
        for nodeid, number in self.nodeids.items():
            parts = nodeid.split("::")
            path = parts[0]
            scanner = _SourceReads(self.root)
            scanner.modules = modules
            try:
                tree, _ = scanner.module(path)
                scanner.scan(tree.body, path)
                owner = None
                body = tree.body
                for name in parts[1:]:
                    name = name.split("[", 1)[0]
                    selected = next((node for node in body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name), None)
                    if selected is None:
                        raise ValueError("test definition not found in pinned source")
                    if isinstance(selected, ast.ClassDef):
                        owner = selected.name
                        scanner.scan(selected.body, path)
                        for method in selected.body:
                            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name in ("setUp", "setUpClass", "setup_method", "setup_class"):
                                scanner.scan(method.body, path, owner=owner)
                    body = selected.body
                scanner.scan(body, path, owner=owner)
                for fixture in scanner.reads:
                    readers.setdefault(fixture, set()).add(number)
                if scanner.uncertain:
                    self._dynamic.add(number)
            except (OSError, ValueError, SyntaxError, RecursionError):
                self._dynamic.add(number)
        self._readers = readers

    def select(self, patch: str) -> Selection:
        if self.root is None or not self.root.is_dir():
            return self._all("pinned source unavailable")
        selected = set()
        try:
            changes = parse_patch(patch)
            if len({c.old_path for c in changes}) != len(changes):
                raise UnsupportedPatch("duplicate source path")
            for change in changes:
                path = change.old_path
                if change.added or change.deleted or path != change.new_path:
                    raise UnsupportedPatch("structural source change")
                full = (self.root / path).resolve()
                if not full.is_relative_to(self.root):
                    raise UnsupportedPatch("source outside checkout")
                original = full.read_text(encoding="utf-8")
                replacement = apply_change(original, change)
                if path.endswith(".py"):
                    compile(replacement, path, "exec", dont_inherit=True)
                    if path in self._files and Path(path).name != "conftest.py":
                        if not self._isolated_test_bodies(path, original, replacement):
                            raise UnsupportedPatch("test collection, definition, or shared helper changed")
                        # Includes module import/collection failures and shared
                        # test-method state, rather than guessing one method.
                        selected.update(self._files[path])
                    else:
                        raise UnsupportedPatch("production/helper Python needs runtime dependencies")
                elif path.startswith("tests/fixtures/"):
                    if not self.static_fixtures:
                        raise UnsupportedPatch("static fixture inference is experimental and disabled")
                    if self._readers is None:
                        self._index_reads()
                    consumers = self._readers.get(path, set())
                    if not consumers:
                        raise UnsupportedPatch("fixture has no static readers")
                    selected.update(consumers | self._dynamic)
                else:
                    raise UnsupportedPatch("unmodeled file type")
        except (OSError, ValueError, SyntaxError, RecursionError) as exc:
            return self._all(f"portable fallback: {exc}")
        return Selection(sorted(selected), "pinned-source static dependencies", False)
