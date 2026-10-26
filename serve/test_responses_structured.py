"""The Responses parser/assembler must never bypass complete-document validation.

    python -m unittest serve.test_responses_structured -v
"""
import json
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from serve import constrain, responses
from serve.frontend import ChatTemplate, Event, OutputParser, ToolCall
from serve.server import ByteTokenizer, MockEngine, Service, serve
from serve.structured import StructuredOutputError


CALL = "<tool_call><function=f></function></tool_call>"
VALUE = {"message": CALL}
SCHEMA = {"type": "object", "properties": {"message": {"const": CALL}},
          "required": ["message"], "additionalProperties": False}
SCHEMA_FORMAT = {"type": "json_schema", "name": "literal", "schema": SCHEMA}
DONE = {"finish": "stop", "completion_tokens": 1}


class Type4Tokenizer(ByteTokenizer):
    """Like the real Qwen tokenizer, recognize these tokens even with parse_special=False."""
    ALWAYS = ("<tool_call>", "</tool_call>", "<think>", "</think>")
    SPECIALS = list(ByteTokenizer.SPECIALS) + list(ALWAYS)


class AssemblerBoundary(unittest.TestCase):
    def assembler(self):
        return responses.Assembler({}, "test", 0, {}, False, json_mode=True)

    def test_parsed_tool_events_fail_before_releasing_any_text(self):
        call = ToolCall(name="f", arguments={}, id="call_1")
        for kind in ("tool_start", "tool_args", "tool_call"):
            with self.subTest(kind=kind):
                asm = self.assembler()
                self.assertEqual(asm.feed(Event("content", "unvalidated text")), [])
                with self.assertRaises(StructuredOutputError):
                    asm.feed(Event(kind, text="{}", call=call))
                self.assertEqual(asm.response["output"], [])

    def test_called_state_is_fatal_at_finish(self):
        asm = self.assembler()
        asm.feed(Event("content", "unvalidated text"))
        asm.called = True
        with self.assertRaises(StructuredOutputError):
            asm.finish(DONE, mock.Mock(return_value="{}"))
        self.assertEqual(asm.response["output"], [])

    def test_existing_tool_item_is_fatal_even_if_called_flag_is_lost(self):
        for kind in ("function_call", "custom_tool_call"):
            with self.subTest(kind=kind):
                asm = self.assembler()
                asm.response["output"].append({"type": kind})
                with self.assertRaises(StructuredOutputError):
                    asm.finish(DONE, mock.Mock(return_value="{}"))

    def test_validator_is_mandatory(self):
        asm = self.assembler()
        asm.feed(Event("content", "{}"))
        with self.assertRaises(StructuredOutputError):
            asm.finish(DONE)
        self.assertEqual(asm.response["output"], [])

    def test_validation_failure_releases_no_answer(self):
        asm = self.assembler()
        asm.feed(Event("content", '{"message":"wrong"}'))
        validate = mock.Mock(side_effect=StructuredOutputError("schema violation"))
        with self.assertRaises(StructuredOutputError):
            asm.finish(DONE, validate)
        validate.assert_called_once_with('{"message":"wrong"}', "stop")
        self.assertEqual(asm.response["output"], [])


