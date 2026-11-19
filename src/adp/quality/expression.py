"""Whitelisted row-predicate expressions for business rules (P4-06).

Why a hand-written parser + Python evaluator rather than handing SQL to DuckDB: an expression
comes from a config file, so the safest engine is one that cannot do anything but the grammar
below. There is no SQL engine, no file/network access, no `eval`: `parse` turns the text into a
small tuple AST (unknown token, function, identifier or construct => ValueError at config
load), `evaluate_predicate` walks it on one row with SQL three-valued logic (NULL propagates).

Grammar (case-insensitive keywords, column names are the declared lowercase identifiers):

    or    := and (OR and)*            and  := not (AND not)*        not := NOT not | cmp
    cmp   := add [(= | <> | != | < | <= | > | >=) add | IS [NOT] NULL]
    add   := mul ((+ | -) mul)*       mul  := unary ((* | /) unary)*     unary := - unary | atom
    atom  := number | 'string' | DATE 'YYYY-MM-DD' | TRUE | FALSE | NULL | column
           | COALESCE(e, ...) | ABS(e) | CAST(e AS INTEGER|DOUBLE|VARCHAR|DATE) | ( or )

Refused up front: `;`, `--`, `/*`, `*/`, control or non-ASCII characters, quotes other than the
single-quoted literal, sub-queries, any function outside the list, any identifier that is not a
declared column, over-long or over-deep expressions. Error messages never contain row values.
"""

import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from adp.mapping.transform import Value

MAX_EXPR_LEN = 300
MAX_TOKENS = 120
MAX_DEPTH = 20
MAX_LITERAL_LEN = 100

Node = tuple[Any, ...]

_KEYWORDS = frozenset({"and", "or", "not", "is", "null", "true", "false", "date", "cast", "as"})
_FUNCTIONS = {"coalesce": (1, 10), "abs": (1, 1)}
_CAST_TYPES = frozenset({"integer", "double", "varchar", "date"})
_FORBIDDEN = (";", "--", "/*", "*/")
_TOKEN = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)|(?P<str>'(?:[^']|'')*')|(?P<id>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op><=|>=|<>|!=|[=<>+\-*/(),])"
)
_BOOL_KINDS = frozenset({"cmp", "and", "or", "not", "isnull"})


