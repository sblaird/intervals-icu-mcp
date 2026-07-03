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


# ---------------------------------------------------------------------------
# Global schema widening
#
# Rather than annotate all ~60 int/float/array tool parameters one-by-one, widen
# every tool's advertised ``inputSchema`` in one post-registration pass so the
# strict pre-dispatch ``jsonschema.validate`` (see module docstring) accepts the
# JSON-string form of any numeric or array argument. FastMCP's own pydantic layer
# then coerces ``"3" -> 3`` / ``"4.5" -> 4.5`` before the tool body runs, so
# scalar params need nothing further. JSON-array *strings* are the exception —
# pydantic won't parse them, so array params still need ``CoerceStrList`` (only
# ``streams`` today). Widening is idempotent, so params already carrying an
# explicit string branch (limit/days_back/streams) are left untouched.
# ---------------------------------------------------------------------------

_WIDENABLE_TYPES = frozenset({"integer", "number", "array"})


def _widen_property_schema(prop: dict[str, Any]) -> bool:
    """Widen one property schema in place to also accept a JSON string.

    Handles the plain ``{"type": ...}`` form (required params) and the ``anyOf``
    form (optional params, which carry a ``null`` branch). Returns True if the
    schema was modified; idempotent for already-widened params.
    """
    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        branches = cast("list[dict[str, Any]]", any_of)
        kinds = {b.get("type") for b in branches}
        if kinds & _WIDENABLE_TYPES and "string" not in kinds:
            branches.append({"type": "string"})
            return True
        return False

    if prop.get("type") in _WIDENABLE_TYPES:
        # Preserve description as a sibling of anyOf (nicer for clients); move the
        # original type + any constraints (items, minimum, ...) into the first branch.
        description = prop.pop("description", None)
        original = dict(prop)
        prop.clear()
        prop["anyOf"] = [original, {"type": "string"}]
        if description is not None:
            prop["description"] = description
        return True

    return False


def widen_tool_schemas_for_string_args(server: Any) -> int:
    """Widen every registered tool's numeric/array params to accept string forms.

    Call once, after all tools are registered. Returns the number of params
    widened (handy for a startup log). Reaches into FastMCP's tool manager, which
    stores the live tool objects whose ``parameters`` dict is what gets served and
    validated against.
    """
    tools = cast(
        "dict[str, Any]",
        server._tool_manager._tools,  # noqa: SLF001 — FastMCP has no public accessor
    )
    widened = 0
    for tool in tools.values():
        schema = getattr(tool, "parameters", None)
        if not isinstance(schema, dict):
            continue
        schema_dict = cast("dict[str, Any]", schema)
        properties = cast("dict[str, Any]", schema_dict.get("properties", {}))
        for prop in properties.values():
            if isinstance(prop, dict) and _widen_property_schema(cast("dict[str, Any]", prop)):
                widened += 1
    return widened
