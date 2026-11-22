"""Choose candidates and a replay route from pristine source, collection, and dependency observations."""
from dataclasses import dataclass

from . import execution_plan, field_plan, input_plan, slots_plan, test_scope, time_plan, type_only
from .deferred import callback_change, compile_source
from .doc_readers import source_doc_readers
from .file_selection import file_selection
from .impact import (_DEFINITION_HOOKS, _accepts_fixture_projection, _changed_progress_signal, _changed_statement_ranges,
                     _describe_module, _paired_inputs, _PINNED_FIXTURE_HELPERS_SHA, _project_sql_fixture,
                     _replaced_expression_statement, _same_outline, _test_hits, _yielded_lines)
from .import_snapshot import pristine_collection, run
from .method_plan import select as method_consumers
from .patches import UnsupportedPatch, apply_change, parse_patch
from .position import candidate_trivia, position_only, qualify_parts
from .process_state import uncertain_process_state
from .registry_state import uncertain_registry_state
from .selector import Selection


@dataclass
class Edit:
    change: object
    original: str
    replacement: str

    @property
    def path(self):
        return self.change.old_path


def _mask(numbers):
    bits = 0
    for number in numbers:
        bits |= 1 << number
    return bits


def _source_readers(data, edit):
    if data._reader_index is None or data._isolated_index is None:
        return None
    ordinary = data._reader_index.changed(edit.path, edit.original, edit.replacement, guard=False)
    independent = data._isolated_index.changed(edit.path, edit.original, edit.replacement)
    if ordinary is None or independent is None:
        return None
    return ordinary | independent


def _data_readers(data, edit, single):
    path = edit.path
    if path in data._root_documents:
        return data._root_documents[path]
    if not input_plan._known_fixture(data, path):
        return None
    readers = data._reads[path]
    filtered = data._filtered_fixture_reads.get(path, 0)
    if not filtered or not path.endswith(".sql"):
        return readers
    if not _accepts_fixture_projection(data._source("tests/helpers.py")):
        return readers
    old, new = _project_sql_fixture(edit.original), _project_sql_fixture(edit.replacement)
    if old == new:
        return readers & ~filtered
    if (single and data._source_hashes.get("tests/helpers.py") == _PINNED_FIXTURE_HELPERS_SHA
            and _paired_inputs(old) == _paired_inputs(new)):
        same = filtered if _yielded_lines(old) == _yielded_lines(new) else filtered & data._paired_fixture_reads.get(path, 0)
        return readers & ~same
    return readers


def _test_readers(data, edit):
    row = data._test_sources.get(edit.path)
    if not row or row.get("raw") is None:
        return None
    raw = _mask(row["raw"])
    if test_scope.trailing_comment_only(edit.original, edit.replacement):
        return raw
    if not row["leaf"] or row.get("foreign") is None:
        return None
    owned = data._tests_by_file.get(edit.path, 0)
    if test_scope.import_stop_only(edit.original, edit.replacement):
        return raw | owned | _mask(row["foreign"])
    assertion = test_scope.assertion_owner(edit.original, edit.replacement)
    if assertion and row.get("metadata") is not None:
        cls, method, line = assertion
        entries = row.get("entries", {}).get(f"{cls}.{method}|{line}")
        if entries:
            return raw | _mask(entries) | _mask(row["metadata"]) | _mask(row["foreign"])
    return None


def _collection_agrees(data, edit):
    """Check roster, import origins, handled failures and discovery after replacing this file."""
    if not data._collection.get(edit.path, 0):
        return True
    before = pristine_collection(data.source_root, data._maps_path.with_name("pristine-collection.json"))
    if (not before or before.get("state") != "collected" or before["code"] or before["errors"]
            or not set(data._snapshot_nodes) <= set(before["items"])):
        return False
    name = edit.path[:-3].replace("/", ".")
    after = run(data.source_root, name, edit.path, "collection", edit.replacement)
    if not after or any(before[k] != after.get(k) for k in ("state", "code", "items", "errors", "handled", "modules")):
        return False
    return all(set(before[k]) - {edit.path} == set(after[k]) - {edit.path} for k in ("opened", "attempted"))


