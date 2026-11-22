"""Index original test modules with statically visible reads of runtime documentation."""

import ast
from pathlib import Path
import sys


def observes(text):
    if not any(word in text for word in ("__doc__", "getdoc", "doctest", "pydoc", "help")):
        return False
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Attribute) and node.attr in {"__doc__", "getdoc"}:
            return True
        if isinstance(node, ast.Constant) and node.value == "__doc__":
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "help":
            return True
        if isinstance(node, ast.ImportFrom) and (node.module in {"pydoc", "doctest"} or any(a.name == "getdoc" for a in node.names)):
            return True
        if isinstance(node, ast.Import) and any(a.name in {"pydoc", "doctest"} for a in node.names):
            return True
    return False


def build(root, package):
    files = []
    for path in sorted((root / "tests").rglob("*.py")):
        try:
            reader = observes(path.read_text(encoding="utf-8"))
        except (UnicodeError, SyntaxError):
            reader = True
        if reader:
            files.append(path.relative_to(root).as_posix())
    (package / "_doc_manifest.py").write_text(f"OBSERVERS = {tuple(files)!r}\n")


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]))
