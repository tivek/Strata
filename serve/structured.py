"""Independent validation of the complete document before HTTP success.

json_schema requires jsonschema and a constrained decoder (checked by Service).
json_object requires only the standard library. Neither format extracts or repairs JSON.
"""
import json
import ipaddress
import re
import threading
from datetime import date
from uuid import UUID


class StructuredOutputError(RuntimeError):
    pass


class _ObjectOnly:
    """The validator for json_object: one complete JSON object."""

    def iter_errors(self, value):
        if not isinstance(value, dict):
            yield _ObjectError()


class _ObjectError:
    absolute_path = ()
    message = "the answer is not a JSON object"


_jsonschema = None                     # (validators, SchemaError, Registry, NoSuchResource), False when not installed
_jsonschema_lock = threading.Lock()


def jsonschema_modules():
    """Import on first use. Missing dependencies are fatal to schema requests, not ordinary inference."""
    global _jsonschema
    with _jsonschema_lock:
        if _jsonschema is None:
            try:
                from jsonschema import validators
                from jsonschema.exceptions import SchemaError
                from referencing import Registry
                from referencing.exceptions import NoSuchResource
                _jsonschema = (validators, SchemaError, Registry, NoSuchResource)
            except ImportError:
                _jsonschema = False
        return _jsonschema or None


# llguidance 1.9.1 targets Draft 2020-12. Its compiler remains the authority for
# supported intersections, disjoint oneOf branches, regexes and formats. Reject
# extensions (especially x-guidance.lenient) before the compiler sees them.
_KEYWORDS = frozenset((
    "$schema", "$ref", "$defs", "definitions", "type", "const", "enum",
    "properties", "patternProperties", "additionalProperties", "required", "minProperties", "maxProperties",
    "items", "prefixItems", "minItems", "maxItems", "minLength", "maxLength", "pattern", "format",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "anyOf", "oneOf", "allOf",
    "title", "description", "default", "examples", "$comment", "deprecated", "readOnly", "writeOnly",
))


_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})*", re.ASCII)
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
# RFC 5321 dot-string / quoted-string, including quoted-pairSMTP.
_MAIL_LOCAL = re.compile(rf'(?:{_ATOM}(?:\.{_ATOM})*|"(?:[\x20-\x21\x23-\x5b\x5d-\x7e]|\\[\x20-\x7e])*")', re.ASCII)
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?"
                   r"(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", re.ASCII)


def _hostname(value):
    if not isinstance(value, str):
        return True
    if len(value) > 253 or not _HOSTNAME.fullmatch(value):
        return False
    # ASCII A-labels still need IDNA validation; an LDH regex alone accepts
    # malformed Punycode and forbidden decoded characters/context rules.
    from idna import ulabel
    for label in value.split("."):
        if label.lower().startswith("xn--"):
            ulabel(label)
    return True


def _email(value):
    if not isinstance(value, str):
        return True
    local, at, domain = value.rpartition("@")
    if not at or len(value) > 254 or len(local) > 64 or not _MAIL_LOCAL.fullmatch(local):
        return False
    if domain.startswith("[") and domain.endswith("]"):
        address = domain[1:-1]
        if address.lower().startswith("ipv6:"):
            return not ipaddress.IPv6Address(address[5:]).scope_id
        return bool(ipaddress.IPv4Address(address))
    return bool(domain and not domain.endswith(".") and _hostname(domain))


def _uuid(value):
    return not isinstance(value, str) or str(UUID(value)) == value.lower()


def _time(value):
    # RFC 3339 without leap seconds: fail closed rather than guess leap-second dates.
    return not isinstance(value, str) or bool(_TIME.fullmatch(value))


def _datetime(value):
    if not isinstance(value, str):
        return True
    if len(value) < 11 or value[10] not in "Tt" or not _time(value[11:]):
        return False
    return bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value[:10]) and date.fromisoformat(value[:10]))


def _format_checker():
    """An explicit verified set, independent of whichever optional packages happen to be installed."""
    from jsonschema import FormatChecker
    # These pinned implementations use datetime.date and ipaddress, including scope rejection.
    checker = FormatChecker(formats=("date", "ipv4", "ipv6"))
    for name, check in (("email", _email), ("hostname", _hostname), ("uuid", _uuid),
                        ("time", _time), ("date-time", _datetime)):
        checker.checks(name, raises=ValueError)(check)
    return checker


def check_supported_schema(schema):
    """Visit schema positions only: property names and enum/const data are not keywords."""
    def visit(node):
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise ValueError("response_format schema must contain schema objects or booleans")
        for key, value in node.items():
            if key not in _KEYWORDS:
                raise ValueError(f"unsupported response_format schema keyword: {key}")
            if key == "$schema" and value.rstrip("#") != "https://json-schema.org/draft/2020-12/schema":
                raise ValueError("response_format supports only JSON Schema Draft 2020-12")
            if key == "$ref" and (not isinstance(value, str) or not (value == "#" or value.startswith("#/"))):
                raise ValueError("response_format supports only local schema references (# or #/...)")
            if key in ("properties", "patternProperties", "$defs", "definitions"):
                for child in value.values():
                    visit(child)
            elif key in ("anyOf", "oneOf", "allOf", "prefixItems"):
                for child in value:
                    visit(child)
            elif key in ("additionalProperties", "items"):
                visit(value)
    visit(schema)


