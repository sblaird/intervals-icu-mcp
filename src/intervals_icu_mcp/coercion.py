"""Lenient coercion for scalar/array tool arguments.

Some MCP clients (observed with claude.ai's Custom Connector, 2026-07-01) send
integer and array tool arguments as JSON *strings* — ``"3"`` instead of ``3``,
``'["watts","heartrate"]'`` instead of ``["watts","heartrate"]``. The low-level
MCP SDK validates every call's arguments against the tool's ``inputSchema`` with
jsonschema in strict mode *before* the value ever reaches FastMCP's middleware or
pydantic (``mcp/server/lowlevel/server.py`` — ``jsonschema.validate`` runs ahead
of the tool dispatch). A bare ``int`` / ``list[str]`` parameter therefore rejects
the string form outright:

    Input validation error: '3' is not of type 'integer'
    Input validation error: '["watts","heartrate"]' is not valid under any of the given schemas

Because strict jsonschema runs first, a coercing middleware would run too late.
Instead, the affected parameters advertise a schema that *additively* accepts the
string form (the native integer / array-of-strings hint is preserved) and a
``BeforeValidator`` coerces the string back to the native type before the tool
body runs. Clients that send native types are completely unaffected.

Usage::

    from pydantic import WithJsonSchema
    from ..coercion import CoerceInt, int_schema

    limit: Annotated[int, CoerceInt, WithJsonSchema(int_schema("How many"))] = 30

The runtime type stays ``int`` / ``list[str] | None`` so tool bodies (and pyright)
see the native type; only the advertised JSON schema is widened.
"""

from __future__ import annotations

import json
from typing import Any, cast

from pydantic import BeforeValidator


def _coerce_int(value: Any) -> Any:
    """Turn a JSON-string integer (``"3"``) into an ``int``; pass everything else through."""
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value  # leave malformed input for normal validation to reject
    return value


def _coerce_str_list(value: Any) -> Any:
    """Turn a JSON-array string (``'["a","b"]'``) into a ``list``; pass everything else through."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return value
            if isinstance(parsed, list):
                return cast("list[Any]", parsed)
    return value


# Reusable pydantic BeforeValidators (metadata for Annotated types).
CoerceInt = BeforeValidator(_coerce_int)
CoerceStrList = BeforeValidator(_coerce_str_list)


def int_schema(description: str) -> dict[str, Any]:
    """JSON schema for an integer parameter that also accepts a JSON-string integer."""
    return {
        "anyOf": [{"type": "integer"}, {"type": "string"}],
        "description": description,
    }


def optional_str_list_schema(description: str) -> dict[str, Any]:
    """JSON schema for an optional ``list[str]`` that also accepts a JSON-array string."""
    return {
        "anyOf": [
            {"type": "array", "items": {"type": "string"}},
            {"type": "string"},
            {"type": "null"},
        ],
        "description": description,
    }
