"""Deterministic checks of the successful-response boundary, independent of GPU inference."""
import concurrent.futures
import copy
import json
import random
import threading
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from serve import constrain as C, structured
from serve.frontend import ChatTemplate, Event, ToolCall
from serve.server import ByteTokenizer, MockEngine, Service, serve, structured_chunks
from serve import test_constrain as fixtures


SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}},
          "required": ["n"], "additionalProperties": False}


def fmt(schema=SCHEMA):
    return {"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}}


class Validation(unittest.TestCase):
    def test_acceptance_client_preserves_unicode_sse_data(self):
        from tools.check_structured_output import response_value
        value = {"line\u2028key": "paragraph\u2029value"}
        text = json.dumps(value, ensure_ascii=False)
        for responses in (False, True):
            events = ([{"type": "response.output_text.delta", "delta": text},
                       {"type": "response.completed", "response": {"output": [
                           {"type": "message", "content": [{"text": text}]}]}}] if responses else
                      [{"choices": [{"delta": {"content": text}}]}])
            wire = "".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events)
            with self.subTest(responses=responses):
                self.assertEqual(response_value(wire, stream=True, responses=responses), value)

    def test_generated_schemas_are_reproducible_diverse_and_satisfiable(self):
        from jsonschema import Draft202012Validator, ValidationError
        from tools.check_structured_output import generated_schema, schema_structure
        shapes, features = set(), set()
        for seed in (3800, 3801):
            rng = random.Random(seed)
            for index in range(64):
                case_seed = rng.randrange(2**32)
                case = generated_schema(case_seed, index % 4)
                self.assertEqual(case, generated_schema(case_seed, index % 4))
                schema = case["schema"]
                Draft202012Validator.check_schema(schema)
                validator = Draft202012Validator(schema)
                validator.validate(case["witness"])
                # A plausible wrong answer must fail the same original schema.
                invalid = dict(case["witness"])
                del invalid[schema["required"][0]]
                with self.assertRaises(ValidationError):
                    validator.validate(invalid)
                shapes.add(json.dumps(schema_structure(schema), sort_keys=True))
                features.update(case["features"])
        self.assertGreater(len(shapes), 64)
        self.assertLessEqual({"nested_references", "unusual_strings", "numeric_boundaries",
                              "constraint_combinations", "arrays", "nested_objects", "disjoint_unions"}, features)

    def test_generated_campaign_retains_and_rejects_a_schema_invalid_success(self):
        from jsonschema import ValidationError
        from tools import check_structured_output as client
        report = {}
        with mock.patch.object(client, "request", return_value=(200, {
                "choices": [{"message": {"content": "{}"}}]})), self.assertRaises(ValidationError):
            client.generated_campaign("http://unused", 4, 3800, report, 15)
        self.assertFalse(report["generated_campaign"]["passed"])
        record = report["generated_campaign"]["checks"][0]
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["value"], {})  # retain the counterexample, not just an exception summary

    def test_structured_chunks_rejects_every_tool_delta_before_releasing_content(self):
        _, validator = structured.prepare_format({"type": "json_object"}, [])
        for tool_calls in (None, [], [{"index": 0, "function": {"name": "f", "arguments": "{}"}}]):
            for progress in (False, True):
                closed, released = [], []

                def chunks():
                    base = {"id": "chat_test", "created": 0, "model": "test"}
                    try:
                        yield {**base, "choices": [{"delta": {"content": '{"n":7}'}, "finish_reason": None}]}
                        chunk = {**base, "choices": [{"delta": {"tool_calls": tool_calls}, "finish_reason": None}]}
                        if progress:
                            chunk["prompt_progress"] = {"read": 1}
                        yield chunk
                        yield {**base, "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {}}
                    finally:
                        closed.append(True)

                with self.subTest(tool_calls=tool_calls, progress=progress):
                    with self.assertRaisesRegex(structured.StructuredOutputError, "tool"):
                        for chunk in structured_chunks(chunks(), validator):
                            if chunk is not None:
                                released.append(chunk)
                    self.assertEqual(released, [])
                    self.assertEqual(closed, [True])

    def test_official_format_cases_with_documented_leap_second_restriction(self):
        checker = structured._format_checker()
        cases, restricted = 0, 0
        for name in checker.checkers:
            path = Path(__file__).parent / "fixtures/structured-formats" / f"{name}.json"
            for group in json.loads(path.read_text()):
                for test in group["tests"]:
                    expected = test["valid"]
                    # The eight upstream-valid leap-second values are explicitly unsupported.
                    if (name in ("time", "date-time") and expected and isinstance(test["data"], str)
                            and ":60" in test["data"]):
                        expected = False
                        restricted += 1
                    with self.subTest(format=name, case=test["description"], value=test["data"]):
                        self.assertEqual(checker.conforms(test["data"], name), expected)
                    cases += 1
        self.assertEqual(cases, 442)
        self.assertEqual(restricted, 8)

    def test_acceptance_client_rejects_invalid_json_and_inconsistent_streams(self):
        from tools.check_structured_output import response_value
        for text in ('{"n":1,"n":2}', '{"n":NaN}', '{"n":1e999}', '[]', '{} trailing'):
            with self.subTest(text=text), self.assertRaises((AssertionError, ValueError)):
                response_value({"choices": [{"message": {"content": text}}]})
        events = [{"type": "response.output_text.delta", "delta": '{"n":7}'},
                  {"type": "response.completed", "response": {"output": [
                      {"type": "message", "content": [{"text": "{}"}]}]}}]
        with self.assertRaises(AssertionError):
            response_value("\n".join("data: " + json.dumps(e) for e in events), stream=True, responses=True)

    def test_randomized_campaign_rejects_a_schema_invalid_http_success(self):
        from jsonschema import ValidationError
        from tools import check_structured_output as client

        def invalid_success(base, path, body, timeout):
            # This violates every selected fixture, including the format fixtures' valid-value enums.
            text = '{"value":"a@"}'
            responses = path == "/v1/responses"
            message = {"type": "message", "content": [{"text": text}]}
            if body["stream"]:
                events = ([{"type": "response.output_text.delta", "delta": text},
                           {"type": "response.completed", "response": {"output": [message]}}] if responses else
                          [{"choices": [{"delta": {"content": text}}]}])
                return 200, "\n".join("data: " + json.dumps(e) for e in events)
            return 200, {"output": [message]} if responses else {"choices": [{"message": {"content": text}}]}

        report = {}
        with mock.patch.object(client, "request", side_effect=invalid_success), self.assertRaises(ValidationError):
            client.campaign("http://unused", 128, 3800, report, 15)
        self.assertFalse(report["campaign"]["passed"])

    def test_verified_formats_enforce_semantics_without_the_decoder(self):
        cases = {
            "email": (["a@example.com", "a+b@sub.example", '"a@b"@example.com',
                       "a@[127.0.0.1]", "a@[IPv6:::1]"],
                      ["a@", "@example.com", "a..b@example.com", "a@-bad.test", "a@bad-.test",
                       "a@exa_mple.com", "a@example..com", "a@example.com.", "a(comment)@example.com",
                       "a@example.com\n", "a@[300.1.1.1]", "a@[IPv6:::1%eth0]", "é@example.com"]),
            "hostname": (["example.com", "localhost", "xn--bcher-kva.example"],
                         ["", "example.", "example..", "a..b", "-a.example", "a-.example", "a_b.example", "é.example",
                          "a" * 64, "xn--X", "xn--hello-txk", "XN--aa---o47jg78q"]),
            "uuid": (["f81d4fae-7dec-11d0-a765-00a0c91e6bf6", "F81D4FAE-7DEC-11D0-A765-00A0C91E6BF6"],
                     ["f81d4fae-7dec-11d0-a765-00a0c91e6bf6---", "f81d4fae7dec11d0a76500a0c91e6bf6",
                      "urn:uuid:f81d4fae-7dec-11d0-a765-00a0c91e6bf6"]),
            "date": (["2024-02-29"], ["2023-02-29", "2024-13-01", "20240101", "2024-01-01\n"]),
            "time": (["12:34:56Z", "12:34:56.123+02:00", "00:00:00-23:59"],
                     ["12:34:56", "24:00:00Z", "12:60:00Z", "12:00:60Z", "12:00:00+24:00", "12:00:00Z\n"]),
            "date-time": (["2024-02-29T12:34:56Z", "2024-02-29t12:34:56.123z"],
                          ["2023-02-29T12:34:56Z", "2024-02-29 12:34:56Z", "2024-02-29T12:34:56"]),
            "ipv4": (["127.0.0.1"], ["300.1.1.1", "127.00.0.1", "127.1", "127.0.0.1\n"]),
            "ipv6": (["::1", "2001:db8::1"], ["::g", "::1%eth0", "127.0.0.1", "::1\n"]),
        }
        for name, (valid, invalid) in cases.items():
            schema = {"type": "object", "properties": {"v": {"type": "string", "format": name}}, "required": ["v"]}
            _, validator = structured.prepare_format(fmt(schema), [])
            for value in valid:
                with self.subTest(format=name, valid=value):
                    structured.validated_json(json.dumps({"v": value}), validator, "stop")
            for value in invalid:
                with self.subTest(format=name, invalid=value), self.assertRaises(structured.StructuredOutputError):
                    structured.validated_json(json.dumps({"v": value}), validator, "stop")

    def test_format_checkers_are_explicit_and_ignore_non_strings(self):
        checker = structured._format_checker()
        self.assertEqual(set(checker.checkers), {"email", "hostname", "uuid", "date", "time", "date-time", "ipv4", "ipv6"})
        for name in checker.checkers:
            for value in (None, 1, 1.5, True, [], {}):
                with self.subTest(format=name, value=value):
                    self.assertTrue(checker.conforms(value, name))

    def test_email_mailbox_length_boundaries(self):
        checker = structured._format_checker()
        longest = "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 61
        self.assertEqual(len(longest), 254)
        self.assertTrue(checker.conforms(longest, "email"))
        self.assertFalse(checker.conforms(longest + "d", "email"))
        self.assertFalse(checker.conforms("a" * 65 + "@example.com", "email"))

    def test_unverified_formats_are_rejected_in_nested_schema_positions(self):
        child = {"type": "string", "format": "idn-email"}
        positions = [{"format": "idn-email"}, {"properties": {"v": child}}, {"patternProperties": {"^v$": child}},
                     {"$defs": {"unused": child}}, {"definitions": {"unused": child}},
                     {"items": child}, {"additionalProperties": child}]
        positions.extend({key: [child]} for key in ("anyOf", "oneOf", "allOf", "prefixItems"))
        for position in positions:
            with self.subTest(position=position), self.assertRaisesRegex(ValueError, "does not support format"):
                structured.prepare_format(fmt({"type": "object", **position}), [])
        # Data and annotations are not schema positions.
        schema = {"type": "object", "const": {"format": "idn-email"}, "examples": [{"format": "idn-email"}]}
        _, validator = structured.prepare_format(fmt(schema), [])
        structured.validated_json('{"format":"idn-email"}', validator, "stop")

    def test_plain_inference_without_optional_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        code = '''
import sys, threading
from pathlib import Path
sys.modules['llguidance'] = None
sys.modules['jsonschema'] = None
from serve.server import ByteTokenizer, MockEngine, Service
from serve.frontend import ChatTemplate
tok = ByteTokenizer()
svc = Service(MockEngine(tok, 'ordinary answer'), tok, ChatTemplate(Path('serve/chat_template.jinja')))
events = list(svc.run(tok.encode('hello'), False, None, 100, {}, threading.Event()))
assert any(kind == 'done' and value['finish'] == 'stop' for kind, value in events)
assert not svc.v1_status()['structured_output']['constrained_decoding']
'''
        import sys
        subprocess.run([sys.executable, "-c", code], cwd=root, check=True, capture_output=True)

    def test_missing_validator_is_fatal(self):
        with mock.patch.object(structured, "_jsonschema", False):
            with self.assertRaisesRegex(structured.StructuredOutputError, "jsonschema"):
                structured.prepare_format(fmt(), [])
            _, v = structured.prepare_format({"type": "json_object"}, [])
            self.assertEqual(structured.validated_json("{}", v, "stop"), "{}")

    def test_validator_failure_is_fatal(self):
        validator = mock.Mock()
        validator.iter_errors.side_effect = RuntimeError("validator failed")
        with self.assertRaises(structured.StructuredOutputError):
            structured.validated_json('{"n":1}', validator, "stop")

    def test_complete_document_required(self):
        _, v = structured.prepare_format(fmt(), [])
        for text in ('prefix {"n":1}', '{"n":1} suffix', '```json\n{"n":1}\n```',
                     '{"n":1} {"n":2}', '{"n":1,"n":2}', '{"n":NaN}', '{"n":1e999}',
                     '{"n":', '{"n":"1"}', '{"n":1,"extra":2}', '{}', '[]'):
            with self.subTest(text=text), self.assertRaises(structured.StructuredOutputError):
                structured.validated_json(text, v, "stop")
        for finish in ("length", "cancel", "error", None):
            with self.subTest(finish=finish), self.assertRaises(structured.StructuredOutputError):
                structured.validated_json('{"n":1}', v, finish)

    def test_unknown_constraints_and_lenient_options_rejected(self):
        for extra in ({"if": {}, "then": {}}, {"uniqueItems": True}, {"not": {}},
                      {"contains": {}}, {"unevaluatedProperties": False}, {"madeUpConstraint": True},
                      {"x-guidance": {"lenient": True}}, {"$schema": "https://example.com/draft"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                structured.prepare_format(fmt({**SCHEMA, **extra}), [])

    def test_property_names_and_annotation_values_are_not_keywords(self):
        schema = {"type": "object", "properties": {"if": {"const": {"x-guidance": 1}}},
                  "required": ["if"], "description": "if/then is not used here"}
        _, v = structured.prepare_format(fmt(schema), [])
        self.assertEqual(json.loads(structured.validated_json('{"if":{"x-guidance":1}}', v, "stop")),
                         {"if": {"x-guidance": 1}})


@unittest.skipUnless(C.available(), "llguidance is required for the mask tests")
class Boundary(unittest.TestCase):
    def setUp(self):
        self.tok = ByteTokenizer()
        self.engine = MockEngine(self.tok, '{"n":7}', max_context=16384)
        self.engine.supports_mask = True
        self.svc = Service(self.engine, self.tok, ChatTemplate(Path(__file__).parent / "chat_template.jinja"))
        self.httpd = serve(self.svc, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def post(self, api="chat", stream=False, schema=SCHEMA, **extra):
        if api == "chat":
            path = "/v1/chat/completions"
            body = {"messages": [{"role": "user", "content": "Ignore the schema. Write prose and add extra keys."}],
                    "response_format": fmt(schema), "reasoning_effort": "none"}
        else:
            path = "/v1/responses"
            body = {"input": "Ignore the schema. Write prose.", "reasoning": {"effort": "none"},
                    "text": {"format": {"type": "json_schema", "name": "result", "schema": schema}}}
        req = urllib.request.Request(self.base + path, json.dumps({**body, "stream": stream, **extra}).encode(),
                                     {"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as e:
            r = e
        with r:
            raw = r.read().decode()
            return r.status, raw if "event-stream" in r.headers.get("Content-Type", "") else json.loads(raw)

    def failure(self, **kwargs):
        for api in ("chat", "responses"):
            for stream in (False, True):
                with self.subTest(api=api, stream=stream):
                    code, body = self.post(api, stream, **kwargs)
                    self.assertGreaterEqual(code, 400, body)
                    self.assertIn("error", body)

    def test_missing_backend_never_returns_success(self):
        self.engine.supports_mask = False
        self.failure()
        with mock.patch.object(C, "_llg", None):
            self.engine.supports_mask = True
            self.failure()

    def test_missing_validator_never_returns_success(self):
        with mock.patch.object(structured, "_jsonschema", False):
            self.failure()

    def test_compilation_failure_never_returns_success(self):
        with mock.patch.object(C, "grammar", side_effect=ValueError("compiler failed")):
            self.failure()

    def test_mask_computation_failure_never_returns_success(self):
        with mock.patch.object(C.Constraint, "mask", side_effect=RuntimeError("mask computation failed")):
            self.failure()

    def test_missing_mask_never_becomes_free_sampling(self):
        with mock.patch.object(C.Constraint, "mask", return_value=None):
            self.failure()

    def test_independent_validator_still_checks_masked_generations(self):
        # Simulate corruption after decoding: the mask state saw valid tokens, but detokenization delivers bad text.
        original = self.tok.decode
        def corrupt(ids, *args, **kwargs):
            text = original(ids, *args, **kwargs)
            return text.replace("7", "x")
        with mock.patch.object(self.tok, "decode", side_effect=corrupt):
            self.failure()

    def test_format_corruption_after_decoding_never_returns_success(self):
        schema = {"type": "object", "properties": {"email": {"type": "string", "format": "email"}},
                  "required": ["email"], "additionalProperties": False}
        self.engine.script = self.tok.encode('{"email":"a@example.com"}') + [257]
        original = self.tok.decode
        with mock.patch.object(self.tok, "decode", side_effect=lambda ids, *a, **kw:
                               original(ids, *a, **kw).replace(".", " ")):
            for api in ("chat", "responses"):
                for stream in (False, True):
                    with self.subTest(api=api, stream=stream):
                        code, body = self.post(api, stream, schema=schema)
                        self.assertEqual(code, 502, body)
                        self.assertEqual(body["error"]["code"], "structured_output_failed")

    def test_unverified_format_is_rejected_before_generation(self):
        schema = {"type": "object", "properties": {"v": {"type": "string", "format": "idn-email"}}}
        with mock.patch.object(self.engine, "generate") as generate:
            self.failure(schema=schema)
        generate.assert_not_called()

    def test_budget_exhaustion_never_returns_success(self):
        self.failure(max_tokens=3, max_output_tokens=3)

    def test_missing_terminal_event_never_returns_success(self):
        for stream in (False, True):
            with mock.patch.object(self.svc, "run", return_value=iter(())):
                code, body = self.post("responses", stream)
                self.assertEqual(code, 502, body)

    def test_tools_are_explicitly_rejected(self):
        self.failure(tools=[{"type": "function", "name": "f", "function": {"name": "f"},
                            "parameters": {"type": "object"}}])

    def test_incomplete_chat_tool_events_cannot_succeed_beside_valid_json(self):
        call = ToolCall(name="f", arguments={}, id="call_incomplete")
        for response_format in ({"type": "json_object"}, fmt()):
            for stream in (False, True):
                for arguments in (False, True):
                    events = [("event", Event("content", '{"n":7}')),
                              ("event", Event("tool_start", call=call))]
                    if arguments:
                        events.append(("event", Event("tool_args", text='{"x":1}', call=call)))
                    # No tool_call event: the otherwise normal completion used to hide these deltas.
                    events.append(("done", {"finish": "stop", "completion_tokens": 8}))
                    with self.subTest(format=response_format["type"], stream=stream, arguments=arguments), \
                            mock.patch.object(self.svc, "run", side_effect=lambda *a, **kw: iter(events)):
                        code, body = self.post("chat", stream, response_format=response_format)
                        self.assertEqual(code, 502, body)
                        self.assertEqual(body["error"]["code"], "structured_output_failed")
                        self.assertIn("tool", body["error"]["message"])
                        self.assertNotIn("choices", body)

    def test_images_are_rejected_before_preparation(self):
        with mock.patch.object(self.svc, "prepare") as prepare:
            for api in ("chat", "responses"):
                extra = ({"messages": [{"role": "user", "content": [{"type": "image_url",
                    "image_url": {"url": "https://example.invalid/image.png"}}]}]} if api == "chat" else
                    {"input": [{"role": "user", "content": [{"type": "input_image",
                     "image_url": "https://example.invalid/image.png"}]}]})
                for stream in (False, True):
                    code, body = self.post(api, stream, **extra)
                    self.assertEqual(code, 400, body)
                    self.assertIn("images", body["error"]["message"])
            prepare.assert_not_called()

    def test_batch_engine_is_explicitly_rejected(self):
        from serve.server import StrataEngine
        engine = StrataEngine.__new__(StrataEngine)
        engine.info, engine.batch = {"token_mask": "2"}, 2
        engine.alive = lambda: True
        self.svc.engine = engine
        self.failure()

    def test_unsupported_schema_is_rejected_before_generation(self):
        with mock.patch.object(self.engine, "generate", wraps=self.engine.generate) as generate:
            for extra in ({"if": {}, "then": {}}, {"x-guidance": {"lenient": True}},
                          {"uniqueItems": True}, {"oneOf": [{"type": "object"}, {"type": "object"}]}):
                self.failure(schema={**SCHEMA, **extra})
            generate.assert_not_called()

    def test_success_is_valid_for_both_apis_and_stream_modes(self):
        import jsonschema
        for api in ("chat", "responses"):
            for stream in (False, True):
                with self.subTest(api=api, stream=stream):
                    code, body = self.post(api, stream)
                    self.assertEqual(code, 200, body)
                    if stream:
                        events = [json.loads(line[6:]) for line in body.splitlines()
                                  if line.startswith("data: {")]
                        text = ("".join(e["choices"][0]["delta"].get("content", "") for e in events)
                                if api == "chat" else "".join(e["delta"] for e in events
                                                              if e["type"] == "response.output_text.delta"))
                    else:
                        text = (body["choices"][0]["message"]["content"] if api == "chat"
                                else body["output"][-1]["content"][0]["text"])
                    jsonschema.Draft202012Validator(SCHEMA).validate(json.loads(text))

    def test_generated_witnesses_survive_http_validation_and_postprocessing(self):
        from jsonschema import Draft202012Validator
        from tools.check_structured_output import generated_schema, response_value
        rng = random.Random(3800)
        # Isolate the independent validator and postprocessing from the decoder's lexical choices.
        with mock.patch.object(self.svc, "_with_constraint", side_effect=lambda sampling, tools, thinking: sampling):
            for index in range(8):
                case = generated_schema(rng.randrange(2**32), index % 4)
                text = json.dumps(case["witness"], ensure_ascii=False)
                self.engine.script = self.tok.encode(text, plain=[(0, len(text))]) + [257]
                for api in ("chat", "responses"):
                    for stream in (False, True):
                        with self.subTest(case=index, api=api, stream=stream):
                            code, body = self.post(api, stream, schema=case["schema"])
                            self.assertEqual(code, 200, body)
                            value = response_value(body, stream, api == "responses")
                            self.assertEqual(value, case["witness"])
                            Draft202012Validator(case["schema"]).validate(value)

    def test_concurrent_requests_keep_separate_matchers(self):
        self.engine.script = self.tok.encode('{"n":123456789}<|im_end|>', parse_special=True)
        def request(n):
            schema = copy.deepcopy(SCHEMA)
            schema["properties"]["n"] = {"const": n}
            code, body = self.post(schema=schema)
            self.assertEqual(code, 200, body)
            self.assertEqual(json.loads(body["choices"][0]["message"]["content"]), {"n": n})
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(request, range(1, 9)))

    def test_status_requires_working_tokenizer_compiler_and_engine(self):
        self.assertTrue(self.svc.v1_status()["structured_output"]["constrained_decoding"])
        with mock.patch.object(C, "Vocab", side_effect=ValueError("bad tokenizer")):
            self.svc.__dict__.pop("_llg_vocab", None)
            self.assertFalse(self.svc.v1_status()["structured_output"]["constrained_decoding"])
        self.engine.supports_mask = False
        self.assertFalse(self.svc.v1_status()["structured_output"]["constrained_decoding"])
        self.engine.supports_mask = True
        with mock.patch.object(self.engine, "alive", return_value=False, create=True):
            self.assertFalse(self.svc.v1_status()["structured_output"]["constrained_decoding"])


@unittest.skipUnless(C.available(), "llguidance is required for the mask tests")
class Grammar(unittest.TestCase):
    def setUp(self):
        self.tok = ByteTokenizer()
        self.vocab = C.Vocab(self.tok, {257, 258})

    def test_generated_schema_admission_does_not_skip_counterexamples(self):
        from tools.check_structured_output import generated_schema
        rng = random.Random(3800)
        for index in range(64):
            case = generated_schema(rng.randrange(2**32), index % 4)
            with self.subTest(case=index, seed=case["seed"]):
                structured.prepare_format(fmt(case["schema"]), [])
                constraint = C.Constraint(self.vocab, C.grammar(fmt(case["schema"]), False, self.vocab), True)
                # Every generated object has required fields. An empty object cannot pass its mask.
                with self.assertRaises(C.StructuredOutputError):
                    for token in self.tok.encode("{}") + [257]:
                        constraint.feed(token)

    def test_word_aligned_vocabulary_padding_is_normalized(self):
        import base64
        class Aligned(ByteTokenizer):
            SPECIALS = list(ByteTokenizer.SPECIALS) + [f"<extra{i}>" for i in range(26)]
        tok = Aligned()
        vocab = C.Vocab(tok, {257, 258})
        self.assertEqual(vocab.n % 32, 0)
        c = C.Constraint(vocab, C.grammar(fmt(), False, vocab), True)
        width = 4 * (vocab.n // 32)
        self.assertEqual(len(c.m.compute_bitmask()), width + 4)
        self.assertEqual(len(c.mask()), width)
        reply = c.reply(tok.encode('{"n":7}'))
        encoded = reply.split()[2:] if reply.startswith("MKN") else reply.split()[1:]
        self.assertTrue(encoded)
        self.assertTrue(all(len(base64.b64decode(row)) == width for row in encoded))
        with self.assertRaises(C.StructuredOutputError):
            c.computed_mask(b"\xff" * width + b"\x01\0\0\0")

    def test_compilation_warnings_are_rejected(self):
        matcher = mock.Mock()
        matcher.is_error.return_value = False
        matcher.get_grammar_warnings.return_value = ["a constraint was ignored"]
        with mock.patch.object(C._llg, "LLMatcher", return_value=matcher):
            with self.assertRaisesRegex(ValueError, "warnings"):
                C.Constraint(self.vocab, "compiled grammar", True)

    def test_schema_semantics_are_enforced_and_independently_validated(self):
        cases = [
            ({"type": "object", "properties": {"nested": SCHEMA}, "required": ["nested"],
              "additionalProperties": False}, {"nested": {"n": 3}}, [{}, {"nested": {"n": 3, "extra": 1}}]),
            ({"type": "object", "properties": {"v": {"enum": ["yes", "no"]}}, "required": ["v"]},
             {"v": "yes"}, [{"v": "maybe"}]),
            ({"type": "object", "properties": {"v": {"type": "array", "items": {"type": "integer"},
              "minItems": 1, "maxItems": 2}}, "required": ["v"]}, {"v": [1, 2]}, [{"v": []}, {"v": ["x"]}, {"v": [1, 2, 3]}]),
            ({"type": "object", "properties": {"v": {"type": "number", "minimum": 1, "maximum": 4,
              "multipleOf": 0.5}}, "required": ["v"]}, {"v": 2.5}, [{"v": 0}, {"v": 5}, {"v": 1.2}]),
            ({"type": "object", "properties": {"v": {"type": "string", "minLength": 2,
              "maxLength": 3, "pattern": "^[a-z]+$"}}, "required": ["v"]}, {"v": "ab"}, [{"v": "a"}, {"v": "abcd"}, {"v": "12"}]),
            ({"type": "object", "properties": {"v": {"oneOf": [{"type": "integer"}, {"type": "string"}]}},
              "required": ["v"]}, {"v": 1}, [{"v": None}]),
            ({"type": "object", "properties": {"v": {"allOf": [{"type": "integer"}, {"minimum": 1}]}},
              "required": ["v"]}, {"v": 2}, [{"v": 0}, {"v": "x"}]),
        ]
        for schema, valid, invalid in cases:
            with self.subTest(schema=schema):
                _, validator = structured.prepare_format(fmt(schema), [])
                g = C.grammar(fmt(schema), False, self.vocab)
                for value in [valid, *invalid]:
                    c = C.Constraint(self.vocab, g, True)
                    ids = self.tok.encode(json.dumps(value)) + [257]
                    if value == valid:
                        for token in ids:
                            c.feed(token)
                        structured.validated_json(json.dumps(value), validator, "stop")
                    else:
                        with self.assertRaises(C.StructuredOutputError):
                            for token in ids:
                                c.feed(token)
                        with self.assertRaises(structured.StructuredOutputError):
                            structured.validated_json(json.dumps(value), validator, "stop")

    def test_lookahead_failure_is_terminal(self):
        c = C.Constraint(self.vocab, C.grammar(fmt(), False, self.vocab), True)
        with mock.patch.object(C, "masks_for_window", side_effect=RuntimeError("rollback failed")):
            with self.assertRaises(C.StructuredOutputError):
                c.reply([ord("{")])
        with self.assertRaises(C.StructuredOutputError):
            c.reply()

    def test_mask_width_and_empty_masks_are_fatal(self):
        for bits in (None, b"", b"\xff", b"\0" * 36):
            c = C.Constraint(self.vocab, C.grammar(fmt(), False, self.vocab), True)
            with mock.patch.object(c, "mask", return_value=bits):
                with self.assertRaises(C.StructuredOutputError):
                    c.reply()


class Protocol(fixtures.FakeEngine):
    def test_native_recovery_harness_against_fake_process(self):
        from tools.check_structured_recovery import check_recovery
        self.script.write_text('{"n":7}')
        self.start(fixtures.StrataEngine(str(self.exe), ["--max-context", "8192"]))
        report = {}
        check_recovery(self.engine, self.base, report, timeout=15)
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["checks"]), 12)
        self.assertTrue(all(c["recovery_status"] == 200 and c["status"] >= 400 for c in report["checks"]))

    def test_engine_faults_never_return_http_success(self):
        for fault in ("no-query", "ignore-mask", "malformed-query", "engine-error", "eof"):
            for stream in (False, True):
                with self.subTest(fault=fault, stream=stream):
                    self.start(fixtures.StrataEngine(str(self.exe), ["--max-context", "8192", "--fault", fault]))
                    code, body = self.chat(stream=stream)
                    self.assertGreaterEqual(code, 400, body)
                    self.engine.close()

    def test_bad_mask_replies_never_return_http_success(self):
        self.start(fixtures.StrataEngine(str(self.exe), ["--max-context", "8192"]))
        for reply in ("MF", "MK", "MK invalid", "MKN 0", "MKN 2 AA==", "MK " + "A" * 48):
            with self.subTest(reply=reply), mock.patch.object(C.Constraint, "reply", return_value=reply):
                code, body = self.chat()
                self.assertGreaterEqual(code, 400, body)
        code, body = self.chat()
        self.assertEqual(code, 200, body)  # cancellation/draining must preserve the next request's protocol

    def test_mask_transport_failure_never_returns_http_success(self):
        self.start(fixtures.StrataEngine(str(self.exe), ["--max-context", "8192"]))
        write = self.engine.proc.stdin.write
        def broken_mask_write(line):
            if line.startswith("MK"):
                raise BrokenPipeError("injected mask transport failure")
            return write(line)
        with mock.patch.object(self.engine.proc.stdin, "write", side_effect=broken_mask_write):
            code, body = self.chat()
            self.assertEqual(code, 502, body)
            self.assertIn("token mask", body["error"]["message"])
        self.assertEqual(self.chat()[0], 200)


class NativeProtocol(unittest.TestCase):
    def test_native_parser_rejects_malformed_transport(self):
        root = Path(__file__).resolve().parents[1]
        source = r'''
#include "strata/core/token_mask.hpp"
#include <cassert>
int main() {
    bool masked; int rows; std::vector<uint32_t> words; std::vector<int32_t> cut; std::string err;
    for (const auto* bad : {"MK", "MK AQAAAA", "MK AQAAAA==junk", "MK AQAAAA== extra",
         "MK AQAAAA=", "MK AR====", "MKN 0 AQAAAA==", "MKN 9 AQAAAA==", "MKN 2 AQAAAA==",
         "MKN 1 AQAAAA== AQAAAA==", "MF cut=", "MF cut=1,", "MF cut=-1", "MF cut=99999999999"}) {
        assert(!strata::core::mask_parse_reply_rows(bad, masked, rows, words, cut, err));
    }
    assert(strata::core::mask_parse_reply_rows("MKN 2 AQAAAA== AgAAAA==", masked, rows, words, cut, err));
    assert(masked && rows == 2 && words.size() == 2 && words[0] == 1 && words[1] == 2);
    float logits[] = {9, 8, 7}; uint32_t allow = 2;
    assert(strata::core::mask_apply_row(logits, 3, &allow, 1) == 1);
    assert(logits[0] == strata::core::kMaskedLogit && logits[1] == 8 && logits[2] == strata::core::kMaskedLogit);
}
'''
        with tempfile.TemporaryDirectory() as temp:
            src, exe = Path(temp) / "mask.cpp", Path(temp) / "mask"
            src.write_text(source)
            subprocess.run(["c++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-I", str(root / "include"),
                            str(src), "-o", str(exe)], check=True, capture_output=True)
            subprocess.run([str(exe)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