class HttpBoundary(unittest.TestCase):
    tokenizer_cls = ByteTokenizer

    def setUp(self):
        self.tok = self.tokenizer_cls()
        self.engine = MockEngine(self.tok, "{}", max_context=16384)
        self.svc = Service(self.engine, self.tok, ChatTemplate(Path(__file__).parent / "chat_template.jinja"))
        self.httpd = serve(self.svc, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def post(self, script, format, stream):
        # Valid JSON represents tag spellings as ordinary text, not model control tokens.
        plain = [(0, len(script))] if script.startswith("{") else ()
        self.engine.script = self.tok.encode(script, parse_special=True, plain=plain) + self.tok.encode("<|im_end|>", parse_special=True)
        req = urllib.request.Request(self.base + "/v1/responses", json.dumps({
            "input": "Return the requested JSON object.", "reasoning": {"effort": "none"},
            "text": {"format": format}, "stream": stream}).encode(), {"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read().decode()
            return response.status, raw if "event-stream" in response.headers.get("Content-Type", "") else json.loads(raw)

    def assert_json_response(self, code, body, expected, stream):
        self.assertEqual(code, 200, body)
        if stream:
            events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]
            self.assertEqual(events[-1]["type"], "response.completed")
            text = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
            result = events[-1]["response"]
        else:
            result = body
            text = "".join(part["text"] for item in result["output"] if item["type"] == "message"
                           for part in item["content"] if part["type"] == "output_text")
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["type"] for item in result["output"]], ["message"])
        self.assertEqual(json.loads(text), expected)
        final_text = "".join(part["text"] for part in result["output"][0]["content"])
        self.assertEqual(json.loads(final_text), expected)
        return json.loads(text)

    def test_undeclared_tool_call_is_not_successful_json_object(self):
        self.assertFalse(getattr(self.engine, "supports_mask", False))
        for script in (CALL, "not JSON\n" + CALL, '{"message":"before"}' + CALL):
            for stream in (False, True):
                with self.subTest(script=script, stream=stream):
                    code, body = self.post(script, {"type": "json_object"}, stream)
                    self.assertEqual(code, 502, body)
                    self.assertEqual(body["error"]["code"], "structured_output_failed")
                    self.assertNotIn("output", body)

    def test_json_object_preserves_literal_tool_syntax_without_masking(self):
        self.assertFalse(getattr(self.engine, "supports_mask", False))
        for literal in (CALL, "before " + CALL + " after", CALL + CALL,
                        '<tool_call>\n<function=f>\n<parameter=x>value</parameter>\n</function>\n</tool_call>'):
            expected = {"message": literal}
            for stream in (False, True):
                with self.subTest(literal=literal, stream=stream):
                    self.assert_json_response(*self.post(json.dumps(expected), {"type": "json_object"}, stream),
                                              expected, stream)

    @unittest.skipUnless(constrain.available(), "llguidance is required for schema masking")
    def test_masked_json_schema_preserves_literal_tool_syntax(self):
        from jsonschema import Draft202012Validator
        self.engine.supports_mask = True
        for stream in (False, True):
            with self.subTest(stream=stream):
                value = self.assert_json_response(*self.post(json.dumps(VALUE), SCHEMA_FORMAT, stream), VALUE, stream)
                Draft202012Validator(SCHEMA).validate(value)

    def test_parsed_tool_fault_cannot_bypass_validation(self):
        # Simulate a parser regression: even valid masked JSON containing a tag
        # must fail closed if postprocessing incorrectly turns it into a call.
        self.svc.parser_cls = lambda **kw: OutputParser(**{**kw, "declared_only": False})
        formats = [{"type": "json_object"}]
        if constrain.available():
            formats.append(SCHEMA_FORMAT)
        for format in formats:
            self.engine.supports_mask = format["type"] == "json_schema"
            for stream in (False, True):
                with self.subTest(format=format["type"], stream=stream):
                    code, body = self.post(json.dumps(VALUE), format, stream)
                    self.assertEqual(code, 502, body)
                    self.assertEqual(body["error"]["code"], "structured_output_failed")

    def test_structured_route_restricts_parser_to_declared_tools(self):
        with mock.patch.object(self.svc, "run", wraps=self.svc.run) as run:
            self.assert_json_response(*self.post("{}", {"type": "json_object"}, False), {}, False)
            self.assertIs(run.call_args.kwargs.get("declared_only"), True)

    def test_plain_responses_keep_existing_undeclared_tool_behavior(self):
        for stream in (False, True):
            code, body = self.post(CALL, {"type": "text"}, stream)
            self.assertEqual(code, 200, body)
            if stream:
                events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]
                body = events[-1]["response"]
            self.assertEqual([item["type"] for item in body["output"]], ["function_call"])


class Type4HttpBoundary(HttpBoundary):
    tokenizer_cls = Type4Tokenizer

    @unittest.skipUnless(constrain.available(), "llguidance is required for schema masking")
    def test_grammar_tokenizer_encodes_type4_spellings_as_literal_bytes(self):
        vocab = constrain.Vocab(self.tok, {257, 258})
        text = json.dumps(VALUE)
        self.assertTrue(set(self.tok.encode(text)) & set(vocab.specials))
        encoded = vocab.lltok.tokenize_str(text)
        self.assertFalse(set(encoded) & set(vocab.specials))
        self.assertEqual(self.tok.decode(encoded), text)
        constraint = constrain.Constraint(vocab, constrain.grammar({"type": "json_schema", "json_schema": {
            "schema": SCHEMA}}, False, vocab), armed=True)
        for token in encoded:
            constraint.feed(token)
        self.assertTrue(constraint.m.is_accepting() or constraint.m.is_stopped())


if __name__ == "__main__":
    unittest.main()
