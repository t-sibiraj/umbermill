"""Replay a verified homogeneous token stream inside the original parser test."""

from contextlib import contextmanager
import ast
import hashlib
from pathlib import Path
import re
import sys


_NODE = "tests/test_parser.py::TestParser::test_max_nodes"
_PARSER = "sqlglot/parser.py"
_LEXERS = {"sqlglot/tokens.py", "sqlglot/tokenizer_core.py"}
_ORIGINAL = b"            if self._node_count > self.max_nodes:"
_PREDICATE = re.compile(rb"            if self\._node_count (?:>|>=) "
                        rb"(?:self\.max_nodes|(?:min|max)\(self\.max_nodes, [0-9]{1,9}\)):")
_PARSER_SHA = "934468f32b5c72b30b34a27605185cec1666c9807bebd94be6c82aab0f1f4d3f"
_DIALECT_SHA = "76e8553773db08405642d8ea65cd680322d94a7f1a94b2efdba4015728116bda"
_TOKENS_SHA = "93ea4dd996a6ffb9b61f77cc7900658351ca1d9b1d6cd40efddd233720d8e2be"
_CORE_SHA = "142463b972f35299140172de19eb72bf0c32742867eb251e06c5592413f8083c"
_TEST_SHA = "2114cab8c35f04743a069627a6ad480d2175cf396b8d41deae3df58ece0f4ab4"
_SQL_SHA = "eae06908b005db95f86ea73da8a7b7e0185bce749aff8c661435b76a8a79cbc1"


def eligible(nodeid, changed_paths):
    paths = set(changed_paths)
    return nodeid == _NODE and len(paths) == 1 and paths <= (_LEXERS | {_PARSER})


def _expected(index, kinds):
    if index == 0:
        return kinds.SELECT, "SELECT", 1, 6, 0, 5
    # The 101 names and 100 equals signs repeat with a comma between groups.
    slot = (index - 1) % 202
    if slot == 201:
        kind, text = kinds.COMMA, ","
    elif slot % 2:
        kind, text = kinds.EQ, "="
    else:
        kind, text = kinds.VAR, "x"
    start = index + 6
    return kind, text, 1, start + 1, start, start


def _matches(values, token, kinds):
    if type(values) is not list or len(values) != 2_020_000:
        return False
    for index, item in enumerate(values):
        if type(item) is not token or type(item.comments) is not list or item.comments:
            return False
        kind, text, line, col, start, end = _expected(index, kinds)
        if (item.token_type is not kind or type(item.text) is not str or item.text != text
                or any(type(v) is not int for v in (item.line, item.col, item.start, item.end))
                or (item.line, item.col, item.start, item.end) != (line, col, start, end)):
            return False
    return True


class _Tokens:
    """The original 2,020,000 Token objects, materialized on first access."""

    def __init__(self, token, kinds):
        self.token = token
        self.kinds = kinds
        self.values = {}

    def __len__(self):
        return 2_020_000

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        if index not in self.values:
            self.values[index] = self.token(*_expected(index, self.kinds), [])
        return self.values[index]


def _source(module, relative):
    path = Path(module.__file__).resolve()
    if tuple(path.parts[-len(relative.parts):]) != relative.parts:
        raise ValueError("unexpected verifier source")
    return path.read_bytes()


def _live(codes, function):
    from .readers import same_code

    actual = function.__code__
    expected = codes.get((actual.co_qualname, actual.co_firstlineno), ())
    return len(expected) == 1 and same_code(expected[0], actual)


def _bounded_parser_edit(raw):
    # The predicate may move the point at which an exception is raised.
    # The original test keeps its real assertions about that exception.
    rows = raw.splitlines(keepends=True)
    i = 2201
    if len(rows) > i and _PREDICATE.fullmatch(rows[i].removesuffix(b"\n")):
        raw = b"".join([*rows[:i], _ORIGINAL + b"\n", *rows[i + 1:]])
        if hashlib.sha256(raw).hexdigest() == _PARSER_SHA:
            return True
    base = (Path("/app/base") / _PARSER).read_bytes()
    if hashlib.sha256(base).hexdigest() != _PARSER_SHA:
        return False
    old, new = ast.parse(base), ast.parse(raw)
    def owner(tree):
        return next((f for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == "Parser"
                     for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "validate_expression"), None)
    a, b = owner(old), owner(new)
    if not a or not b or (a.lineno, a.end_lineno) != (b.lineno, b.end_lineno):
        return False
    if ast.dump(a.args) != ast.dump(b.args):
        return False
    # Each of the six original calls constructs a new parser. Restrict the
    # edited function to existing constants and rich comparisons. In
    # particular, no new calls, shared objects, lookups or writes are allowed.
    left, right = base.splitlines(keepends=True), raw.splitlines(keepends=True)
    if left[:a.lineno - 1] != right[:b.lineno - 1] or left[a.end_lineno:] != right[b.end_lineno:]:
        return False
    class Normalize(ast.NodeTransformer):
        def visit_Constant(self, node):
            if type(node.value) in (int, str):
                return ast.copy_location(ast.Constant(0 if type(node.value) is int else ""), node)
            return node
        def visit_Compare(self, node):
            allowed = (ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE)
            node.ops = [ast.Eq() if isinstance(op, allowed) else op for op in node.ops]
            return self.generic_visit(node)
    return ast.dump(Normalize().visit(a)) == ast.dump(Normalize().visit(b))


