"""Project dialect configuration and callback table changes onto original field consumers."""
import ast
from functools import lru_cache
import gzip
import hashlib
import json

from .deferred import warning_free
from .module_cold import cold_mask
from .import_snapshot import run
from .patches import UnsupportedPatch, apply_change
from .readers import number


DECLARATIONS_SHA = "07bdafc26ac3a3c07ce9a549506d6549019576a7c3f827132091c5e8cd070e62"
FIELDS_SHA = "28363ad1f70be20597547ef7c5159bb489f8b880be65a4a90066b765b84aae2b"
DISPATCH_SHA = "2b0e84b14b71887323e2a46f17e0f79e18fa63f0486ea074bedf25c5bbc7bdc5"
MISSING = object()


@lru_cache(maxsize=2)
def _observations(path):
    """Only use the exact original-suite evidence shipped for this source and roster."""
    try:
        raw = path.with_name("effective-dispatch.json.gz").read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != DISPATCH_SHA:
        return None
    return json.loads(gzip.decompress(raw))


@lru_cache(maxsize=2)
def _catalog(path):
    def read(name, digest):
        raw = path.with_name(name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("unknown dialect evidence")
        return json.loads(gzip.decompress(raw))
    return read("class-declarations.json.gz", DECLARATIONS_SHA), read("fields.json.gz", FIELDS_SHA)


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return base + "." + node.attr if base else ""
    return ""


def _imports(tree):
    result = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and not stmt.level and stmt.module:
            for item in stmt.names:
                result[item.asname or item.name] = stmt.module + "." + item.name
    return result


def _operand(node, bindings, catalog):
    if isinstance(node, ast.Lambda):
        return not node.args.defaults and not any(node.args.kw_defaults)
    symbol = _dotted(node)
    if symbol and not any(piece.startswith("__") for piece in symbol.split(".")):
        return True
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return False
    return (bindings.get(node.func.id) in catalog["factories"] and not node.keywords
            and all(isinstance(arg, ast.Constant) and type(arg.value) in (str, bool, int) for arg in node.args))


def _table_key(node, bindings, expressions):
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return "string", node.value
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and bindings.get(node.value.id) == "sqlglot.exp" and node.attr in expressions):
        return "expression", node.attr
    symbol = _dotted(node)
    head, sep, tail = symbol.partition(".")
    if sep and bindings.get(head) == "sqlglot.tokens.TokenType" and tail.isidentifier():
        return "token", tail
    if (sep and bindings.get(head) == "sqlglot.exp" and tail.startswith("DType.")
            and tail.removeprefix("DType.").isidentifier()):
        return "dtype", tail.removeprefix("DType.")
    return None