def _tokenize(text: str) -> list[tuple[str, str]]:
    if not text.strip():
        raise ValueError("empty expression")
    if len(text) > MAX_EXPR_LEN:
        raise ValueError(f"expression longer than {MAX_EXPR_LEN} characters")
    if any(f in text for f in _FORBIDDEN):
        raise ValueError("forbidden sequence (';', '--', '/*', '*/')")
    if any(not (c == " " or (c.isascii() and c.isprintable())) for c in text):
        raise ValueError("control or non-ASCII character")
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if text[pos] == " ":
            pos += 1
            continue
        m = _TOKEN.match(text, pos)
        if m is None:
            raise ValueError(f"unexpected character at position {pos}")
        kind = str(m.lastgroup)
        tokens.append((kind, m.group()))
        pos = m.end()
        if len(tokens) > MAX_TOKENS:
            raise ValueError(f"more than {MAX_TOKENS} tokens")
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], columns: frozenset[str]) -> None:
        self.t = tokens
        self.i = 0
        self.cols = columns
        self.depth = 0
        self.used: set[str] = set()

    def peek(self) -> tuple[str, str]:
        return self.t[self.i] if self.i < len(self.t) else ("end", "")

    def word(self) -> str:
        kind, text = self.peek()
        return text.lower() if kind == "id" else ""

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        self.i += 1
        return tok

    def op(self, *ops: str) -> str | None:
        kind, text = self.peek()
        if kind == "op" and text in ops:
            self.i += 1
            return text
        return None

    def kw(self, name: str) -> bool:
        if self.word() == name:
            self.i += 1
            return True
        return False

    def expect_op(self, o: str) -> None:
        if self.op(o) is None:
            raise ValueError(f"expected '{o}'")

    def boolean(self, node: Node) -> Node:
        if node[0] not in _BOOL_KINDS and not (node[0] == "lit" and isinstance(node[1], bool)):
            raise ValueError("AND/OR/NOT operands must be boolean predicates")
        return node

    def or_(self) -> Node:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ValueError(f"expression nested deeper than {MAX_DEPTH}")
        node = self.and_()
        while self.kw("or"):
            node = ("or", self.boolean(node), self.boolean(self.and_()))
        self.depth -= 1
        return node

    def and_(self) -> Node:
        node = self.not_()
        while self.kw("and"):
            node = ("and", self.boolean(node), self.boolean(self.not_()))
        return node

    def not_(self) -> Node:
        if self.kw("not"):
            self.depth += 1
            if self.depth > MAX_DEPTH:
                raise ValueError(f"expression nested deeper than {MAX_DEPTH}")
            node = ("not", self.boolean(self.not_()))
            self.depth -= 1
            return node
        return self.cmp()

    def cmp(self) -> Node:
        left = self.add()
        if self.kw("is"):
            negated = self.kw("not")
            if not self.kw("null"):
                raise ValueError("expected NULL after IS")
            return ("isnull", left, negated)
        o = self.op("=", "<>", "!=", "<=", ">=", "<", ">")
        if o is not None:
            return ("cmp", "<>" if o == "!=" else o, left, self.add())
        return left

    def add(self) -> Node:
        node = self.mul()
        while (o := self.op("+", "-")) is not None:
            node = ("bin", o, node, self.mul())
        return node

    def mul(self) -> Node:
        node = self.unary()
        while (o := self.op("*", "/")) is not None:
            node = ("bin", o, node, self.unary())
        return node

    def unary(self) -> Node:
        if self.op("-"):
            self.depth += 1
            if self.depth > MAX_DEPTH:
                raise ValueError(f"expression nested deeper than {MAX_DEPTH}")
            node = ("neg", self.unary())
            self.depth -= 1
            return node
        return self.atom()

    def string(self) -> str:
        kind, text = self.take()
        if kind != "str":
            raise ValueError("expected a string literal")
        value = text[1:-1].replace("''", "'")
        if len(value) > MAX_LITERAL_LEN:
            raise ValueError(f"string literal longer than {MAX_LITERAL_LEN}")
        return value

    def atom(self) -> Node:
        kind, text = self.peek()
        if kind == "num":
            self.i += 1
            return ("lit", float(text) if "." in text else int(text))
        if kind == "str":
            return ("lit", self.string())
        if kind == "op" and text == "(":
            self.i += 1
            node = self.or_()
            self.expect_op(")")
            return node
        if kind != "id":
            raise ValueError("unexpected token" if kind != "end" else "unexpected end")
        name = text.lower()
        self.i += 1
        if name in ("true", "false"):
            return ("lit", name == "true")
        if name == "null":
            return ("lit", None)
        if name == "date":
            try:
                return ("lit", date.fromisoformat(self.string()))
            except ValueError as exc:
                raise ValueError("invalid DATE literal (expected YYYY-MM-DD)") from exc
        if name == "cast":
            self.expect_op("(")
            inner = self.or_()
            if not self.kw("as"):
                raise ValueError("expected AS in CAST")
            target = self.word()
            if target not in _CAST_TYPES:
                raise ValueError("CAST target type not allowed")
            self.i += 1
            self.expect_op(")")
            return ("cast", inner, target)
        if name in _KEYWORDS:
            raise ValueError(f"unexpected keyword {name.upper()}")
        if self.peek() == ("op", "("):
            return self.call(name)
        if name not in self.cols:
            raise ValueError(f"identifier '{name}' is not a declared column")
        self.used.add(name)
        return ("col", name)

    def call(self, name: str) -> Node:
        if name not in _FUNCTIONS:
            raise ValueError(f"function '{name}' is not allowed")
        self.expect_op("(")
        args = [self.or_()]
        while self.op(","):
            args.append(self.or_())
        self.expect_op(")")
        lo, hi = _FUNCTIONS[name]
        if not lo <= len(args) <= hi:
            raise ValueError(f"wrong number of arguments for {name.upper()}")
        return ("call", name, args)


