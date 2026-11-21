"""Frozen witnesses for ordinary function entry and guard-dominated statements."""

import ast
import base64
import gzip
import hashlib
import json

from . import BASE_COMMIT
from .deferred import warning_free
from .precision import outside_expression


def _parameters(function):
    positional = function.args.posonlyargs + function.args.args
    defaults = function.args.defaults
    return [
        *zip(positional[len(positional) - len(defaults):], defaults),
        *zip(function.args.kwonlyargs, function.args.kw_defaults),
    ]


def _literal(node):
    return isinstance(node, ast.Constant) and type(node.value) in (bool, str, int)


class FlowIndex:
    def __init__(self, file, nodeids):
        with gzip.open(file, 'rb') as stream:
            data = json.loads(stream.read(1_000_000))
        encoded = json.dumps(sorted(nodeids.items()), separators=(',', ':')).encode()
        if data['base'] != BASE_COMMIT or data['roster'] != hashlib.sha256(encoded).hexdigest():
            raise ValueError('flow index identity mismatch')
        self.defaults = data['defaults']
        self.tails = data['tails']
        self.size = len(nodeids)

    def _mask(self, encoded):
        mask = int.from_bytes(base64.b64decode(encoded, validate=True), 'little')
        if mask.bit_length() > self.size:
            raise ValueError('flow selection outside roster')
        return mask

    def default_consumers(self, path, source, replacement):
        models = self.defaults.get(path)
        if not models:
            return None
        old = ast.parse(source).body
        new = ast.parse(replacement).body
        if len(old) != len(new):
            return None
        for before, after in zip(old, new):
            if not isinstance(before, ast.FunctionDef) or before.name not in models:
                continue
            if not isinstance(after, ast.FunctionDef) or before.name != after.name:
                continue
            if before.decorator_list or after.decorator_list or not before.name.startswith('_'):
                continue
            for (name, value), (other, changed) in zip(_parameters(before), _parameters(after)):
                if name.arg != other.arg or not _literal(value) or not _literal(changed):
                    continue
                if type(value.value) is not type(changed.value) or value.value == changed.value:
                    continue
                row = models[before.name].get(name.arg)
                if row is None or type(row[0]) is not type(value.value) or row[0] != value.value:
                    continue
                if outside_expression(source, value) != outside_expression(replacement, changed):
                    continue
                if warning_free(path, source) and warning_free(path, replacement):
                    return self._mask(row[1])
        return None

    def has_tail(self, path, body):
        return str(body.last_line) in self.tails.get(path, {})

    def tail_consumers(self, path, body, line):
        rows = self.tails.get(path, {}).get(str(body.last_line), ())
        masks = [
            self._mask(value) for name, mark, value in rows
            if name == body.name and body.first_line <= mark <= line
        ]
        if not masks:
            return None
        mask = masks[0]
        for other in masks[1:]:
            mask &= other
        return mask