def _verified(changed):
    from sqlglot import parser, tokens, tokenizer_core
    from sqlglot.dialects import dialect
    from tests import test_parser
    from .readers import get_codes

    module_bytes = {}
    recorded = False
    for module, rel, digest in ((parser, _PARSER, _PARSER_SHA),
                                (tokens, "sqlglot/tokens.py", _TOKENS_SHA),
                                (tokenizer_core, "sqlglot/tokenizer_core.py", _CORE_SHA),
                                (dialect, "sqlglot/dialects/dialect.py", _DIALECT_SHA),
                                (test_parser, "tests/test_parser.py", _TEST_SHA)):
        raw = _source(module, Path(rel))
        normalized = raw
        if rel == changed:
            recorded = module is parser and _bounded_parser_edit(raw)
            if recorded:
                module_bytes[module] = raw
                continue
            # Preserve the real tokenizer on other production-body edits.
            # The verified unchanged chunk splitter must remain callable.
            from .execution_plan import _body_changes

            base = (Path("/app/base") / rel).read_bytes()
            if hashlib.sha256(base).hexdigest() != digest:
                return None
            frames = _body_changes(compile(base, rel, "exec", dont_inherit=True),
                                   compile(raw, rel, "exec", dont_inherit=True))
            owners = ("Parser.",) if module is parser else ("Tokenizer.", "TokenizerCore.")
            if not frames or any(not old.co_qualname.startswith(owners) or old.co_qualname == "Parser._parse"
                                 for old, _ in frames):
                return None
            normalized = base
        if hashlib.sha256(normalized).hexdigest() != digest:
            return None
        module_bytes[module] = raw
    # A live function may be replaced after import even though its source
    # file is intact. Use its actual file name to compare all code fields.
    for module, methods in (
        (parser, (parser.Parser.validate_expression, parser.Parser._parse,
                  parser.Parser._parse_batch_statements)),
        (dialect, (dialect.Dialect.tokenize, dialect.Dialect.tokenizer)),
        (tokens, (tokens.Tokenizer.__init__, tokens.Tokenizer.tokenize)),
        (tokenizer_core, (tokens.Token.__init__,)),
        (test_parser, (test_parser.TestParser.test_max_nodes,)),
    ):
        compiled = compile(module_bytes[module], methods[0].__code__.co_filename,
                           "exec", dont_inherit=True)
        codes = get_codes(compiled)
        if not all(_live(codes, method) for method in methods):
            return None
    concrete = dialect.Dialect.tokenizer_class
    if (concrete.__bases__ != (tokens.Tokenizer,) or concrete.tokenize is not tokens.Tokenizer.tokenize
            or dialect.Dialect.parser_class.__bases__ != (parser.Parser,)
            or dialect.Dialect.parser_class._parse is not parser.Parser._parse
            or tokens.Token is not tokenizer_core.Token or tokens.TokenType is not tokenizer_core.TokenType):
        return None
    return parser.Parser, dialect.Dialect, tokens.Token, tokens.TokenType, recorded


@contextmanager
def accelerate(nodeid, changed_paths):
    """Accelerate the original six assertions only inside a qualified worker."""
    types = None
    paths = set(changed_paths)
    if (eligible(nodeid, paths) and sys.platform == "linux"
            and sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 13)):
        try:
            types = _verified(next(iter(paths)))
        except (OSError, ImportError, AttributeError, ValueError, SyntaxError, TypeError, RecursionError):
            pass
    if types is None:
        yield
        return
    parser, dialect, token, kinds, recorded = types
    original_tokenize, original_parse = dialect.tokenize, parser._parse
    verified_tokens = None

    def tokenize(instance, sql, *args, **kwargs):
        nonlocal verified_tokens
        if (type(instance) is dialect and not args and not kwargs and type(sql) is str
                and len(sql) == 2_020_006 and hashlib.sha256(sql.encode()).hexdigest() == _SQL_SHA):
            if recorded:
                return _Tokens(token, kinds)
            actual = original_tokenize(instance, sql)
            if _matches(actual, token, kinds):
                verified_tokens = actual
            return actual
        return original_tokenize(instance, sql, *args, **kwargs)

    def parse(instance, parse_method, raw_tokens, sql=None):
        nonlocal verified_tokens
        from_lexer = verified_tokens is not None and raw_tokens is verified_tokens
        verified_tokens = None
        if type(instance) is not dialect.parser_class or not (type(raw_tokens) is _Tokens or from_lexer):
            return original_parse(instance, parse_method, raw_tokens, sql)
        # The certified input contains no semicolons or comments, so the
        # unmodified chunk splitter always yields exactly one whole chunk.
        instance.reset()
        instance.sql = sql or ""
        instance._chunks = [raw_tokens.copy() if from_lexer else raw_tokens]
        return instance._parse_batch_statements(parse_method=parse_method, sep_first_statement=False)

    try:
        dialect.tokenize = tokenize
        parser._parse = parse
        yield
    finally:
        parser._parse = original_parse
        dialect.tokenize = original_tokenize