def _identifiers(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def _safe_new_callback(node, bindings, catalog):
    """Allow lazy expression construction without calls to unknown helpers or state mutation."""
    if node.args.defaults or any(node.args.kw_defaults):
        return False
    arguments = {arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
    def value(expr):
        return isinstance(expr, ast.Constant) or isinstance(expr, ast.Name) and expr.id in arguments
    if value(node.body):
        return True
    if not isinstance(node.body, ast.Call):
        return False
    target = node.body.func
    return (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
            and bindings.get(target.value.id) == "sqlglot.exp" and target.attr in catalog["expressions"]
            and all(value(arg) for arg in node.body.args)
            and all(key.arg and value(key.value) for key in node.body.keywords))


def _table_delta(old, new, field, bindings, catalog, helper):
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
        return None
    tables = []
    for table in (old, new):
        entries, other = {}, []
        for key, value in zip(table.keys, table.values):
            item = _table_key(key, bindings, catalog["expressions"])
            if item is None:
                # Unpacking can override preceding entries. Only an unchanged prefix is supported.
                if entries:
                    return None
                other.append((ast.dump(key) if key is not None else None, ast.dump(value)))
                continue
            if item in entries:
                return None
            entries[item] = value
        tables.append((entries, other))
    (a, left), (b, right) = tables
    if left != right:
        return None
    changed = {k for k in a.keys() | b.keys() if k not in a or k not in b or ast.dump(a[k]) != ast.dump(b[k])}
    permuted = [k for k in a if k in b] != [k for k in b if k in a]
    if (not changed and not permuted) or len(changed) > 12:
        return None
    known = {ast.dump(v) for v in a.values()}
    inlined = False
    for key in changed:
        if key in b and not _operand(b[key], bindings, catalog):
            return None
        if key in a and not _operand(a[key], bindings, catalog):
            return None
        # A new eager expression must already have been evaluated in the same class scope.
        if key in b and ast.dump(b[key]) not in known:
            if helper is None and isinstance(b[key], ast.Lambda) and _safe_new_callback(b[key], bindings, catalog):
                continue
            if helper is None and isinstance(b[key], ast.Call) and _operand(b[key], bindings, catalog):
                continue
            if not (helper and key in a and isinstance(a[key], ast.Lambda) and isinstance(b[key], ast.Lambda)
                    and _operand(b[key], bindings, catalog) and helper.name in _identifiers(a[key])
                    and _identifiers(b[key]) <= _identifiers(helper) | _identifiers(a[key])):
                return None
            inlined = True
    if helper and not inlined:
        return None
    return changed, a.keys() != b.keys(), permuted


def _members(klass):
    result = {}
    for stmt in klass.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign):
            target = stmt.target
        else:
            continue
        if isinstance(target, ast.Name) and target.id.isupper():
            if target.id in result:
                raise ValueError("duplicate class field")
            result[target.id] = stmt
    return result


def _scalar(node):
    if node is None:
        return MISSING
    if isinstance(node.value, ast.Constant) and type(node.value.value) in (str, bool, int):
        return node.value.value
    raise ValueError("unsupported scalar declaration")


def _effects(selector, change, catalog):
    path = change.old_path
    if (change.added or change.deleted or path != change.new_path or not path.endswith(".py")
            or not path.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/"))):
        return None
    old = selector._source(path)
    new = apply_change(old, change)
    if not warning_free(path, new):
        return None
    before, after = ast.parse(old), ast.parse(new)
    bindings = _imports(before)
    module = path[:-3].replace("/", ".")
    effects = []
    helper_mask = 0
    left, right = ({n.name: n for n in tree.body if isinstance(n, ast.ClassDef)} for tree in (before, after))
    if left.keys() != right.keys():
        return None
    for klass, a in left.items():
        b = right[klass]
        if ast.dump(a) == ast.dump(b):
            continue
        owner = module + "." + klass
        if owner not in catalog["classes"]:
            return None
        old_methods = {n.name: n for n in a.body if isinstance(n, ast.FunctionDef)}
        new_methods = {n.name for n in b.body if isinstance(n, ast.FunctionDef)}
        removed = set(old_methods) - new_methods
        if len(removed) > 1 or new_methods - old_methods.keys():
            return None
        helper = old_methods[next(iter(removed))] if removed else None
        if helper:
            if (helper.name not in catalog["unique_reads"] or helper.decorator_list or helper.args.defaults
                    or any(helper.args.kw_defaults) or len(helper.body) > 8
                    or any(isinstance(n, (ast.Global, ast.Nonlocal, ast.With, ast.Raise, ast.Try,
                                          ast.Import, ast.ImportFrom, ast.Delete)) for n in ast.walk(helper))):
                return None
        class_effects = len(effects)
        original, updated = _members(a), _members(b)
        for field in original.keys() | updated.keys():
            one, two = original.get(field), updated.get(field)
            if one and two and ast.dump(one) == ast.dump(two):
                continue
            if (field in ("FUNCTIONS", "FUNCTION_PARSERS", "NO_PAREN_FUNCTIONS", "TRANSFORMS")
                    or (one and two and isinstance(one.value, ast.Dict) and isinstance(two.value, ast.Dict)
                        and path.startswith(("sqlglot/parsers/", "sqlglot/generators/")))):
                if not one or not two:
                    return None
                result = _table_delta(one.value, two.value, field, bindings, catalog, helper)
                if result is None:
                    return None
                effects.append(("table", owner, field, *result))
            else:
                if any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == field
                       for cls in (a, b) for n in ast.walk(cls)):
                    return None
                effects.append(("scalar", owner, field, _scalar(one), _scalar(two)))
            if one and two:
                one.value = two.value = ast.Constant(None)
            elif one and isinstance(one, ast.Assign):
                a.body.remove(one)
            elif two and isinstance(two, ast.Assign):
                b.body.remove(two)
            else:
                return None
        if helper:
            if len(effects) != class_effects + 1 or effects[-1][0] != "table":
                return None
            a.body.remove(helper)
            for line in range(helper.lineno, helper.end_lineno + 1):
                helper_mask |= selector._sites.get(path, {}).get(line, 0)
    if not effects or ast.dump(before) != ast.dump(after):
        return None
    warm = selector._reader_index.changed(path, old, new)
    cold = selector._isolated_index.changed(path, old, new)
    if warm is None or cold is None:
        return None
    readers = helper_mask | warm | cold | selector._reads.get(path, 0) | selector._collection.get(path, 0)
    return effects, readers | (selector._position_readers or {}).get(path, 0), new


def _ancestry(owner, classes):
    seen = set()
    while owner in classes:
        if owner in seen:
            raise ValueError("cyclic class hierarchy")
        seen.add(owner)
        yield owner
        owner = classes[owner][0]


def _resolved(chain, field, changes, classes):
    for owner in chain:
        if (owner, field) in changes:
            value = changes[owner, field]
            if value is not MISSING:
                return owner, value
        elif field in classes[owner][1]:
            literal = classes[owner][1][field]
            if len(literal) != 1:
                raise ValueError("opaque inherited value")
            return owner, literal[0]
    raise ValueError("unresolved inherited field")


def _qualified(owner):
    module, _, cls = owner.rpartition(".")
    return module + ":" + cls


def _mask(values, size):
    result = 0
    for encoded in values:
        if encoded == "eager":
            raise ValueError("field materialized before test execution")
        mask = number(encoded, size)
        if mask is None:
            raise ValueError("missing original field receipt")
        result |= mask
    return result


def _scalar_readers(selector, catalog, index, edits):
    classes = catalog["classes"]
    changed = {(e[1], e[2]): e[4] for e in edits}
    size = len(selector.all_tests)
    mask = 0
    for field in {e[2] for e in edits}:
        touched = {owner for (owner, key) in changed if key == field}
        values, owners = [], set()
        for owner in classes:
            chain = tuple(_ancestry(owner, classes))
            if not touched.intersection(chain):
                continue
            old_owner, old = _resolved(chain, field, {}, classes)
            new_owner, new = _resolved(chain, field, changed, classes)
            if type(old) is not type(new):
                raise ValueError("scalar type changed")
            owners.update((owner, old_owner, new_owner))
            if old != new:
                key = _qualified(old_owner) + ":" + field
                if key not in index["fields"] or index["derived"].get(key):
                    raise ValueError("untracked scalar reader")
                values.append(index["fields"][key])
        for owner in owners:
            values.append(index["reflection"].get(_qualified(owner), ""))
        mask |= _mask(values, size)
        # Include every generic source site that names the flag, across dialects.
        for path, lines in catalog["readers"].get(field, {}).items():
            sites = selector._sites.get(path, {})
            for line in lines:
                mask |= sites.get(line, 0)
                if path.startswith("tests/"):
                    mask |= selector._collection_sites.get(path, {}).get(line, 0)
    return mask


def _effective_readers(selector, catalog, index, edit, observations):
    """Intersect concrete cache use with typed key changes; retain shape and escape observers."""
    _, owner, field, changed, different_keys, permuted = edit
    key = owner + ":" + field
    if key not in observations["declared"]:
        return None
    original_key = _qualified(owner) + ":" + field
    if original_key not in index["fields"] or index["derived"].get(original_key):
        return None
    symbols = observations["key_types"].get(key, {})
    entries = set()
    for kind, name in changed:
        if kind == "string":
            entries.add("s:" + name)
            continue
        value = symbols.get(name, "")
        if kind == "expression" and value.startswith("c:sqlglot.expressions."):
            entries.add(value)
        elif kind == "token" and value.startswith("e:sqlglot.tokens.TokenType."):
            entries.add(value)
        elif kind == "dtype" and value.startswith("e:sqlglot.expressions.") and ".DType." in value:
            entries.add(value)
        else:
            # A newly inserted class key may not have a baseline type-identity receipt.
            return None

    size = len(selector.all_tests)
    rows = set()
    reflection = []
    for child in catalog["classes"]:
        if owner not in _ancestry(child, catalog["classes"]):
            continue
        qualifier = _qualified(child)
        reflection.extend((index["reflection"].get(qualifier, ""), index["dict_reflection"].get(qualifier, "")))
        child_key = child + ":" + field
        if child_key in observations["declared"]:
            rows.add(child_key)
        if field == "TRANSFORMS":
            cache = child + ":@dispatch"
            rows.add(cache)
            rows.update(observations["ancestry"].get(cache, ()))
    mask = _mask(reflection, size)
    allocated = (1 << size) - 1
    for table in rows:
        events = observations["tables"].get(table, {})
        writes = observations["writes"].get(table, {})
        keys = entries | {"*"}
        if different_keys or permuted:
            keys.add("#order")
        if different_keys:
            keys.add("#length")
        # A changed key mutated during this run can escape to a later test in the
        # same interpreter. Let the broad path account for that state.
        if any(writes.get(k) for k in entries | {"*"}):
            return None
        # A view, copy or iterator may outlive its creator. A borrowed result with
        # unknown lifetime can be consumed in another method or even during teardown.
        borrowed = int(events.get("*", "0"), 16)
        order = int(events.get("#order", "0"), 16)
        if different_keys or permuted:
            borrowed |= order
        else:
            for entry in entries:
                borrowed |= order & int(events.get(entry, "0"), 16)
        if borrowed:
            return allocated
        for entry in keys:
            mask |= int(events.get(entry, "0"), 16)
        if mask & ~allocated:
            return None
    return mask | int(observations["guard"], 16)


def _callback_readers(selector, catalog, index, edit):
    _, owner, field, typed, different_keys, permuted = edit
    observed = _observations(selector._maps_path)
    if observed and observed["roster"] == index["roster_sha256"] and observed["count"] == len(selector.all_tests):
        exact = _effective_readers(selector, catalog, index, edit, observed)
        if exact is not None:
            return exact
    if permuted or (field != "TRANSFORMS" and any(kind != "string" for kind, name in typed)):
        raise ValueError("missing qualified key observations")
    if field not in ("FUNCTIONS", "FUNCTION_PARSERS", "NO_PAREN_FUNCTIONS", "TRANSFORMS"):
        raise ValueError("missing dispatch observations")
    changed = {name for _, name in typed}
    qualified = _qualified(owner)
    key = qualified + ":" + field
    table = index["dicts"].get(key)
    if table is None or index["derived"].get(key):
        raise ValueError("unobserved callback table")
    values = [table.get("*", ""), index["reflection"].get(qualified, ""),
              index["dict_reflection"].get(qualified, "")]
    if field == "TRANSFORMS":
        # Generator instances snapshot class transforms into a dispatch cache.
        # Retain all known warm and independent cold consumers of this class module.
        path = owner.rpartition(".")[0].replace(".", "/") + ".py"
        cold = cold_mask(selector, path)
        if cold is None:
            raise ValueError("untracked generator dispatch cache")
        mask = cold | selector._files.get(path, 0)
        values.extend((index["fields"][key], *table.values()))
    else:
        mask = 0
        values.extend(table.get("=" + item, "") for item in changed)
        if different_keys:
            values.append(index["fields"][key])
            for path, sites in catalog["shape"].get(field, {}).items():
                for line in sites:
                    mask |= selector._sites.get(path, {}).get(line, 0)
                    mask |= selector._collection_sites.get(path, {}).get(line, 0)
            for path in catalog["published"].get(field, ()):
                observer = cold_mask(selector, path)
                if observer is None:
                    raise ValueError("unresolved stateful dispatch publisher")
                mask |= observer | selector._files.get(path, 0)
        # Child tables may copy an existing dispatcher or receive its newly added key.
        for child in catalog["classes"]:
            if child == owner or owner not in _ancestry(child, catalog["classes"]):
                continue
            child_key = _qualified(child) + ":" + field
            child_table = index["dicts"].get(child_key, {})
            values.extend((child_table.get("*", ""), index["reflection"].get(_qualified(child), "")))
            values.extend(child_table.get("=" + item, "") for item in changed)
            if different_keys and child_key in index["fields"]:
                values.append(index["fields"][child_key])
    return mask | _mask(values, len(selector.all_tests))


def select(selector, changes, patch):
    """Return the original test numbers for a fully supported class edit."""
    if (selector.unavailable or not changes or len(changes) > 6 or not selector._reader_index or not selector._isolated_index
            or len({change.old_path for change in changes}) != len(changes)):
        return None
    try:
        if not all(c.old_path.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/"))
                   for c in changes):
            return None
        catalog, index = _catalog(selector._maps_path)
        effects = []
        mask = selector._unmapped
        probes = []
        for c in changes:
            result = _effects(selector, c, catalog)
            if result is None:
                return None
            items, readers, source = result
            probes.append((c.old_path, source, {item[1] for item in items}))
            effects.extend(items)
            mask |= readers
        scalar = [e for e in effects if e[0] == "scalar"]
        if scalar:
            mask |= _scalar_readers(selector, catalog, index, scalar)
        for edit in effects:
            if edit[0] == "table":
                mask |= _callback_readers(selector, catalog, index, edit)
        if len(probes) > 1 and any(e[0] != "scalar" for e in effects):
            # A parent's copied table and a child's simultaneous modification can interact.
            return None
        for path, source, owners in probes:
            families = {child.rsplit(".", 2)[-2] for child in catalog["classes"]
                        if any(parent in owners for parent in _ancestry(child, catalog["classes"]))}
            if not families or len(families) > 12:
                return None
            module = path[:-3].replace("/", ".")
            if run(selector.source_root, module, path, "dialects", source, ",".join(sorted(families)), "") != {"state":"ready"}:
                return None
        for n in (*index["guards"], *index["uninstrumented_tests"]):
            mask |= 1 << n
        return {n for n in selector.all_tests if mask >> n & 1}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError,
            SyntaxError, RecursionError, UnsupportedPatch):
        return None