def _dispatch(data, edit):
    """Bound one existing class table by its actual lookups after dispatch caches materialize."""
    import ast

    if not edit.path.startswith(("sqlglot/parsers/", "sqlglot/generators/")):
        return None
    catalog, legacy = field_plan._catalog(data._maps_path)
    observed = field_plan._observations(data._maps_path)
    if (not observed or observed["roster"] != legacy["roster_sha256"]
            or observed["count"] != len(data.all_tests)):
        return None
    effects = field_plan._effects(data, edit.change, catalog)
    if not effects or len(effects[0]) != 1 or effects[0][0][0] != "table":
        return None
    effect = effects[0][0]
    _, owner, field, _, _, _ = effect
    # Replacing only the dictionary RHS must explain the entire source change.
    # A removed helper needs its own direct readers and a different plan.
    trees = []
    for source in (edit.original, edit.replacement):
        tree = ast.parse(source, type_comments=True)
        cls, = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == owner.rsplit(".", 1)[1]]
        field_plan._members(cls)[field].value = ast.Constant(None)
        trees.append(ast.dump(tree))
    if trees[0] != trees[1]:
        return None
    readers = _source_readers(data, edit)
    effective = field_plan._effective_readers(data, catalog, legacy, effect, observed)
    if readers is None or effective is None:
        return None
    families = {child.rsplit(".", 2)[-2] for child in catalog["classes"]
                if owner in field_plan._ancestry(child, catalog["classes"])}
    if not families or len(families) > 12:
        return None
    identity = edit.path[:-3].replace("/", ".")
    if run(data.source_root, identity, edit.path, "dialects", edit.replacement, ",".join(sorted(families)), "") != {"state":"ready"}:
        return None
    if not _collection_agrees(data, edit):
        return None
    return (readers | effective | _mask(legacy["guards"]) | _mask(legacy["uninstrumented_tests"])
            | (data._position_readers or {}).get(edit.path, 0))


def _trivia(data, edit):
    if not candidate_trivia(edit.change) or not position_only(edit.path, edit.original, edit.replacement, allow_class_line=True):
        return None
    row = qualify_parts(data, [(edit.change, 0, "source positions may affect file dependents")],
                        mode="observed_class", overrides=data._position_readers)
    return None if row is None else row[0]


def _body(data, edit, single):
    """Start with original statement coordinates; stop when bindings or eager state can escape."""
    original = _describe_module(edit.path, edit.original)
    patched = _describe_module(edit.path, edit.replacement)
    readers = _source_readers(data, edit)
    if readers is None:
        return None
    if len(original.bodies) != len(patched.bodies) or not _same_outline(original.outline, patched.outline):
        return None
    changed = [(a, b) for a,b in zip(original.bodies, patched.bodies) if not a.same_statements(b)]
    if not changed or _changed_progress_signal(changed) or original.closure_layout != patched.closure_layout:
        return None
    covered = data._sites.get(edit.path)
    eager = data._deferred.get(edit.path)
    if covered is None:
        return None
    collection = data._collection_sites.get(edit.path, {})
    result = readers
    for a,b in changed:
        if a.has_definition_hook or a.binding_details() != b.binding_details():
            return None
        first, last = a.first_line, a.last_line
        if (a.name in _DEFINITION_HOOKS or a.name in original.module_load_calls or _test_hits(collection, first, last)
                or (eager and any(first <= line <= last for _, line in eager[0]))):
            return None
        ranges = _changed_statement_ranges(a.syntax, b.syntax, guarded=True)
        if single and len(changed) == 1:
            expression = _replaced_expression_statement(edit.change, a.syntax, b.syntax)
            if expression:
                ranges = expression
        if not ranges or not all(_test_hits(covered, start, end) for start,end in ranges):
            ranges = [(first, last)]
        for start,end in ranges:
            result |= _test_hits(covered, start, end)
        result |= data._isolated_index.executed_ranges(edit.path, ranges)
    return result


