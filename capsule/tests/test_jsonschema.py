import unittest

from capsule.jsonschema import SchemaError, check_schema, validate


class Keywords(unittest.TestCase):
    CASES = [
        ({"type": "string"}, "x", True), ({"type": "string"}, 1, False),
        ({"type": "integer"}, 3, True), ({"type": "integer"}, True, False), ({"type": "integer"}, 3.5, False),
        ({"type": "number"}, 3.5, True), ({"type": "boolean"}, False, True), ({"type": "boolean"}, 0, False),
        ({"type": "array"}, [], True), ({"type": "object"}, {}, True), ({"type": "null"}, None, True),
        ({"enum": ["a", "b"]}, "b", True), ({"enum": ["a", "b"]}, "c", False),
        ({"const": "report.v1"}, "report.v1", True), ({"const": "report.v1"}, "report.v2", False),
        ({"minLength": 2}, "ab", True), ({"minLength": 2}, "a", False),
        ({"maxLength": 2}, "abc", False), ({"pattern": "^t_[0-9a-f]{8}$"}, "t_0000aaaa", True),
        ({"pattern": "^t_[0-9a-f]{8}$"}, "t_zz", False),
        ({"minimum": 0}, -1, False), ({"maximum": 255}, 256, False), ({"minimum": 0, "maximum": 255}, 7, True),
        ({"minItems": 1}, [], False), ({"maxItems": 1}, [1, 2], False),
        ({"items": {"type": "string"}}, ["a", 1], False),
        ({"required": ["a"]}, {}, False), ({"required": ["a"]}, {"a": 1}, True),
        ({"properties": {"a": {"type": "string"}}, "additionalProperties": False}, {"b": 1}, False),
        ({"properties": {"a": {"type": "string"}}}, {"a": 1}, False),
    ]

    def test_table(self):
        for schema, doc, ok in self.CASES:
            with self.subTest(schema=schema, doc=doc):
                self.assertEqual(validate(doc, schema) == [], ok)

    def test_messages_name_the_path(self):
        schema = {"type": "object", "required": ["commands"], "properties": {
            "commands": {"type": "array", "items": {"type": "object", "required": ["exit"]}}}}
        self.assertEqual(validate({"commands": [{}]}, schema), ["#/commands/0: missing required field 'exit'"])

    def test_wrong_type_stops_descent(self):
        self.assertEqual(validate("x", {"type": "object", "required": ["a"]}), ["#: expected object"])


class SchemaCheck(unittest.TestCase):
    def test_unsupported_keyword_is_an_error(self):
        for bad in ({"oneOf": []}, {"$ref": "#/x"}, {"properties": {"a": {"allOf": []}}}, {"format": "date"}):
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                check_schema(bad)

    def test_additional_properties_only_false(self):
        with self.assertRaises(SchemaError):
            check_schema({"additionalProperties": {"type": "string"}})

    def test_ignored_keywords_pass(self):
        check_schema({"$schema": "x", "$id": "y", "title": "t", "description": "d", "type": "object"})
