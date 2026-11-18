"""A small JSON Schema subset, enough to validate what the stages ask for.

Kept in-tree rather than pulling a dependency: the schemas here are fixed and
narrow, and the error strings are written to be fed straight back to a model on
the retry, so they need to name the offending path in plain language.
"""
from __future__ import annotations


class ValidationError(Exception):
    pass


def _type_ok(value, t: str) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "null":
        return value is None
    return True


def _validate(value, schema: dict, path: str, errs: list[str]) -> None:
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            errs.append(f"{path or 'root'}: expected {'/'.join(types)}, got "
                        f"{type(value).__name__}")
            return

    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path or 'root'}: {value!r} is not one of "
                    f"{', '.join(map(repr, schema['enum']))}")

    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path or 'root'}: missing required key {req!r}")
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                _validate(v, props[k], f"{path}.{k}" if path else k, errs)
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path or 'root'}: unexpected key {k!r}; allowed keys are "
                            f"{', '.join(sorted(props))}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path or 'root'}: needs at least {schema['minItems']} items, "
                        f"got {len(value)}")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                _validate(item, item_schema, f"{path}[{i}]", errs)


def validate(value, schema: dict) -> None:
    """Raise ValidationError listing every problem, so one retry can fix them all."""
    errs: list[str] = []
    _validate(value, schema, "", errs)
    if errs:
        raise ValidationError("; ".join(errs[:20]))
