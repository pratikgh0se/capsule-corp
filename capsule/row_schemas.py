"""Pinned schemas for operational rows (row 569): `contracts/schemas/rows/<name>.json`."""
from capsule import answer_base
from capsule.jsonschema import load_schema, validate

ROWS = answer_base.SCHEMAS / "rows"


def load(name):
    return load_schema(ROWS / f"{name}.json")


def problems(name, doc):
    """[] or one `#/path: message` per violation of schema `name`."""
    return validate(doc, load(name))


def require(name, doc):
    """Raise ValueError naming the schema and its violations; return `doc` when it is valid."""
    found = problems(name, doc)
    if found:
        raise ValueError(f"{name}: " + "; ".join(found))
    return doc
