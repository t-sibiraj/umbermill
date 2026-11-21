"""Find tests affected by patches using evidence from the pinned baseline."""

import ast
import base64
import io
import tokenize
from dataclasses import dataclass, field
from functools import cached_property
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
from types import CodeType

from . import BASE_COMMIT
from .patches import Change, UnsupportedPatch, apply_change, parse_patch
from .selector import Selection


_MAX_MAP_BYTES = 96_000_000
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_STATEMENT_CONTAINERS = (ast.stmt, ast.ExceptHandler, ast.match_case)
_DEFINITION_CHANGES = (
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.FunctionDef,
    ast.Global,
    ast.Import,
    ast.ImportFrom,
    ast.Nonlocal,
    ast.Yield,
    ast.YieldFrom,
)
_DEFINITION_HOOKS = {"__prepare__", "__set_name__", "__init_subclass__"}
_SIMPLE_DECORATORS = {"classmethod", "staticmethod", "property"}
_SQL_FIXTURE_HELPERS = {"load_sql_fixtures", "load_sql_fixture_pairs"}
_PINNED_FIXTURE_HELPERS_SHA = "c0dc285fccd1b8bee4a451f5962118fe771de005843b56e3af4c7136f74c3ed3"
_FILTER_COMMENTS_OUTLINE = ast.dump(ast.parse('''
def _filter_comments(s):
    return "\\n".join([line for line in s.splitlines() if line and not line.startswith("--")])
''').body[0])


@dataclass
class FunctionBody:
    name: str
    first_line: int
    last_line: int
    source_slice: bytes | None = field(compare=False)
    has_definition_hook: bool
    syntax: list[ast.stmt] = field(compare=False)

    @cached_property
    def statements(self) -> str:
        return ast.dump(ast.Module(body=self.syntax, type_ignores=[]))

    def same_statements(self, other) -> bool:
        return (self.source_slice is not None and self.source_slice == other.source_slice
                or self.statements == other.statements)

    def binding_details(self) -> tuple[tuple[str, ...], set[tuple[str, type]]]:
        definitions: list[str] = []
        bindings: set[tuple[str, type]] = set()
        for statement in self.syntax:
            for node in ast.walk(statement):
                if isinstance(node, _DEFINITION_CHANGES):
                    definitions.append(ast.dump(node))
                elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    bindings.add((node.id, type(node.ctx)))
        return tuple(definitions), bindings


@dataclass
class ModuleShape:
    bodies: list[FunctionBody]
    outline: ast.Module
    path: str
    raw: bytes

    @cached_property
    def bytecode(self) -> CodeType:
        # Every path that narrows the roster still validates the compiled
        # program. Full-roster fallbacks have nothing to learn from this step.
        return compile(self.raw, self.path, "exec", dont_inherit=True)

    @cached_property
    def closure_layout(self) -> tuple:
        return _closure_layout(self.bytecode)

    @cached_property
    def loading_checks(self) -> tuple[str, ...]:
        # This projection distinguishes only full-roster diagnostics. The
        # caller needs it only when the complete outline has already changed.
        return tuple(ast.dump(stmt) for stmt in self.outline.body if any(
            _can_interrupt_loading(node) for node in ast.walk(stmt)
        ))

    @cached_property
    def module_load_calls(self) -> set[str]:
        by_name: dict[str, list[FunctionBody]] = {}
        for function in self.bodies:
            by_name.setdefault(function.name, []).append(function)
        pending = _called_names(self.outline.body) & by_name.keys()
        reached: set[str] = set()
        while pending:
            name = pending.pop()
            reached.add(name)
            for function in by_name[name]:
                pending.update((_called_names(function.syntax) & by_name.keys()) - reached)
        return reached


def _called_names(statements: list[ast.stmt]) -> set[str]:
    """Find local calls made while statements execute, including decorators."""
    names: set[str] = set()
    pending: list[ast.AST] = list(statements)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Lambda):
                pending.append(node.func.body)
        if isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            names.update(decorator.id for decorator in node.decorator_list if isinstance(decorator, ast.Name))
        if isinstance(node, _FUNCTIONS):
            pending.extend(node.decorator_list)
            pending.extend(node.type_params)
            pending.append(node.args)
            if node.returns is not None:
                pending.append(node.returns)
        elif isinstance(node, ast.Lambda):
            pending.append(node.args)
        else:
            pending.extend(ast.iter_child_nodes(node))
    return names


