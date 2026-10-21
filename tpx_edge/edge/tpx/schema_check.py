"""Validate queries.py against a live (or recorded) introspection result."""
from __future__ import annotations

from typing import Iterable

from . import queries as Q


def _unwrap(t: dict) -> str | None:
    while t and t.get("kind") in ("NON_NULL", "LIST"):
        t = t.get("ofType") or {}
    return t.get("name") if t else None


def validate_schema(schema: dict) -> list[str]:
    """Return a list of human-readable problems (empty == all good)."""
    problems: list[str] = []
    types = {t["name"]: t for t in schema.get("types", []) if t.get("name")}
    query_type = types.get((schema.get("queryType") or {}).get("name") or "Query")
    if not query_type:
        return ["schema has no Query type?!"]
    qfields = {f["name"]: f for f in query_type.get("fields") or []}

    for name, spec in Q.EXPECTED_QUERIES.items():
        f = qfields.get(name)
        if not f:
            level = "note" if spec.get("optional") else "MISSING"
            problems.append(f"{level}: Query.{name} not in live schema")
            continue
        if spec.get("input"):
            arg_types = {a["name"]: _unwrap(a["type"]) for a in f.get("args") or []}
            if arg_types.get("input") != spec["input"]:
                problems.append(f"Query.{name}(input:) is {arg_types.get('input')!r}, expected {spec['input']!r}")
        payload = types.get(_unwrap(f["type"]) or "")
        if payload:
            have = {pf["name"] for pf in payload.get("fields") or []}
            for want in spec["fields"]:
                if want not in have:
                    problems.append(f"{payload['name']}.{want} not found (used by {name})")
            if "error" not in have:
                problems.append(f"note: {payload['name']} has no `error` field")

    mutation_type = (schema.get("mutationType") or {}).get("name")
    if mutation_type:
        problems.append(f"note: schema exposes mutations via {mutation_type}; this tool never calls them")
    return problems


def key_configuration_enum_values(schema: dict, enum_name: str = "KeyType") -> Iterable[str]:
    for t in schema.get("types", []):
        if t.get("name") == enum_name and t.get("enumValues"):
            return [v["name"] for v in t["enumValues"]]
    return []