def parse(text: str, columns: Sequence[str]) -> Node:
    """Parse a boolean predicate; ValueError (no data in message) if outside the whitelist."""
    p = _Parser(_tokenize(text), frozenset(columns))
    node = p.or_()
    if p.peek()[0] != "end":
        raise ValueError("unexpected trailing tokens")
    if node[0] not in _BOOL_KINDS:
        raise ValueError("expression must be a boolean predicate (comparison, AND/OR/NOT, IS NULL)")
    return node


# --- evaluation (three-valued logic: None = unknown) -----------------------------------------


def _num(v: Any) -> int | float:
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise TypeError("numeric operand expected")
    return v


def _cmp_operands(a: Any, b: Any) -> None:
    kinds = {
        "n" if isinstance(x, int | float) and not isinstance(x, bool) else type(x) for x in (a, b)
    }
    if len(kinds) != 1:
        raise TypeError("incompatible operand types")


def _cast(v: Any, target: str) -> Any:
    if target == "varchar":
        return str(v)
    if target == "date":
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            return date.fromisoformat(v.strip())
        raise TypeError("cannot cast to DATE")
    if isinstance(v, bool) or not isinstance(v, int | float | str):
        raise TypeError("cannot cast")
    if target == "integer":
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("non-finite")
        return int(v) if not isinstance(v, str) else int(v.strip())
    return float(v)


_CMP = {
    "=": lambda a, b: a == b, "<>": lambda a, b: a != b, "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
}  # fmt: skip


def _ev(n: Node, row: Mapping[str, Value]) -> Any:
    k = n[0]
    if k == "lit":
        return n[1]
    if k == "col":
        v = row[n[1]]
        return v.date() if isinstance(v, datetime) else v
    if k in ("and", "or"):
        a = _ev(n[1], row)
        short = k == "or"
        if a is short:
            return short
        b = _ev(n[2], row)
        if b is short:
            return short
        return None if a is None or b is None else not short
    if k == "not":
        v = _ev(n[1], row)
        return None if v is None else not v
    if k == "isnull":
        return (_ev(n[1], row) is None) != n[2]
    if k == "neg":
        v = _ev(n[1], row)
        return None if v is None else -_num(v)
    if k == "bin":
        a, b = _ev(n[2], row), _ev(n[3], row)
        if a is None or b is None:
            return None
        x, y = _num(a), _num(b)
        return {"+": x + y, "-": x - y, "*": x * y}[n[1]] if n[1] != "/" else x / y
    if k == "cmp":
        a, b = _ev(n[2], row), _ev(n[3], row)
        if a is None or b is None:
            return None
        _cmp_operands(a, b)
        return _CMP[n[1]](a, b)
    if k == "cast":
        v = _ev(n[1], row)
        return None if v is None else _cast(v, n[2])
    if k == "call":
        vals = [_ev(a, row) for a in n[2]]
        if n[1] == "abs":
            return None if vals[0] is None else abs(_num(vals[0]))
        return next((v for v in vals if v is not None), None)
    raise ValueError("unknown node")  # unreachable: parse only builds the kinds above


def evaluate_predicate(node: Node, row: Mapping[str, Value]) -> bool | None:
    """True/False, or None when unknown (a NULL input). TypeError/ValueError/ArithmeticError
    (type mismatch, division by zero, bad cast) propagate: the caller decides ("not evaluable")."""
    result = _ev(node, row)
    if result is not None and not isinstance(result, bool):
        raise TypeError("predicate did not yield a boolean")
    return result