def _closure_layout(code: CodeType) -> tuple:
    nested = tuple(_closure_layout(child) for child in code.co_consts if isinstance(child, CodeType))
    return code.co_name, code.co_cellvars, code.co_freevars, nested


def _explicit_boolean_exits(body: list[ast.stmt]) -> tuple[set[bool] | None, bool]:
    """Find explicit Boolean returns and operations that can change state."""
    exits: set[bool] = set()
    can_change_state = False
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, (*_FUNCTIONS, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return):
            if not isinstance(node.value, ast.Constant) or type(node.value.value) is not bool:
                return None, can_change_state
            exits.add(node.value.value)
        if isinstance(node, (ast.Call, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            can_change_state = True
        pending.extend(ast.iter_child_nodes(node))
    return exits, can_change_state


def _changed_progress_signal(changed: list[tuple[FunctionBody, FunctionBody]]) -> bool:
    for before, after in changed:
        old_exits, old_effect = _explicit_boolean_exits(before.syntax)
        if old_exits != {False, True}:
            continue
        new_exits, new_effect = _explicit_boolean_exits(after.syntax)
        if new_exits is not None and len(new_exits) == 1 and (old_effect or new_effect):
            return True
    return False


def _same_outline(before: ast.Module, after: ast.Module) -> bool:
    """Compare parser-generated AST fields without formatting them as text."""
    pending = [(before, after)]
    while pending:
        left, right = pending.pop()
        if left is right:
            continue
        if type(left) is not type(right):
            return False
        if isinstance(left, ast.AST):
            pending.extend((getattr(left, name, None), getattr(right, name, None)) for name in left._fields)
        elif isinstance(left, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right))
        elif isinstance(left, (float, complex)):
            if repr(left) != repr(right):
                return False
        elif left != right:
            return False
    return True


def _describe_module(path: str, source: str) -> ModuleShape:
    # Compile bytes to honor the same encoding declarations as Python's loader.
    raw = source.encode("utf-8")
    tree = ast.parse(raw, filename=path, type_comments=True)
    # Only use the shortcut when the Python loader and the stored bytes agree
    # on UTF-8. Include whole lines (including type comments) in the comparison.
    encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0]
    lines = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n") if encoding in {"utf-8", "utf-8-sig"} else None
    bodies: list[FunctionBody] = []

    class RemoveBodies(ast.NodeTransformer):
        def generic_visit(self, node):
            # Function definitions can occur in statement suites, including
            # handlers and match arms, and cannot occur in expressions.
            for child in ast.iter_child_nodes(node):
                if isinstance(child, _STATEMENT_CONTAINERS):
                    self.visit(child)
            return node

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            bodies.append(
                FunctionBody(
                    node.name,
                    body[0].lineno,
                    node.end_lineno,
                    b"\n".join(lines[node.lineno - 1:node.end_lineno]) if lines is not None else None,
                    node.name in _DEFINITION_HOOKS or any(
                        not isinstance(decorator, ast.Name) or decorator.id not in _SIMPLE_DECORATORS
                        for decorator in node.decorator_list
                    ),
                    body,
                )
            )
            # A function docstring, unlike an ordinary statement in the body,
            # can affect a test that merely inspects the function object.
            if ast.get_docstring(node, clean=False) is not None:
                node.body = [body[0]]
            else:
                node.body = []
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

    outline = RemoveBodies().visit(tree)
    return ModuleShape(bodies, outline, path, raw)


def _can_interrupt_loading(node):
    if isinstance(node, (ast.Import, ast.ImportFrom, ast.Raise, ast.Assert)):
        return True
    if isinstance(node, ast.Call):
        return (isinstance(node.func, ast.Name) and node.func.id in {"__import__", "exec"}
                or isinstance(node.func, ast.Attribute) and node.func.attr in {"import_module", "exec_module"})
    return False


def _valid_path(path: str) -> bool:
    if not isinstance(path, str) or not path or "\\" in path:
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and all(part not in ("/", "..") for part in parts) and path == "/".join(parts)


