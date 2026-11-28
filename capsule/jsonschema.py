"""A stdlib JSON-Schema subset (spec §7.3, row 416)."""
import json
import re
from pathlib import Path

SUPPORTED = {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
             "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum", "pattern"}
IGNORED = {"$schema", "$id", "title", "description"}
ENVELOPE = ("schema", "run_id", "task_id", "claim_lock")
TYPES = {"object": dict, "array": list, "string": str, "null": type(None)}


class SchemaError(ValueError):
    pass


def check_schema(schema, where="#"):
    if not isinstance(schema, dict):
        raise SchemaError(f"{where}: a schema must be an object")
    unknown = set(schema) - SUPPORTED - IGNORED
    if unknown:
        raise SchemaError(f"{where}: unsupported keyword(s) {sorted(unknown)}")
    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        raise SchemaError(f"{where}: additionalProperties may only be false")
    for key, sub in schema.get("properties", {}).items():
        check_schema(sub, f"{where}/properties/{key}")
    if "items" in schema:
        check_schema(schema["items"], f"{where}/items")


def load_schema(path):
    schema = json.loads(Path(path).read_text())
    check_schema(schema)
    return schema


def load_all(directory):
    return {p.name[:-len(".json")]: load_schema(p) for p in sorted(Path(directory).glob("*.json"))}


def _is(value, kind):
    if isinstance(kind, list):
        return any(_is(value, k) for k in kind)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    return isinstance(value, TYPES[kind])


def validate(doc, schema, where="#"):
    kind = schema.get("type")
    if kind is not None and not _is(doc, kind):
        return [f"{where}: expected {kind}"]
    errs = []
    if "const" in schema and doc != schema["const"]:
        errs.append(f"{where}: must equal {schema['const']!r}")
    if "enum" in schema and doc not in schema["enum"]:
        errs.append(f"{where}: must be one of {schema['enum']}")
    if isinstance(doc, str):
        if len(doc) < schema.get("minLength", 0):
            errs.append(f"{where}: shorter than {schema['minLength']}")
        if len(doc) > schema.get("maxLength", len(doc)):
            errs.append(f"{where}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], doc):
            errs.append(f"{where}: does not match {schema['pattern']}")
    if _is(doc, "number"):
        if "minimum" in schema and doc < schema["minimum"]:
            errs.append(f"{where}: below {schema['minimum']}")
        if "maximum" in schema and doc > schema["maximum"]:
            errs.append(f"{where}: above {schema['maximum']}")
    if isinstance(doc, list):
        if len(doc) < schema.get("minItems", 0):
            errs.append(f"{where}: fewer than {schema['minItems']} items")
        if len(doc) > schema.get("maxItems", len(doc)):
            errs.append(f"{where}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(doc):
                errs += validate(item, schema["items"], f"{where}/{i}")
    if isinstance(doc, dict):
        props = schema.get("properties", {})
        errs += [f"{where}: missing required field {k!r}" for k in schema.get("required", []) if k not in doc]
        if schema.get("additionalProperties") is False:
            errs += [f"{where}: unexpected field {k!r}" for k in doc if k not in props]
        for key, sub in props.items():
            if key in doc:
                errs += validate(doc[key], sub, f"{where}/{key}")
    return errs
