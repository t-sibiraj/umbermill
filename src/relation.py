"""Match a single source-verified operator while preserving both operands."""
from .deferred import warning_free

OPERATORS=(b"is",b"is not",b"==",b"!=",b"<",b"<=",b">",b">=")


def one_relation(path, old, new, row, sites):
    if not sites or not path.startswith("sqlglot/"):
        return None
    before=old.encode()
    after=new.encode()
    delta=len(after)-len(before)
    for column,(start,end) in sites.items():
        finish=end+delta
        if finish<=start or finish>len(after) or before[:start]!=after[:start] or before[end:]!=after[finish:]:
            continue
        previous=before[start:end].strip()
        text=after[start:finish]
        operator=text.strip(b" \t")
        if previous not in OPERATORS or operator not in OPERATORS or operator==previous:
            continue
        # Alphabetic operators must stay delimited on both sides. A swallowed
        # name, attribute or number is a different program despite valid syntax.
        if operator[:1].isalpha() and not (text[:1].isspace() and text[-1:].isspace()):
            continue
        if not warning_free(path,old) or not warning_free(path,new):
            continue
        return f"{path}|{row}|{column}", OPERATORS.index(operator)