def _only_objects(node, root, refs=()):
    """True when every value `node` accepts is a JSON object, so "return one JSON object" stays true.

    `type: object`, an anyOf/oneOf whose branches all qualify (e.g. a root union of object shapes, which llama.cpp's
    grammar path accepts and apps send), an allOf with a qualifying member, or a local `$ref` to one of these.
    Anything that can also be an array, string, number, boolean or null (including `type: ["object", "null"]`) is
    not, and a `$ref` cycle never qualifies.
    """
    if not isinstance(node, dict):
        return False
    if node.get("type") == "object":
        return True
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#") and ref not in refs:
        target = root
        for part in ref[1:].split("/")[1:]:
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return False
            target = target[part]
        return _only_objects(target, root, refs + (ref,))
    for key in ("anyOf", "oneOf"):
        branches = node.get(key)
        if isinstance(branches, list) and branches and all(_only_objects(b, root, refs) for b in branches):
            return True
    branches = node.get("allOf")
    return isinstance(branches, list) and any(_only_objects(b, root, refs) for b in branches)


def prepare_format(response_format, messages, with_tools=False):
    if response_format is None:
        return messages, None
    if not isinstance(response_format, dict):
        raise ValueError("response_format must be an object")
    kind = response_format.get("type")
    if kind == "text":
        return messages, None
    if kind == "json_object":
        schema = {"type": "object"}
        modules = None                 # the standard library is enough for "one JSON object"
    elif kind == "json_schema":
        spec = response_format.get("json_schema")
        if not isinstance(spec, dict) or not isinstance(spec.get("schema"), dict):
            raise ValueError("response_format.json_schema needs a schema object")
        if not isinstance(spec.get("name"), str) or not spec["name"]:
            raise ValueError("response_format.json_schema needs a name")
        if "strict" in spec and not isinstance(spec["strict"], bool):
            raise ValueError("response_format.json_schema.strict must be boolean")
        schema = spec["schema"]
        if not _only_objects(schema, schema):
            raise ValueError("response_format schema must accept only JSON objects at its root "
                             "(type object, or anyOf/oneOf of object schemas)")
        modules = jsonschema_modules()
        if modules is None:
            raise StructuredOutputError('json_schema requires jsonschema; install "jsonschema>=4.23,<5"')
    else:
        raise ValueError("response_format.type must be text, json_object or json_schema")

    if modules is None:
        validator = _ObjectOnly()
    else:
        validators, SchemaError, Registry, NoSuchResource = modules

        def no_remote(uri):
            raise NoSuchResource(ref=uri)

        try:
            cls = validators.Draft202012Validator
            cls.check_schema(schema)
            check_supported_schema(schema)
            checker = _format_checker()
            # Recognition by jsonschema alone does not establish strict semantics.
            def check_formats(node):
                if isinstance(node, dict):
                    if "format" in node and node["format"] not in checker.checkers:
                        raise ValueError(f"independent validator does not support format: {node['format']}")
                    for key in ("properties", "patternProperties", "$defs", "definitions"):
                        for child in node.get(key, {}).values():
                            check_formats(child)
                    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
                        for child in node.get(key, []):
                            check_formats(child)
                    for key in ("additionalProperties", "items"):
                        check_formats(node.get(key))
            check_formats(schema)
            validator = cls(schema, registry=Registry(retrieve=no_remote), format_checker=checker)
        except SchemaError as exc:
            raise ValueError(f"invalid response_format schema: {exc.message}") from exc
    directive = ("OUTPUT FORMAT REQUIREMENT: Return exactly one JSON object matching the JSON Schema below. "
                 "No Markdown, headings, code fences, commentary, or text outside the JSON. "
                 "Use every required field, correct types, and only allowed fields. "
                 "Put all requested writing inside the appropriate JSON string fields.\nJSON Schema:\n" +
                 json.dumps(schema, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    if with_tools:
        # /v1/responses (#782): the schema is for the final answer; a turn that calls a tool is not an answer
        directive += ("\nThis applies only to your final answer. To use a tool, call it as usual; the JSON object is "
                      "what you write once you are done with the tools.")
    messages = [dict(message) for message in messages]
    if messages and messages[0].get("role") == "system":
        content = messages[0].get("content") or ""
        if isinstance(content, list):
            messages[0]["content"] = content + [{"type": "text", "text": directive}]
        else:
            messages[0]["content"] = content + "\n\n" + directive
    else:
        messages.insert(0, {"role": "system", "content": directive})
    return messages, validator


def unique_json_object(items):
    """json.loads object_pairs_hook: reject ambiguity before any object member is lost."""
    obj = {}
    for key, value in items:
        if key in obj:
            raise ValueError(f"duplicate JSON key: {key}")
        obj[key] = value
    return obj


def validated_json(text, validator, finish):
    def constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    if finish != "stop":
        raise StructuredOutputError(f"structured output was incomplete (finish_reason={finish}); increase the output budget")
    try:
        value = json.loads(text, object_pairs_hook=unique_json_object, parse_constant=constant)
        canonical = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        canonical.encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise StructuredOutputError(f"model did not return valid JSON: {exc}") from exc
    try:
        error = next(validator.iter_errors(value), None)
    except Exception as exc:
        raise StructuredOutputError(f"could not validate structured output: {exc}") from exc
    if error is not None:
        path = "/" + "/".join(str(part) for part in error.absolute_path)
        raise StructuredOutputError(f"model output failed the JSON Schema at {path}: {error.message}")
    return canonical