def _changed_statement_ranges(
    before: list[ast.stmt], after: list[ast.stmt], *, guarded: bool = False
) -> set[tuple[int, int]] | None:
    """Use only structural replacements. Insertion and deletion need wider coverage."""
    touched: set[tuple[int, int]] = set()

    def replaced(old, new, statement=None) -> bool:
        if isinstance(old, ast.stmt):
            statement = old
        if isinstance(old, list) and isinstance(new, list):
            return len(old) == len(new) and all(replaced(x, y, statement) for x, y in zip(old, new))
        if guarded and isinstance(old, ast.Constant) and isinstance(new, ast.Constant):
            if ast.dump(old) == ast.dump(new):
                return True
            if statement is None or type(old.value) is not type(new.value):
                return False
            touched.add((statement.lineno, statement.end_lineno))
            return True
        if isinstance(old, ast.AST) and type(old) is type(new):
            return all(replaced(getattr(old, name), getattr(new, name), statement) for name in old._fields)
        if type(old) is type(new) and old == new:
            return True
        if statement is None:
            return False
        if guarded:
            # Substituting one operator of the same family keeps its enclosing
            # old statement as a point every affected execution must reach.
            families = (ast.boolop, ast.cmpop, ast.operator, ast.unaryop)
            if not any(isinstance(old, family) and isinstance(new, family) for family in families):
                return False
        touched.add((statement.lineno, statement.end_lineno))
        return True

    return touched if replaced(before, after) else None


def _test_hits(sites: dict[int, int], first: int, last: int) -> int:
    hits = 0
    for line in range(first, last + 1):
        hits |= sites.get(line, 0)
    return hits


def _accepts_fixture_projection(source: str) -> bool:
    module = ast.parse(source)
    functions = {}
    for node in module.body:
        if isinstance(node, ast.FunctionDef):
            if node.name in functions:
                return False
            functions[node.name] = node
    filter_function = functions.get("_filter_comments")
    if filter_function is None or ast.dump(filter_function) != _FILTER_COMMENTS_OUTLINE:
        return False
    for name in _SQL_FIXTURE_HELPERS:
        helper = functions.get(name)
        if helper is None or not helper.body or not isinstance(helper.body[0], ast.With):
            return False
        with_block = helper.body[0]
        if len(with_block.items) != 1:
            return False
        file = with_block.items[0].optional_vars
        if not isinstance(file, ast.Name):
            return False
        reads = [
            node
            for node in ast.walk(with_block)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == file.id
        ]
        if len(reads) != 1:
            return False
        expected = ast.dump(ast.parse(f"_filter_comments({file.id}.read())", mode="eval").body)
        if not any(isinstance(node, ast.Call) and ast.dump(node) == expected for node in ast.walk(with_block)):
            return False
    return True


def _project_sql_fixture(source: str) -> list[str]:
    # The verified loader filters full comment lines and empty lines. Keep
    # every other character, including indentation and inline SQL comments.
    return [line for line in source.splitlines() if line and not line.startswith("--")]


def _paired_inputs(lines: list[str]) -> list[str]:
    """Keep the trimmed halves of each complete pair consumed by the loader."""
    parts = "\n".join(lines).split(";")
    return [part.strip() for part in parts[:len(parts) - len(parts) % 2]]


def _yielded_lines(lines: list[str]) -> list[str]:
    """The single-statement loader holds trailing metadata until it sees SQL."""
    end = len(lines)
    while end and lines[end - 1].startswith("#"):
        end -= 1
    return lines[:end]