def _callable(data, edit):
    readers = _source_readers(data, edit)
    eager = data._deferred.get(edit.path)
    if readers is None or not eager:
        return None
    old, clean_old = compile_source(edit.path, edit.original.encode())
    new, clean_new = compile_source(edit.path, edit.replacement.encode())
    if not clean_old or not clean_new:
        return None
    result = callback_change(old, new, site_lines=True)
    if not result:
        return None
    identity, lines, exclusive, differing = result
    if identity in eager[0] or any(n in data._collection_sites.get(edit.path, {}) for n in exclusive):
        return None
    sites = data._sites.get(edit.path, {})
    points = differing if differing and all(sites.get(n) for n in differing) else lines
    reach = data._isolated_index.executed_ranges(edit.path, [(n,n) for n in points])
    for n in points:
        reach |= sites.get(n,0)
    return readers | reach


def _predicate(data, edit):
    if len(edit.change.old_lines) != 1 or edit.change.inserted_after:
        return None
    readers = _source_readers(data, edit)
    if readers is None:
        return None
    result = execution_plan._outcomes(data, edit.change, edit.original, edit.replacement)
    if result is None:
        return None
    line, = edit.change.old_lines
    return result | readers | data._collection_sites.get(edit.path, {}).get(line, 0)


def _decision(data, changes, patch):
    broad = (1 << len(data.all_tests)) - 1
    structural = input_plan.structural_guard(data, changes, patch)
    if structural is not None:
        return _mask(structural), "shared state", "collection and package changes", True
    edits = []
    for change in changes:
        old = data._source(change.old_path)
        new = apply_change(old, change)
        if old != new:
            edits.append(Edit(change, old, new))
    if not edits:
        return 0, "static", "identical files", False
    single = len(edits) == 1
    product = [e for e in edits if e.path.startswith("sqlglot/") and e.path.endswith(".py")]
    if not product:
        bits = 0
        for edit in edits:
            branch = _test_readers(data, edit) if single and edit.path.startswith("tests/") and edit.path.endswith(".py") else None
            if branch is None:
                branch = _data_readers(data, edit, single)
            if branch is None:
                resolved = file_selection(data, patch)
                if resolved is not None:
                    return resolved, "fixture", "original input and structural observers", False
                return broad, "fresh", "unresolved test or data change", False
            bits |= branch
        basis = "fixture" if any(e.path.startswith("tests/") for e in edits) else "static"
        return bits, basis, "observed source and fixture readers", False

    if len(product) != len(edits):
        parts = []
        for edit in edits:
            if edit.path.startswith("tests/fixtures/") and edit.path.endswith(".sql"):
                readers = _data_readers(data, edit, False)
                if readers is None:
                    break
                parts.append((edit.change, readers, "observed data consumers"))
            elif edit.path.startswith("sqlglot/") and position_only(edit.path, edit.original, edit.replacement,
                                                                    allow_class_line=True):
                parts.append((edit.change, 0, "source positions may affect file dependents"))
            elif edit.path.startswith("sqlglot/"):
                body = _body(data, edit, False)
                if body is None:
                    break
                parts.append((edit.change, body, "changed function callers plus text readers"))
            else:
                break
        if len(parts) == len(edits):
            combined = qualify_parts(data, parts, mode="observed_composite_class", overrides=data._position_readers)
            if combined is not None:
                return combined[0], "fresh", "composed source coordinates, ordinary code and fixture readers", False
        # A modified test can start consuming a changed production file for the first time.
        return broad, "fresh", "mixed test input and production source", False
    layout = slots_plan.select(data, changes, patch)
    if layout is not None:
        return _mask(layout), "layout", "class layout and inherited consumers", False
    if single:
        edit = edits[0]
        position = _trivia(data, edit)
        if position is not None:
            return position, "static", "equivalent code and source-position observers", False
        predicate = _predicate(data, edit)
        if predicate is not None:
            if predicate:
                body = _body(data, edit, True)
                if body is not None:
                    predicate &= body
            return predicate, "hot", "observed original predicate and source readers", False
    if all(e.path.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/")) for e in edits):
        timed = time_plan.select(data, changes, patch)
        if timed is not None:
            return _mask(timed), "fresh", "constructed dialect conversion and lookup observations", False
        if single:
            try:
                keys = _dispatch(data, edits[0])
            except (OSError, ValueError, KeyError, TypeError, SyntaxError, RecursionError):
                keys = None
            if keys is not None:
                return keys, "fresh", "effective class and cache key consumers", False
        methods = method_consumers(data, changes, patch)
        if methods is not None:
            return _mask(methods), "fresh", "effective method changes across dialects", False
    fields = getattr(data, "_field_selector", None)
    literal = fields.narrow(patch) if fields else None
    if literal is not None:
        return literal[0], "fresh", "original class field and reflected consumers", False
    declared = field_plan.select(data, changes, patch)
    if declared is not None:
        return _mask(declared), "fresh", "effective declared fields", False
    erased = type_only.select(data, changes, patch)
    if erased is not None:
        return _mask(erased), "static", "unexecuted annotation-only dependencies", False

    bits = 0
    for edit in edits:
        effect = _trivia(data, edit) if not single else None
        if effect is None:
            effect = _callable(data, edit)
        if effect is None:
            effect = _body(data, edit, single)
        if effect is None:
            # A change in module initialization can publish objects later read in a
            # different file. Cold coverage helps scope definite failures only.
            if single:
                from .import_snapshot import select as import_effect
                imports = import_effect(data, edit.change)
                if imports is not None:
                    return imports[0], "fresh", imports[1], False
            return broad, "fresh", "unresolved initialization or call effects", False
        bits |= effect
    return bits, "hot" if single else "fresh", "changed original statements, callbacks and cold readers", False


def plan_patch(evidence, patch: str) -> Selection:
    """Return one complete plan for this patch; partial file evidence cannot discard other files."""
    try:
        if evidence.unavailable:
            raise UnsupportedPatch(evidence.unavailable)
        if uncertain_process_state(patch, evidence) or uncertain_registry_state(patch, evidence):
            return Selection(list(evidence.all_tests), "shared process or registry state", True, "shared state")
        try:
            changes = parse_patch(patch)
        except UnsupportedPatch:
            bits = file_selection(evidence, patch)
            if bits is None:
                raise
            decision = bits, "static", "observed structural paths and original consumers", False
        else:
            paths = {c.old_path for c in changes}
            git_headers = patch.startswith("diff --git ") + patch.count("\ndiff --git ")
            if len(paths) != len(changes) or git_headers and git_headers != len(changes):
                raise UnsupportedPatch("ambiguous file boundaries")
            decision = _decision(evidence, changes, patch)
        mask, basis, reason, fallback = decision
        mask |= evidence._unmapped | _mask(source_doc_readers(evidence, patch, evidence._snapshot_nodes))
        mask |= _mask(execution_plan.changed_comparisons(evidence, patch))
        if type(mask) is not int or mask < 0 or mask.bit_length() > len(evidence.all_tests):
            raise ValueError("invalid observed roster")
        return Selection([n for n in evidence.all_tests if mask >> n & 1], reason, fallback, basis)
    except (AttributeError, EOFError, IndexError, KeyError, OSError, RecursionError, SyntaxError, TypeError, ValueError):
        return Selection(list(evidence.all_tests), "unsupported source or incomplete evidence", True, "shared state")
