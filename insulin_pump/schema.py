"""Minimal JSON Schema (draft 2020-12 subset) validator that also runs on MicroPython.

The profile schema file is the single source of truth for profile structure,
hard limits and default values. This validator implements only the keywords
listed below. If a schema uses any other *constraint* keyword, it raises
SchemaError rather than silently skipping a check it cannot perform.

Supported constraints: $ref (local "#/$defs/..."), type, enum, const, properties,
required, additionalProperties (boolean or schema), items, minItems, maxItems,
minimum, maximum, exclusiveMinimum, exclusiveMaximum, minLength, maxLength,
pattern.
Annotations (ignored for validation): $schema, $id, $comment, $defs, title,
description, default, examples, and any "x-..." extension keyword.

``x-decimals`` (an extension keyword) caps the number of decimal places, so every
number can be converted to an exact integer or rational value.
"""

import re

from .exact import DecimalError, is_int, parse_decimal

_CONSTRAINTS = (
    "$ref",
    "type",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
)
_ANNOTATIONS = (
    "$schema",
    "$id",
    "$comment",
    "$defs",
    "title",
    "description",
    "default",
    "examples",
)


class SchemaError(Exception):
    """The schema itself is invalid or uses an unsupported keyword."""


def _type_matches(value, expected):
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return value is True or value is False
    if expected == "integer":
        return is_int(value)
    if expected == "number":
        return is_int(value) or (
            isinstance(value, float) and value == value and value - value == 0
        )
    if expected == "null":
        return value is None
    raise SchemaError("unsupported type %r" % (expected,))


def _describe(path):
    return path or "profile"


def _number(value):
    return parse_decimal(value)