class ImpactSelector:
    """Select observed dependents, widening when a change exceeds the evidence."""

    def __init__(
        self,
        nodeids: dict[str, int],
        maps_path: Path | None,
        source_root: Path | None,
        *,
        granularity: str = "file",
    ) -> None:
        if granularity not in ("file", "function", "statement"):
            raise ValueError("unknown impact granularity")
        self.all_tests = sorted(nodeids.values())
        self.source_root = source_root.resolve() if source_root is not None else None
        self.granularity = granularity
        self.unavailable = "no verified impact map"
        self._sources: dict[str, str] = {}
        self._shapes: dict[str, ModuleShape] = {}
        self._fixture_projection_checked: bool | None = None
        self._tests_by_file: dict[str, int] = {}
        for nodeid, number in nodeids.items():
            file = nodeid.split("::", 1)[0]
            self._tests_by_file[file] = self._tests_by_file.get(file, 0) | (1 << number)
        if maps_path is None or self.source_root is None:
            return
        try:
            self._read_map(maps_path, nodeids)
        except (OSError, EOFError, TypeError, ValueError, RecursionError) as exc:
            self.unavailable = f"invalid impact map: {exc}"

    def _read_map(self, path: Path, nodeids: dict[str, int]) -> None:
        open_map = gzip.open if path.suffix == ".gz" else open
        with open_map(path, "rb") as stream:
            data = stream.read(_MAX_MAP_BYTES + 1)
        if len(data) > _MAX_MAP_BYTES:
            raise ValueError("impact map exceeds the uncompressed size limit")
        payload = json.loads(data)
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
            or payload.get("base_commit") != BASE_COMMIT
            or payload.get("complete") is not True
        ):
            raise ValueError("unsupported or incomplete baseline map")
        roster = sorted(nodeids, key=nodeids.__getitem__)
        if payload.get("roster") != roster or self.all_tests != list(range(len(roster))):
            raise ValueError("the runtime roster must match the baseline map")
        mapped = payload.get("mapped")
        if not isinstance(mapped, list) or len(mapped) != len(roster) or any(type(flag) is not bool for flag in mapped):
            raise ValueError("invalid test mapping flags")
        self._unmapped = sum(1 << number for number, covered in enumerate(mapped) if not covered)

        def decode_mask(value):
            if not isinstance(value, str):
                raise ValueError("invalid encoded mask")
            mask = int.from_bytes(base64.b64decode(value, validate=True), "little")
            if mask.bit_length() > len(roster):
                raise ValueError("test bitset exceeds runtime roster")
            return mask

        table = payload.get("mask_table", [])
        if not isinstance(table, list):
            raise ValueError("invalid mask table")
        masks = [decode_mask(value) for value in table]

        def numbers(items):
            if type(items) is int:
                if not 0 <= items < len(masks):
                    raise ValueError("invalid coverage mask index")
                return masks[items]
            if isinstance(items, str):
                return decode_mask(items)
            if not isinstance(items, list) or any(
                type(number) is not int or not 0 <= number < len(roster) for number in items
            ):
                raise ValueError("invalid baseline test numbers")
            mask = 0
            for number in items:
                mask |= 1 << number
            return mask

        self._files = self._read_path_map(payload, "files", numbers)
        self._collection = self._read_path_map(payload, "collection", numbers)
        self._reads = self._read_path_map(payload, "reads", numbers)
        self._filtered_fixture_reads = self._read_path_map(payload, "filtered_fixture_reads", numbers) if "filtered_fixture_reads" in payload else {}
        self._paired_fixture_reads = self._read_path_map(payload, "paired_fixture_reads", numbers) if "paired_fixture_reads" in payload else {}

        def sites(lines):
            if not isinstance(lines, dict):
                raise ValueError("invalid coverage sites")
            result = {}
            for line, items in lines.items():
                if not line.isascii() or not line.isdecimal() or int(line) < 1:
                    raise ValueError("invalid coverage line number")
                result[int(line)] = numbers(items)
            return result

        self._sites = self._read_path_map(payload, "sites", sites)
        self._collection_sites = self._read_path_map(payload, "collection_sites", sites) if "collection_sites" in payload else {}
        self._source_hashes = self._read_path_map(payload, "source_hashes")
        if any(
            not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            for digest in self._source_hashes.values()
        ):
            raise ValueError("invalid source digest")
        # Absence from a completed, pinned initialization run is relevant only
        # to compiled deferred code. Missing evidence keeps the existing fallback.
        self._relation_sites = payload.get("relation_sites", {})
        if not isinstance(self._relation_sites, dict):
            raise ValueError("invalid comparison sites")
        self._reader_index = None
        if path.with_name("readers.json.gz").is_file():
            from .readers import ReaderIndex
            self._reader_index = ReaderIndex(path.with_name("readers.json.gz"), BASE_COMMIT, len(roster))
        self._position_readers = self._read_path_map(payload,"position_readers",numbers) if "position_readers" in payload else {}
        self._deferred = {}
        if "deferred_baseline" in payload:
            def deferred_record(record):
                if not isinstance(record, list) or len(record) != 2 or not isinstance(record[0], list):
                    raise ValueError("invalid deferred metadata")
                if any(not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str)
                       or type(item[1]) is not int or item[1] < 1 for item in record[0]):
                    raise ValueError("invalid eager code identity")
                return {tuple(item) for item in record[0]}, numbers(record[1])
            self._deferred = self._read_path_map(payload, "deferred_baseline", deferred_record)
        self.unavailable = ""

    @staticmethod
    def _read_path_map(payload, key, parse=lambda value: value):
        index = payload.get(key)
        if not isinstance(index, dict) or any(not _valid_path(path) for path in index):
            raise ValueError(f"invalid {key} path map")
        return {path: parse(value) for path, value in index.items()}

    def _source(self, path: str) -> str:
        if path not in self._sources:
            source = (self.source_root / path).resolve()
            if not source.is_relative_to(self.source_root) or not source.is_file():
                raise UnsupportedPatch("pinned source file is missing")
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != self._source_hashes.get(path):
                raise UnsupportedPatch("pinned source did not match its baseline map")
            self._sources[path] = data.decode("utf-8")
        return self._sources[path]

    def _all(self, reason: str) -> Selection:
        return Selection(self.all_tests.copy(), reason, True)

    def select(self, patch: str) -> Selection:
        if self.unavailable:
            return self._all(self.unavailable)
        selected = self._unmapped
        reasons: list[str] = []
        parts = []
        try:
            changes = parse_patch(patch)
            paths = {change.old_path for change in changes}
            if len(paths) != len(changes):
                raise UnsupportedPatch("duplicate old path in a patch")
            for change in changes:
                tests, reason = self._change_tests(change, allow_fixture_projection="tests/helpers.py" not in paths,
                                                   single_file=len(changes) == 1)
                selected |= tests
                reasons.append(reason)
                parts.append((change, tests, reason))
            if "source positions may affect file dependents" in reasons:
                from .position import qualify_parts
                alternative = qualify_parts(self, parts, mode='observed_composite_class', overrides=self._position_readers)
                if alternative is not None:
                    selected, qualified = alternative
                    reasons = ["position-only source with observed readers"] if qualified else reasons
        except (OSError, UnsupportedPatch, SyntaxError, UnicodeError, ValueError, RecursionError) as exc:
            return self._all(f"uncertain impact: {exc}")
        tests = [number for number in self.all_tests if selected & (1 << number)]
        return Selection(tests, ", ".join(dict.fromkeys(reasons)), False)

    def _change_tests(self, change: Change, *, allow_fixture_projection: bool, single_file: bool = True) -> tuple[int, str]:
        path = change.old_path
        if change.added or change.deleted or path != change.new_path:
            raise UnsupportedPatch("file added, removed, or renamed")
        if not path.startswith(("sqlglot/", "tests/")):
            raise UnsupportedPatch("unmodeled configuration or coverage scope")
        if PurePosixPath(path).name == "conftest.py":
            raise UnsupportedPatch("global fixture configuration changed")
        source = self._source(path)
        replacement = apply_change(source, change)
        if source == replacement:
            return 0, "identical source"
        readers = self._reads.get(path, 0)
        projected_readers = None
        if single_file and self._reader_index and self.granularity == "function" and path.startswith("sqlglot/"):
            projected_readers = self._reader_index.changed(path, source, replacement)
        if projected_readers is not None:
            readers = projected_readers
        if single_file and (path in self._deferred or projected_readers is not None) and len(change.old_lines) == 1 and self._relation_sites:
            line = next(iter(change.old_lines))
            if str(line) in self._relation_sites.get(path, {}):
                from .relation import one_relation
                proof = one_relation(path, source, replacement)
                if proof is not None:
                    site, operator = proof
                    _, row, column = site.split("|")
                    alts = self._relation_sites[path].get(row, {}).get(column)
                    if isinstance(alts, list) and len(alts) == 8 and isinstance(alts[operator], str):
                        affected = int.from_bytes(base64.b64decode(alts[operator], validate=True), "little")
                        if affected.bit_length() <= len(self.all_tests):
                            return affected | readers | (0 if projected_readers is not None else self._deferred[path][1]), "baseline operand comparison outcomes and observers"
        if not path.endswith(".py"):
            if not readers:
                raise UnsupportedPatch("data file has no recorded consumers")
            filtered = self._filtered_fixture_reads.get(path, 0)
            if allow_fixture_projection and path.endswith(".sql") and filtered:
                before = _project_sql_fixture(source)
                after = _project_sql_fixture(replacement)
                if self._fixture_projection_checked is None:
                    try:
                        self._fixture_projection_checked = _accepts_fixture_projection(self._source("tests/helpers.py"))
                    except (OSError, UnsupportedPatch, SyntaxError, UnicodeError):
                        self._fixture_projection_checked = False
                if self._fixture_projection_checked and before == after:
                    return readers & ~filtered, "unchanged SQL fixture input for verified filtered readers"
                if (single_file and self._fixture_projection_checked
                        and self._source_hashes.get("tests/helpers.py") == _PINNED_FIXTURE_HELPERS_SHA
                        and _paired_inputs(before) == _paired_inputs(after)):
                    unchanged = filtered & self._paired_fixture_reads.get(path, 0)
                    if _yielded_lines(before) == _yielded_lines(after):
                        unchanged = filtered
                    if unchanged:
                        return readers & ~unchanged, "unchanged emitted records for verified fixture readers"
            return readers, "observed data consumers"

        # Test collection, initialization, direct imports, and fixture execution
        # can all change behavior even when the edited line has no trace hits.
        file_tests = (
            self._files.get(path, 0)
            | self._collection.get(path, 0)
            | self._tests_by_file.get(path, 0)
            | readers
        )
        if path not in self._shapes:
            self._shapes[path] = _describe_module(path, source)
        old = self._shapes[path]
        new = _describe_module(path, replacement)
        uncertain_load = None
        changed = []
        if not _same_outline(old.outline, new.outline):
            if old.loading_checks != new.loading_checks:
                uncertain_load = "changed module-load imports or failure checks can affect the full roster"
            else:
                uncertain_load = "changed definition-time values or declarations can affect indirect consumers"
        else:
            changed = [(before, after) for before, after in zip(old.bodies, new.bodies, strict=True)
                       if not before.same_statements(after)]
            if _changed_progress_signal(changed):
                raise UnsupportedPatch("collapsed Boolean outcomes with possible state changes can affect caller progress")
            collection_sites = self._collection_sites.get(path, {})
            for before, _ in changed:
                if (_test_hits(collection_sites, before.first_line, before.last_line)
                        or before.name in _DEFINITION_HOOKS or before.name in old.module_load_calls):
                    uncertain_load = "changed function can run during module loading or test collection"
                    break
        if uncertain_load is not None:
            if single_file and self.granularity == "function" and path in self._deferred and path.startswith("sqlglot/"):
                from .deferred import callback_change, warning_free
                callback = callback_change(old.bytecode, new.bytecode)
                if callback is not None:
                    identity, lines, exclusive = callback
                    eager, text_readers = self._deferred[path]
                    collection = self._collection_sites.get(path, {})
                    if identity not in eager and all(line not in collection for line in exclusive):
                        sites = self._sites.get(path, {})
                        callers = 0
                        for line in lines:
                            callers |= sites.get(line, 0)
                        if callers and warning_free(path, source) and warning_free(path, replacement):
                            return callers | readers | text_readers, "observed deferred callback and source readers"
            raise UnsupportedPatch(uncertain_load)
        if not file_tests:
            raise UnsupportedPatch("Python file has no recorded consumers")
        old_code = old.bytecode
        new_code = new.bytecode
        if path.startswith("sqlglot/") and not changed and old.bodies == new.bodies and old_code == new_code:
            # The executable representation and its coordinates agree. Readers
            # can still observe the raw source, so preserve their tests.
            return readers, "unchanged executable source plus text readers"
        if self.granularity == "file" or path.startswith("tests/") or path.endswith("/__init__.py"):
            return file_tests, "observed file dependents"
        if old.closure_layout != new.closure_layout:
            return file_tests, "changed closure or class creation"
        sites = self._sites.get(path, {})
        selected = readers
        used_statement_sites = False
        for before, after in changed:
            if before.has_definition_hook or before.binding_details() != after.binding_details():
                return file_tests, "function changes affect binding or execution mode"
            callers = _test_hits(sites, before.first_line, before.last_line)
            if not callers:
                return file_tests, "changed function has no baseline callers"
            statements = _changed_statement_ranges(
                before.syntax, after.syntax, guarded=self.granularity != "statement"
            )
            if statements:
                statement_callers = [_test_hits(sites, first, last) for first, last in statements]
                if all(statement_callers):
                    used_statement_sites = True
                    callers = 0
                    for hits in statement_callers:
                        callers |= hits
            selected |= callers
        if not changed:
            return file_tests, "source positions may affect file dependents"
        return selected, "changed statement callers plus text readers" if used_statement_sites else "changed function callers plus text readers"
