"""Identify a one-token comparison operator edit in an otherwise identical file."""
import ast
import base64
import gzip
import json
import warnings

NAMES=("Is","IsNot","Eq","NotEq","Lt","LtE","Gt","GtE")

def one_relation(path,old,new):
    if not path.startswith("sqlglot/") or not path.endswith(".py"):return
    a=old.encode();b=new.encode()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            ast_a=ast.parse(a,path);ast_b=ast.parse(b,path)
            compile(a,path,'exec',dont_inherit=True);compile(b,path,'exec',dont_inherit=True)
        except (SyntaxError,Warning):return
    def ranges(tree,source):
        starts=[0]
        for line in source.splitlines(keepends=True):starts.append(starts[-1]+len(line))
        out={}
        for n in ast.walk(tree):
            if not isinstance(n,ast.Compare) or len(n.ops)!=1 or type(n.ops[0]).__name__ not in NAMES:continue
            left=n.left;right=n.comparators[0]
            if left.end_lineno!=right.lineno:continue
            x=starts[left.end_lineno-1]+left.end_col_offset;y=starts[right.lineno-1]+right.col_offset
            # The expression and its left operand begin at the same coordinates
            # on each side of a one-operator edit.
            out[(n.lineno,n.col_offset,x)]=(y,type(n.ops[0]).__name__)
        return out
    orig=ranges(ast_a,a);updated=ranges(ast_b,b)
    for (line,column,start),(end,op) in orig.items():
        other=updated.get((line,column,start))
        if not other or other[1]==op:continue
        end2,newop=other
        if a[:start]!=b[:start] or a[end:]!=b[end2:]:continue
        if b'#' in a[start:end] or b'#' in b[start:end2]:continue
        return f"{path}|{line}|{column}",NAMES.index(newop)

class RelationIndex:
    def __init__(self,path):
        self.sites=json.loads(gzip.decompress(path.read_bytes()))['relations']
    def eligible(self,path,old,new):
        item=one_relation(path,old,new)
        if not item:return
        site,operator=item
        row=self.sites.get(site)
        if row is None or row[operator] is None:return
        return int.from_bytes(base64.b64decode(row[operator],validate=True),'little')
