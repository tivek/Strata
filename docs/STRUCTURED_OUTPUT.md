# Structured output

A successful structured answer must contain a complete JSON object. For `json_schema`, it must also pass
`jsonschema` validation against the original request schema. A request can fail; schema compliance is required
for every successful answer. The tests described below do not establish that every request will complete.

## Requests and errors

Use `response_format` on `/v1/chat/completions` or `text.format` on `/v1/responses`:

```json
{"type":"json_schema","json_schema":{"name":"result","strict":true,"schema":{"type":"object","properties":{"n":{"type":"integer"}},"required":["n"],"additionalProperties":false}}}
```

For Responses, move `name`, `strict` and `schema` alongside `type` inside `text.format`.
`strict: false` does not disable independent validation.

Chat Completions rejects any top-level `text.format` field with HTTP 400 before prompt preparation or model
loading, even when `response_format` is also present. Use the selector for the requested API; sending both
does not establish precedence. Text options without a `format` key and nested JSON data retain their behavior.

1. Schema requests require `llguidance==1.9.1`, `jsonschema==4.26.0`, a working tokenizer and a loaded engine
   advertising token masks. Missing dependencies or unsupported engines return an explicit error. `json_object`
   can run on an older engine, with mandatory document validation.
2. Invalid or unsupported schemas and incompatible combinations return 400. Compilation warnings are errors.
   Generation, mask computation, grammar state, transport, malformed output and incomplete output failures return
   non-2xx errors. `structured_output_failed` identifies the structured generation boundary.
3. `stream: true` buffers generation and validation **before HTTP success headers**. A failure returns a JSON
   error with a non-2xx status. A successful stream contains validated content in the usual SSE format. No
   keep-alives are sent before validation. The client's read/first-response timeout and any proxy idle timeout
   must cover queue wait, prompt processing, reasoning, full generation and validation. A timeout intended for
   the interval between ordinary streamed tokens is insufficient. For example, use `timeout=(10, 240)` with
   Python `requests` for a 10-second connection limit and a 240-second read limit; increase the latter for
   workloads that take longer. The hardware checker exposes this as `--timeout 240`.
4. Tools, MCP and image inputs with structured output are explicitly unsupported on both API routes. Continuous
   batch slots, replica wrappers without mask support, and the DeepSeek family are rejected. Ordinary text and
   tool requests use their existing paths.
5. Prose, fences, extra documents, duplicate keys, NaN/Infinity, non-finite decoded numbers and incomplete token
   budgets are errors. The server does not extract a valid-looking object from an invalid document.

Unexpected tool-call deltas during structured Chat Completions also fail before HTTP success, including a
`tool_start`/`tool_args` sequence that ends without a completed tool call. Both API routes reject these parser
anomalies even when accompanying text is valid JSON. Literal tool syntax inside a JSON string remains text.

Inference request bodies also reject duplicate JSON object keys with HTTP 400, before prompt preparation or
model loading. This applies to Chat Completions, Responses and Messages, including ordinary text requests:
repeated envelope fields must not conceal a structured-output selector. Nested schema keywords, property names,
definitions and JSON data are checked before parsing can discard a value. Escaped spellings of the same key
count as duplicates. Identical names in separate objects and JSON-looking text inside strings remain valid.

## JSON Schema support

The supported dialect is Draft 2020-12, with an object-only root as in the existing API. User-provided
`x-guidance` options are rejected: they can change separators or silently weaken constraints. Unknown keywords
are rejected, including in unused definitions. Property names, annotations and enum/const data are not treated
as schema keywords.