class Validator:
    def __init__(self, schema):
        if not isinstance(schema, dict):
            raise SchemaError("schema must be a JSON object")
        self.root = schema
        self._patterns = {}
        self._check_keywords(schema, "#")

    # ---- schema self-check -------------------------------------------------------

    def _check_keywords(self, node, where):
        if not isinstance(node, dict):
            return
        for key in node:
            if key.startswith("x-") or key in _ANNOTATIONS:
                continue
            if key not in _CONSTRAINTS:
                raise SchemaError("unsupported schema keyword %r at %s" % (key, where))
        if "$ref" in node:
            self.resolve(node)
        for key in ("properties", "$defs"):
            for name, sub in (node.get(key) or {}).items():
                self._check_keywords(sub, where + "/" + key + "/" + name)
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), dict):
                self._check_keywords(node[key], where + "/" + key)

    def resolve(self, node):
        """Follow $ref. Sibling keywords next to a $ref are merged in (2020-12 applies both);
        a constraint present on both sides would be ambiguous, so it is rejected."""
        seen = 0
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise SchemaError(
                    "only local $ref values are supported, got %r" % (ref,)
                )
            target = self.root
            for part in ref[2:].split("/"):
                if not isinstance(target, dict) or part not in target:
                    raise SchemaError("unresolvable $ref %r" % (ref,))
                target = target[part]
            if not isinstance(target, dict):
                raise SchemaError("$ref %r does not point at a schema object" % (ref,))
            merged = dict(target)
            for key, value in node.items():
                if key == "$ref":
                    continue
                if key in target and key in _CONSTRAINTS:
                    raise SchemaError(
                        "keyword %r is set both beside and inside $ref %r" % (key, ref)
                    )
                merged[key] = value
            node = merged
            seen += 1
            if seen > 20:
                raise SchemaError("$ref cycle at %r" % (ref,))
        return node

    # ---- defaults ------------------------------------------------------------------

    def apply_defaults(self, instance, node=None):
        """Fill in missing optional properties from schema `default` values (recursively)."""
        node = self.resolve(self.root if node is None else node)
        if isinstance(instance, dict) and isinstance(node.get("properties"), dict):
            for name, sub in node["properties"].items():
                sub_resolved = self.resolve(sub)
                if name not in instance and "default" in sub_resolved:
                    instance[name] = _deep_copy(sub_resolved["default"])
                if name in instance:
                    self.apply_defaults(instance[name], sub_resolved)
        elif isinstance(instance, list) and isinstance(node.get("items"), dict):
            for item in instance:
                self.apply_defaults(item, node["items"])
        return instance

    # ---- validation ----------------------------------------------------------------

    def validate(self, instance, node=None, path="", errors=None):
        errors = [] if errors is None else errors
        node = self.resolve(self.root if node is None else node)
        where = _describe(path)

        expected = node.get("type")
        if expected is not None:
            options = expected if isinstance(expected, list) else [expected]
            if not any(_type_matches(instance, t) for t in options):
                errors.append("%s must be of type %s" % (where, " or ".join(options)))
                return errors

        if "const" in node and not _equal(instance, node["const"]):
            errors.append("%s must be %r" % (where, node["const"]))
        if "enum" in node and not any(
            _equal(instance, option) for option in node["enum"]
        ):
            errors.append(
                "%s must be one of %s"
                % (where, ", ".join(repr(o) for o in node["enum"]))
            )

        if _type_matches(instance, "number"):
            self._check_number(instance, node, where, errors)
        if isinstance(instance, str):
            self._check_string(instance, node, where, errors)
        if isinstance(instance, list):
            self._check_array(instance, node, path, errors)
        if isinstance(instance, dict):
            self._check_object(instance, node, path, errors)
        return errors

    def _check_number(self, value, node, where, errors):
        try:
            exact = parse_decimal(value, node.get("x-decimals"))
        except DecimalError as exc:
            errors.append("%s %s" % (where, exc))
            return
        unit = node.get("x-unit", "")
        suffix = (" " + unit) if unit else ""
        if "minimum" in node and exact < _number(node["minimum"]):
            errors.append(
                "%s must be at least %s%s (got %s)"
                % (where, node["minimum"], suffix, value)
            )
        if "maximum" in node and exact > _number(node["maximum"]):
            errors.append(
                "%s must be at most %s%s (got %s)"
                % (where, node["maximum"], suffix, value)
            )
        if "exclusiveMinimum" in node and exact <= _number(node["exclusiveMinimum"]):
            errors.append(
                "%s must be greater than %s%s (got %s)"
                % (where, node["exclusiveMinimum"], suffix, value)
            )
        if "exclusiveMaximum" in node and exact >= _number(node["exclusiveMaximum"]):
            errors.append(
                "%s must be less than %s%s (got %s)"
                % (where, node["exclusiveMaximum"], suffix, value)
            )

    def _check_string(self, value, node, where, errors):
        if "minLength" in node and len(value) < node["minLength"]:
            errors.append(
                "%s must be at least %d character(s)" % (where, node["minLength"])
            )
        if "maxLength" in node and len(value) > node["maxLength"]:
            errors.append(
                "%s must be at most %d characters" % (where, node["maxLength"])
            )
        if "pattern" in node:
            pattern = node["pattern"]
            compiled = self._patterns.get(pattern)
            if compiled is None:
                compiled = self._patterns[pattern] = re.compile(pattern)
            if compiled.match(value) is None:
                errors.append("%s has an invalid format: %r" % (where, value))

    def _check_array(self, value, node, path, errors):
        where = _describe(path)
        if "minItems" in node and len(value) < node["minItems"]:
            errors.append("%s needs at least %d item(s)" % (where, node["minItems"]))
        if "maxItems" in node and len(value) > node["maxItems"]:
            errors.append("%s allows at most %d items" % (where, node["maxItems"]))
        if isinstance(node.get("items"), dict):
            for i, item in enumerate(value):
                self.validate(item, node["items"], "%s[%d]" % (path, i), errors)

    def _check_object(self, value, node, path, errors):
        where = _describe(path)
        properties = node.get("properties") or {}
        missing = [name for name in node.get("required", []) if name not in value]
        if missing:
            errors.append(
                "%s is missing required key(s): %s"
                % (where, ", ".join(sorted(missing)))
            )
        extra_rule = node.get("additionalProperties", True)
        for name in sorted(value):
            child = name if not path else path + "." + name
            if name in properties:
                self.validate(value[name], properties[name], child, errors)
            elif extra_rule is False:
                errors.append(
                    "%s has unknown key %r - check the spelling" % (where, name)
                )
            elif isinstance(extra_rule, dict):
                self.validate(value[name], extra_rule, child, errors)


def _equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if _type_matches(a, "number") and _type_matches(b, "number"):
        return parse_decimal(a) == parse_decimal(b)
    return a == b


def _deep_copy(value):
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value
