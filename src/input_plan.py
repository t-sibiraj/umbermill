"""Select consumers of test sources, fixture data and observed document paths."""

from functools import lru_cache
from pathlib import PurePosixPath
import re

from .file_selection import file_selection
from .patches import apply_change
from .test_scope import pick


_DATA_SUFFIXES = {".sql", ".csv", ".json", ".txt", ".yaml", ".yml", ".tsv"}
_PYTEST_CONFIG = {
    "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "pyproject.toml",
    "tox.ini", "setup.cfg", "conftest.py",
}
_LAZY_HOOK = re.compile(r"^(?:(?:async\s+)?def\s+)?__(?:getattr|dir)__\b", re.M)
_HOOK_NAME = re.compile(r"\b__(?:getattr|dir)__\b")
_NAMESPACE = re.compile(r"^(?:__all__|__path__|__spec__|__loader__)\b")
_COLLECTION = re.compile(
    r"^\s*(?:(?:async\s+)?def\s+(?:test\w*|pytest_\w*|load_tests|setUpClass|tearDownClass)\b"
    r"|class\s+\w+\b|(?:pytestmark|pytest_plugins|__test__|test\w*)\s*(?::|=)"
    r"|@(?:pytest|unittest)\b)"
)


@lru_cache(maxsize=256)
def _has_lazy_hook(selector, path):
    source = selector._source(path)
    return ("__getattr__" in source or "__dir__" in source) and bool(_LAZY_HOOK.search(source))


def _sensitive_python(selector, change):
    path = change.old_path
    if not path.endswith(".py"):
        return False
    delta = [line[1:] for h in change.hunks for line in h.lines if line.startswith(("+", "-"))]
    # Attribute hooks run on a missing name, including module-level 'from'
    # imports. A change to any global they consult can alter the namespace.
    if path.startswith("sqlglot/"):
        return (
            _has_lazy_hook(selector, path)
            or any(_HOOK_NAME.search(line) or _NAMESPACE.match(line) for line in delta)
            or (path.endswith("/__init__.py") and any(
                line.startswith(("import ", "from ")) for line in delta
            ))
        )
    if not path.startswith("tests/"):
        return False
    return any(_COLLECTION.match(line) for line in delta)


def structural_guard(selector, changes, patch) -> set[int] | None:
    """Protect original roster members when collection or package resolution can change."""
    if selector.unavailable or selector.granularity != "function":
        return None
    try:
        git_sections = patch.startswith("diff --git ") + patch.count("\ndiff --git ")
        structural = (
            not changes
            or (git_sections and git_sections != len(changes))
            or any(c.added or c.deleted or c.old_path != c.new_path for c in changes)
        )
        if not structural:
            paths = {c.old_path for c in changes}
            if any(PurePosixPath(p).name in _PYTEST_CONFIG for p in paths):
                return set(selector.all_tests)
            if any(_sensitive_python(selector, change) for change in changes):
                return set(selector.all_tests)
            # A changed test module can import another changed source for the
            # first time. Reconstruct all affected files before composing bounds.
            structural = len(paths) > 1 and any(
                p.startswith("tests/") and p.endswith(".py") for p in paths
            )
        if not structural:
            return None
        # Include the numbered original roster if the new collection is unknown.
        # That covers newly collected tests and fixtures, disappeared tests, and
        # arbitrary new import-time code. New node IDs have no contest number.
        mask = file_selection(selector, patch)
        if mask is not None:
            mask |= selector._unmapped
            return {n for n in selector.all_tests if mask & (1 << n)}
    except (OSError, UnicodeError, ValueError, SyntaxError, TypeError, LookupError, RecursionError):
        pass
    return set(selector.all_tests)


def _known_fixture(selector, path):
    return (
        path.startswith("tests/fixtures/")
        and PurePosixPath(path).suffix in _DATA_SUFFIXES
        and path in selector._reads
    )


def _input_mask(selector, changes, patch):
    git_sections = patch.startswith("diff --git ") + patch.count("\ndiff --git ")
    if (
        not changes
        or (git_sections and git_sections != len(changes))
        or any(c.added or c.deleted or c.old_path != c.new_path for c in changes)
    ):
        # Some Git operations have no text hunks. This recognizer verifies every
        # file operation, its old/new blobs and the source/destination names.
        return file_selection(selector, patch)

    paths = {change.old_path for change in changes}
    if len(paths) != len(changes) or any(path.startswith("sqlglot/") for path in paths):
        return None

    if len(changes) == 1:
        change = changes[0]
        path = change.old_path
        if path in selector._test_sources:
            proof = pick(selector, patch, selector._test_sources)
            if proof is not None:
                mask, _ = proof
                return mask
        if (
            path not in selector._tests_by_file
            and path not in selector._root_documents
            and not _known_fixture(selector, path)
        ):
            return None
        mask, _ = selector._change_tests(change, allow_fixture_projection=True)
        return mask

    if all(path in selector._root_documents or _known_fixture(selector, path) for path in paths):
        # Every changed input keeps its original path; the fixture loader and
        # its callers are unchanged. Retain the readers of every changed input.
        mask = 0
        for change in changes:
            path = change.old_path
            if path in selector._root_documents:
                apply_change(selector._source(path), change)
                mask |= selector._root_documents[path]
            else:
                readers, _ = selector._change_tests(
                    change, allow_fixture_projection=True, single_file=False
                )
                mask |= readers
        return mask

    # A changed test can import or read another changed path for the first
    # time. Only the whole-patch recognizer can authorize that composition.
    return file_selection(selector, patch)


def select(selector, changes, patch) -> set[int] | None:
    """Return a complete reader bound, or defer the entire patch to the caller."""
    if selector.unavailable or selector.granularity != "function":
        return None
    try:
        mask = _input_mask(selector, changes, patch)
    except (OSError, ValueError, SyntaxError, KeyError, TypeError, RecursionError):
        return None
    if mask is None:
        return None
    mask |= selector._unmapped
    return {number for number in selector.all_tests if mask & (1 << number)}
