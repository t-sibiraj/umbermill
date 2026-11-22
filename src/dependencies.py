"""Resolve a whole patch against the dependency records for the pinned tree."""

from . import execution_plan, field_plan, input_plan, method_plan, slots_plan, time_plan, type_only
from .patches import UnsupportedPatch, parse_patch
from .selector import Selection


def plan_patch(selector, patch, *, general=False):
    try:
        changes = parse_patch(patch)
    except UnsupportedPatch:
        # File creation, deletion and rename have a separate structural parser.
        changes = None
    if changes:
        paths = {change.old_path for change in changes}
        if len(paths) != len(changes):
            return None
    else:
        paths = set()

    production = paths and all(path.startswith("sqlglot/") and path.endswith(".py") for path in paths)
    strategies = []
    if general:
        if production:
            strategies.append((execution_plan.select, "observed execution and source dependencies"))
    else:
        guarded = input_plan.structural_guard(selector, changes, patch)
        if guarded is not None:
            return Selection(sorted(guarded), "changed package resolution or test collection", True, "shared state")
        try:
            layout = slots_plan.select(selector, changes, patch)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, SyntaxError, UnsupportedPatch):
            layout = None
        if layout is not None:
            return Selection(sorted(layout), "changed instance layout and inherited consumers", False, "layout")
    if not general and production:
        strategies.append((type_only.select, "unreached type-only imports and observed source readers"))
        if all(path.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/"))
               for path in paths):
            strategies.extend((
                (time_plan.select, "observed format conversions and table readers"),
                (field_plan.select, "observed field and key readers"),
                (method_plan.select, "inherited method and dispatch consumers"),
            ))
    elif not general:
        strategies.append((input_plan.select, "test and data dependencies"))

    for resolve, reason in strategies:
        try:
            candidates = resolve(selector, changes, patch)
        except (OSError, EOFError, ValueError, KeyError, TypeError, RecursionError, SyntaxError):
            # A partial snapshot cannot account for the patch. Keep the legacy fallback available.
            continue
        if candidates is None:
            continue
        # A result describes the complete patch. An empty set is a valid observation.
        selected = set(candidates)
        if not selected.issubset(selector.all_tests):
            return None
        selected.update(i for i in selector.all_tests if (selector._unmapped >> i) & 1)
        return Selection(sorted(selected), reason, False, "dependencies")
    return None