The exact compiler is pinned. See the [llguidance 1.9.1 schema reference](https://github.com/guidance-ai/llguidance/blob/v1.9.1/docs/json_schema.md).
The compiler must accept the particular schema without warnings; recognizing a keyword is not a promise that
all of its combinations compile.

| Feature | Admission and enforcement |
| --- | --- |
| `type`, `required`, `properties`, `additionalProperties`, nested objects, `enum`, `const` | Supported; original schema also checked independently. |
| `items`, `prefixItems`, `minItems`, `maxItems` | Supported. `uniqueItems`, `contains`, `minContains`, `maxContains` are rejected. |
| `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf` | Supported; validator retains the original numeric constraints. |
| `minLength`, `maxLength`, `pattern` | Supported when the compiler accepts the regex; lookarounds are rejected. |
| `format` | Explicit independent checkers for `email`, `hostname`, `date`, `time`, `date-time`, `ipv4`, `ipv6` and `uuid`; compiler support is also required. All other formats are rejected, including formats recognized only by optional installed packages. |
| `anyOf` | Supported subject to compilation. |
| `oneOf` | Only compiler-proven disjoint alternatives; no coercion to `anyOf`. Ambiguous alternatives are rejected. |
| `allOf` | Compiler-supported intersections only; independent validation checks original branch semantics. |
| `patternProperties`, `minProperties`, `maxProperties` | Compiler-dependent: patterns must be disjoint; object-size constraints have compiler restrictions. |
| Local `$ref`, `$defs`, `definitions` | Local JSON pointers only; remote references, `$id`, anchors and dynamic references are rejected. |
| `if`/`then`/`else`, `not`, dependent and unevaluated constraints | Rejected. |

llguidance fixes declared-property order and can permit duplicate keys in additional/pattern properties. The
independent parser rejects duplicate keys. The compiler documents ordering/intersection differences; an output
that differs from the original schema's meaning is an error, never a successful approximation.

Format checks have defined boundaries. Email uses ASCII SMTP mailbox syntax, with dot-atom or quoted local
parts and DNS domains or IPv4/IPv6 address literals; empty/invalid domains, comments, control characters and
oversized mailboxes are rejected. Hostnames use ASCII DNS labels. Internationalized formats and generalized
email address literals are unsupported. There is no DNS lookup or deliverability check. UUIDs require the
canonical hyphenated representation, with either case accepted. Dates are calendar-checked; times and
date-times require RFC 3339 syntax and a timezone. Leap seconds and year zero are rejected. These checks apply
independently after generation; the grammar may impose further restrictions.

## Engine protocol and isolation

A schema request owns its matcher. The shared tokenizer vocabulary is initialized under a lock; mutable matcher
state is never shared across requests. Native batch decoding does not support this mask protocol and is rejected.
Concurrent HTTP requests queue for the serial engine, preserving separate schemas and matcher states.

`GEN ... mask=1` requests `MQ` before each window. `MK` contains one mask; `MKN` contains one mask per speculative
row. Draft lookahead uses a matcher copy, then rolls back the original. A state or rollback failure aborts the
request. The server checks mask dimensions and nonempty allowed tokens, validates draft-query framing, and checks
every emitted token against its authorized row before advancing the grammar.
For word-aligned vocabularies, the pinned backend's zero spare word is removed before transport; unexpected
padding remains an error. The real Qwen vocabulary exercises this case.

Reasoning uses `MF cut=...` until `</think>`; the cut prevents answer tokens from entering a free speculative
window. Budget continuations preserve matcher state and observe the inserted reasoning close. EOS cannot finish
an incomplete grammar. Pipeline decoding is disabled for masked requests. Layer-split verifiers propagate masks
to the final sampling stage. CUDA copy/synchronization failures and sampler picks outside the mask are fatal.

If a mask query fails after it has been read, the server sends STOP and a cancellation reply before draining.
STOP alone cannot release the native query wait. This prevents the failed turn from blocking or contaminating
the following request. The cleanup reply is sent after cancellation; it is not a fallback generation.

`/v1/status.structured_output.constrained_decoding` requires a loaded mask-capable engine, a usable tokenizer,
a successful grammar/mask probe and the independent validator. It is not inferred from installed packages alone.
It does not claim that every schema is supported or that a future device operation cannot fail.

## Reproduce the CPU checks

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-monitor.txt pytest pip
.venv/bin/python -m unittest serve.test_constrain serve.test_structured serve.test_structured_hardening serve.test_responses tools.test_setup_pins
.venv/bin/python -m unittest serve.test_responses_structured -v
.venv/bin/python -m unittest discover -s serve -t .
.venv/bin/python -m pytest tests/terminal -q
```

The fault suite covers unavailable dependencies/backends, strict compilation, invalid masks/queries, ignored masks,
transport failure, rollback failure, invalid and incomplete documents, missing completion events, independent
validator failure, concurrent request isolation, both API routes and buffered streaming. A native C++ host test
compiles and exercises the actual mask parser and mask application helper without CUDA inference.

The Responses regressions also cover undeclared tool calls, literal tool-call syntax inside JSON strings,
and tokenizers that recognize type-4 special tokens inside ordinary text. Both streaming modes must return
validated JSON or an HTTP error. The `Structured output invariant` workflow is configured to run these checks
without a model or GPU and retain its log for 14 days when enabled. CI is intentionally disabled by the
repository owner; the commands above are run locally for verification.

The [integration and validation report](STRUCTURED_OUTPUT_REPORT.md) records the tested revisions, CPU faults,
single- and dual-RTX-4090 results, baseline regression failures and exact reproduction commands.

## Native failure recovery and randomized checks

During an available GPU window, use an existing run config and a new evidence directory:

```bash
.venv/bin/python tools/check_structured_recovery.py --config strata-q2_0.json --output /tmp/structured-recovery --random-cases 128 --generated-cases 64 --timeout 240 --run
```

Without `--run`, the command only describes the run. With it, the checker starts a private native engine and
an ephemeral loopback HTTP server. It corrupts an actual mask reply, injects a matcher failure after a query,
and simulates a mask-write failure. Each fault must return an HTTP error; the immediate next schema request
must return validated HTTP 200 on the same live process, without a restart. Both APIs and streaming modes
are exercised. The checker closes its own engine and server when finished.

The same process then runs the existing acceptance checks and a seeded campaign across repository schemas,
including the Story fixture, nested/union/reference schemas, literal tool syntax and the supported formats.
Every HTTP 200 document is independently checked against its original schema. Format fixtures use finite enums
of known-valid RFC examples as an additional independent check. Errors remain visible in the report; a campaign
cannot pass with a schema-invalid success or without any successful generation for a fixture. `--seed` controls
the reproducible request sequence. These are repository fixtures, not a claim about private production schemas.
`--random-cases` retains fixed schema structures while varying fixture selection, order, temperature and seeds.
`--generated-cases` adds recursive schema generation. Every generated schema is tested on both APIs and in both
streaming modes. It varies object nesting and arity, homogeneous and tuple arrays, escaped local-reference
chains, unusual Unicode/control/literal-tool strings, integer and binary-fraction bounds, disjoint unions and
constraint intersections. It uses the existing `random` and unittest infrastructure, without another framework.

Each generated schema has a separately validated satisfying witness. The witness is not supplied as the model's
answer; the model generates new values under the schema. Every successful document is checked with the original
Draft 2020-12 schema. The finite corpus must produce at least one success for every schema; all error responses
are counted and retained. A violation retains its exact schema, seed, request and response for replay.
The structural generator is format-free and complements the explicit format fixtures. It has no automatic
shrinking and does not explore every supported schema combination or private production schema.
Raw results, the request seed/schema/value records and native engine logs are written to the evidence directory.
