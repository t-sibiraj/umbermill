"""Private prototype for pinned 3.13 source-position changes."""
import ast
import dis
import io
import tokenize
import marshal
import sys
import warnings
from types import CodeType

from .patches import UnsupportedPatch, apply_change, parse_patch


def firstline_constant(code, index):
    """Verify all uses of this integer are the compiler's class location store."""
    if "__firstlineno__" not in code.co_names:
        return False
    ops = list(dis.get_instructions(code))
    hits = [i for i, op in enumerate(ops) if op.opcode in dis.hasconst and op.arg == index]
    return bool(hits) and all(i + 1 < len(ops) and ops[i].opname == "LOAD_CONST"
                             and ops[i+1].opname == "STORE_NAME"
                             and ops[i+1].argval == "__firstlineno__" for i in hits)


def equivalent_code(old, new, allow_class_line=False):
    fields = ("co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals",
              "co_stacksize", "co_flags", "co_code", "co_names", "co_varnames", "co_filename",
              "co_name", "co_qualname", "co_exceptiontable", "co_freevars", "co_cellvars")
    if any(getattr(old, f) != getattr(new, f) for f in fields) or len(old.co_consts) != len(new.co_consts):
        return False
    for idx, (before, after) in enumerate(zip(old.co_consts, new.co_consts)):
        if type(before) is not type(after):
            return False
        if isinstance(before, CodeType):
            if not equivalent_code(before, after, allow_class_line):
                return False
        elif marshal.dumps(before) != marshal.dumps(after):
            if not (allow_class_line and type(before) is int and
                    firstline_constant(old, idx) and firstline_constant(new, idx)):
                return False
    return True


def abstract_program(text, path):
    tree=ast.parse(text,path,type_comments=True)
    directives=[]
    if tree.type_ignores:
        statements=[n for n in ast.walk(tree) if isinstance(n,ast.stmt)]
        lines=text.splitlines()
        for note in tree.type_ignores:
            owners=tuple(i for i,n in enumerate(statements) if n.lineno<=note.lineno<=n.end_lineno)
            # Associate an ignored diagnostic with the same lexical statements
            # and physical source line; retain absolute coordinates for orphans.
            directives.append((note.tag,owners,lines[note.lineno-1],None if owners else note.lineno))
            note.lineno=0
    return ast.dump(tree), directives


def position_only(path, before, after, allow_class_line=False):
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13):
        return False
    if not path.startswith("sqlglot/") or not path.endswith(".py") or before == after:
        return False
    def code(text):
        return compile(text.encode("utf8"), path, "exec", dont_inherit=True)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            # Parse type comments explicitly so a source-level typing pragma change keeps its fallback.
            if abstract_program(before,path) != abstract_program(after,path):
                return False
            return equivalent_code(code(before), code(after), allow_class_line)
        except (Warning, SyntaxError, ValueError, OverflowError, RecursionError):
            return False


def simple_operator_change(before, after):
    alternatives=({"and","or"},{"==","!=","<","<=",">",">="},{"+","-","*","/","//","%"},{"&","|","^"})
    def tokens(text):
        return [(t.type,t.string) for t in tokenize.generate_tokens(io.StringIO(text).readline)]
    try:
        left,right=tokens(before),tokens(after)
    except (SyntaxError,tokenize.TokenError):
        return False
    if len(left)!=len(right):
        return False
    changed=[(a,b) for a,b in zip(left,right) if a!=b]
    if len(changed)!=1 or changed[0][0][0]!=changed[0][1][0]:
        return False
    a,b=changed[0][0][1],changed[0][1][1]
    return any(a in family and b in family for family in alternatives)


def select(selector, patch, *, mode, overrides=None):
    changes=parse_patch(patch)
    parts=[]
    for c in changes:
        mask, why=selector._change_tests(c, allow_fixture_projection=all(ch.old_path!="tests/helpers.py" for ch in changes), single_file=len(changes)==1)
        parts.append((c,mask,why))
    return qualify_parts(selector, parts, mode=mode, overrides=overrides)


def qualify_parts(selector, parts, *, mode="observed_class", overrides=None):
    if not any(why=="source positions may affect file dependents" for _,_,why in parts):
        return None
    # Every behavioral sibling must be a tracked SQL fixture. Keep ALL baseline
    # readers for such siblings, including interactions with physical locations.
    selected=selector._unmapped; qualified=[]
    for c,mask,why in parts:
        path=c.old_path
        if why=="identical source":
            continue
        if path.startswith("tests/fixtures/") and path.endswith(".sql"):
            if not selector._reads.get(path):
                return None
            selected |= selector._reads[path]
            continue
        if why not in ("unchanged executable source plus text readers","source positions may affect file dependents"):
            if ("composite" not in mode or not path.startswith("sqlglot/")
                    or why not in ("changed function callers plus text readers","changed statement callers plus text readers")
                    or not simple_operator_change(selector._source(path),apply_change(selector._source(path),c))):
                return None
            # Cross-file empirical guard: an existing operator may enable an
            # observer absent from the normal baseline path through its caller.
            selected |= mask | selector._files.get(path,0) | selector._collection.get(path,0) | selector._reads.get(path,0)
            continue
        if not position_only(path,selector._source(path),apply_change(selector._source(path),c),allow_class_line=mode.endswith("class")):
            return None
        data=(overrides or {}).get(path)
        permitted=path in selector._deferred
        if not permitted and data is None:
            return None
        raw=selector._deferred[path][1] if permitted else 0
        if data is not None:
            raw |= data
        if mode.startswith("oldreads"):
            raw |= selector._reads.get(path,0)
        selected |= raw
        qualified.append(path)
    return selected, qualified
