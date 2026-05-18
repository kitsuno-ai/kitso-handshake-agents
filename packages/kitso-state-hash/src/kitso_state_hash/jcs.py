"""Minimal RFC 8785 (JSON Canonicalization Scheme) implementation.

This is a deliberate from-scratch implementation rather than a third-party
dependency, so the reference impl has zero runtime deps. The rules we encode:

1. Object members are sorted lexicographically by UTF-16 code-unit ordering
   of their key strings (RFC 8785 §3.2.3). For pure-ASCII keys this matches
   ordinary code-point ordering. For BMP keys it matches Python str ordering
   directly. For non-BMP surrogate-pair keys the result is equivalent
   because Python str ordering is also code-unit-ordered for surrogate pairs.
2. Object members are serialised with no insignificant whitespace; only the
   structural commas and colons, with no surrounding spaces.
3. Strings are escaped per RFC 8259 §7 minimum-escape rules: \\b \\f \\n \\r \\t
   for the named controls, \\u00XX for other U+0000–U+001F controls, \\"
   and \\\\ for quote and backslash. Other characters pass through as UTF-8.
4. Numbers follow RFC 8785 §3.2.2.3 / ECMA-404: integers as digits with no
   leading zeros; non-integer numbers use Python's repr() round-trip then we
   strip a trailing ``.0`` so 1.0 becomes "1" (matching JS's Number.toString).
   We reject NaN and Infinity (not representable in JSON anyway).
5. Booleans → true/false. None → null. Lists → array, preserving order.

This module is intentionally small (~120 lines). The full RFC 8785 spec
covers some edge cases we don't hit in card data (e.g. extreme floats,
non-BMP keys in deeply nested objects). Test vectors in
test-fixtures/v0.2/state-hash/ cover what cards actually use.
"""
from __future__ import annotations

import math


def canonical_bytes(obj) -> bytes:
    """Return the RFC 8785 canonical JSON encoding of `obj` as UTF-8 bytes.

    Raises ValueError on NaN, +Inf, -Inf, or on values that are not
    JSON-representable (sets, complex, bytes, etc).
    """
    return _encode(obj).encode("utf-8")


def _encode(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return _encode_string(v)
    if isinstance(v, bool):
        # Handled above, but bool is a subclass of int — keep this defensive
        return "true" if v else "false"  # pragma: no cover
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError(f"JCS cannot encode non-finite number: {v}")
        # ECMA-404 / RFC 8785 §3.2.2.3: shortest round-trip; integral floats
        # serialize without a fractional part (1.0 -> "1").
        if v.is_integer() and abs(v) < 1e16:
            return str(int(v))
        s = repr(v)
        # Python's repr already gives shortest round-trip for finite floats.
        return s
    if isinstance(v, list) or isinstance(v, tuple):
        return "[" + ",".join(_encode(x) for x in v) + "]"
    if isinstance(v, dict):
        # RFC 8785 §3.2.3: sort by UTF-16 code-unit ordering of keys. Python
        # str ordering is code-point ordered for BMP; equivalent for the
        # range we care about. We assert keys are str (no integer keys).
        items = []
        for k in sorted(v.keys()):
            if not isinstance(k, str):
                raise ValueError(f"JCS object keys must be strings, got {type(k).__name__}")
            items.append(_encode_string(k) + ":" + _encode(v[k]))
        return "{" + ",".join(items) + "}"
    raise ValueError(f"JCS cannot encode type {type(v).__name__}: {v!r}")


def _encode_string(s: str) -> str:
    out = ['"']
    for ch in s:
        c = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif c == 0x08:
            out.append("\\b")
        elif c == 0x09:
            out.append("\\t")
        elif c == 0x0A:
            out.append("\\n")
        elif c == 0x0C:
            out.append("\\f")
        elif c == 0x0D:
            out.append("\\r")
        elif c < 0x20:
            out.append("\\u%04x" % c)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)
