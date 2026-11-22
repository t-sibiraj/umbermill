"""Plan the input batch before spending CPU on speculative execution."""

from dataclasses import dataclass
from pathlib import Path
import time

from .patches import UnsupportedPatch, parse_patch
from .replay_limits import planning_seconds
from .selector import Selection


@dataclass
class Job:
    path: Path
    text: str
    selection: Selection
    fixture: bool
    import_edit: bool
    estimated_load: float


def prepare(directory, selector, durations):
    jobs = []
    paths = sorted(directory.glob("*.patch"))
    deadline = time.monotonic() + planning_seconds(len(paths))
    for path in paths:
        if time.monotonic() >= deadline:
            selection = Selection(sorted(durations), "planning time limit", True, "shared state")
            jobs.append(Job(path, "", selection, False, True, 0))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            selection = Selection(sorted(durations), "invalid UTF-8 patch", True, "shared state")
            jobs.append(Job(path, "", selection, False, False, 0))
            continue
        selection = selector.select(text)
        try:
            changes = parse_patch(text)
        except UnsupportedPatch:
            changes = []
        fixture = bool(changes) and all(
            c.old_path == c.new_path and c.old_path.startswith("tests/fixtures/")
            and c.old_path.endswith(".sql") for c in changes
        )
        import_edit = any(
            line[1:].lstrip().startswith(("from ", "import ", "class ", "@"))
            for c in changes for hunk in c.hunks for line in hunk.lines
            if line[:1] in {"+", "-"}
        )
        load = sum(durations[n] for n in selection.tests)
        jobs.append(Job(path, text, selection, fixture, import_edit, load))
    # Keep warm fixture runs together and defer costly cold imports. Filename
    # breaks ties, so enumeration order on the filesystem is immaterial.
    jobs.sort(key=lambda j: (not j.fixture, j.import_edit, j.estimated_load, j.path.name))
    return jobs
