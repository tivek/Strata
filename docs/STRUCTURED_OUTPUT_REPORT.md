# PR #3800 integration and structured-output validation

## Review follow-up: reject mixed API format selectors

The review of `eb69d0012bba43aa35e430c4a92746936ecb15d1` found that Chat Completions rejected a Responses-style
`text.format` only when `response_format` was absent. A local HTTP reproduction confirmed that adding
`response_format: {"type":"text"}` changed the reviewer's request from HTTP 400 to HTTP 200 with ordinary text,
in both streaming modes. This was an admission inconsistency for a misplaced API field.

Chat Completions now rejects the presence of `text.format` independently of `response_format` and of the
format value. The same four reproduction requests now all return HTTP 400. The existing HTTP fixture covers
50 invalid combinations: absent/null/text/object/schema Chat selectors, schema/object/text/null/empty misplaced
formats, and both streaming modes. It verifies that model loading and prompt preparation never run.
Correct selectors on both APIs, ordinary text options without a format key, nested metadata and literal
`text.format` content remain successful.

| Check | Observed result |
| --- | --- |
| Request-hardening module | **18 passed**, including three new regression methods. |
| Selected local CPU suite, CPython 3.12.3 | **128 passed**, covering admission, parser/assembler boundaries, independent validation, masking faults and ordinary tool compatibility. |
| Exact reviewer HTTP reproduction | Both previously accepted mixed-selector requests now return HTTP 400; both absent-selector controls remain HTTP 400. |

[Before/after HTTP responses and raw local test logs](structured-output-evidence/review-eb69d001/README.md)
are retained with checksums and tested source hashes. No native code changed. GPU inference and the broader
server/terminal suites were not rerun for this rejection before loading; their earlier results below are
historical. CI remains intentionally disabled at the owner's request. All checks in this follow-up ran locally.

## Review follow-up: Chat tool-event guard and generated schema structures

The review of `64fa76b3c60a36f2bcc75bd5f2b7a204e2b10c7c` identified a Chat Completions anomaly: valid JSON
beside an unfinished tool-call stream could return HTTP 200 after the tool deltas were discarded. This was
schema-valid output, but the anomalous tool events should have failed the request. New regressions reproduced
14 failing assertions before the guard. Both structured formats, streaming modes, a lone `tool_start`, and
`tool_start` plus valid arguments without a final `tool_call` now return HTTP 502 before success headers.

`structured_chunks()` rejects any delta containing `tool_calls` before buffering or handling progress. The check
also rejects empty/null tool-call fields and deltas marked as prompt progress. It checks every choice and closes
the upstream iterator on failure. Normal tool requests retain their existing route; literal tool syntax in JSON
strings remains text. The existing Responses assembler guard is retained.

The fixed-fixture campaign is retained, and a new seeded recursive generator creates schema structures rather
than only choosing fixtures. It varies object arity/nesting, homogeneous/tuple arrays, disjoint unions, local
reference chains with escaped pointer names, unusual literal strings, numeric bounds and constraint intersections.
Numeric cases include integers near the 32-bit and 53-bit boundaries and exact binary-fraction multiples.
The generator uses the project's existing `random` and unittest infrastructure. There is no rejection sampling
or filtering of schemas that fail admission. A satisfying witness is independently checked for each schema;
it is not sent as the model's answer. Native responses are validated against the complete original schema.

These cases caught an additional acceptance-client bug: `splitlines()` split U+2028/U+2029 characters inside
otherwise valid SSE JSON strings. The client now uses Strata's LF framing. A regression covers both APIs;
generated native streams also exercise those characters. This was a client parsing failure, not a server
schema-invalid success.

| Check | Observed result |
| --- | --- |
| Selected local CPU suite, CPython 3.12.3 | **125 passed**, including new tool-delta HTTP faults, generator/oracle checks, generated-witness HTTP tests and Unicode SSE handling. The existing 442 format cases are included under their documented policy. |
| Schema generation | **64 generated schemas, 62 structural signatures** after ignoring names and literal values; seed 3800. All schemas admitted without warnings. Independent witness/property checks additionally exercise two seeds locally. |
| Single RTX 4090, GPU 0 | **256/256 generated-schema HTTP 200 responses**, all independently valid; zero errors. Also **12/12 recovery pairs, 28/28 acceptance checks and 128/128 fixed-fixture requests** passed. Native PID 699279 throughout. |
| Dual RTX 4090, GPUs 0,1 | **256/256 generated-schema HTTP 200 responses**, all independently valid; zero errors. The same recovery, acceptance and fixture checks passed. Native PID 714422 throughout; native log confirms the 24/24 layer split. |
| Combined generated-schema coverage | **512 validated responses**. Every schema ran on each API with streaming on and off in both configurations. The two runs use identical schema fingerprints. |

Of the 64 generated schemas, 47 contain reference chains, 36 contain unusual string literals, 58 contain numeric
bounds and 53 contain constraint combinations. These categories overlap. The shape signatures ignore generated names and
literal values. The corpus also includes nested objects, arrays and disjoint unions. A generated campaign fails
on any schema-invalid HTTP 200, saves the exact counterexample request/response, and requires a successful
generation for every schema. All safe error responses remain visible in its counts.

[Complete raw evidence](structured-output-evidence/review-64fa76b3/README.md) includes schemas, witnesses,
per-request seeds, API/stream modes, generated values, native/server logs, before/after regressions, source
checksums and dependency versions. Each configuration executed 436 generation HTTP requests, including the
failure/recovery pairs, plus its capability probe. Both private engines and servers were stopped. The CUDA binary is unchanged and its checksum
matches the earlier committed evidence; no native source or backend build changed.

Reproduce with the same pinned model and a new evidence directory:

```bash
.venv/bin/python tools/check_structured_recovery.py --config strata-q2_0.json --output /tmp/generated-schema-check --random-cases 128 --generated-cases 64 --seed 3800 --timeout 240 --run
```

The generated subset has object-only roots and required properties, and is format-free; the existing fixture
campaign covers the application Story shape and explicit formats. This finite test has no automatic shrinking
or exhaustive schema exploration and does not claim coverage of private production schemas or other backends.
The broader historical test failures below remain visible; those suites were not rerun for this follow-up.
CI remains intentionally disabled at the owner's request. All results above are local executions.

## Review follow-up: duplicate keys in inference requests

The review of `d98723f7eb897884eb282a2440a4ad882ce3333c` identified ambiguity in incoming JSON. A raw HTTP
reproduction against both Chat Completions and Responses returned HTTP 200 with `{"n":1}` after a repeated
`properties` key silently replaced the earlier `const: 9` constraint. The reproduction uses the real HTTP/service
path and the mask-capable mock engine; it does not claim that an invalid value bypassed the parsed schema.

Inference request parsing now uses the same `object_pairs_hook` that already rejected duplicate output keys.
The hook checks each object before `json.loads` can discard a member. Chat Completions, Responses and Messages
reject duplicate keys with HTTP 400, including ordinary inference requests, so a duplicated format selector
cannot erase the evidence that structured output was requested. Nested schema constraints, repeated property
names, definitions, array items, const data and escaped spellings of the same key are covered. Identical names
in different objects and JSON-looking strings remain valid. Model loading and prompt preparation do not run
for these rejected requests. Generated-output validation retains its existing duplicate-key behavior.

| Check | Observed result |
| --- | --- |
| Before-fix HTTP reproduction | Both APIs returned HTTP 200 with `{"n":1}` for the reviewer's duplicate-key schema. |
| Request-hardening module | **15 passed**, including five new regression methods and raw duplicate-key requests in both APIs and streaming modes. |
| Exact selected CI command, executed locally | **118 passed** on CPython 3.12.3. The workflow now includes the entire request-hardening module alongside existing format, parser, mask, recovery and tool compatibility suites. |
| Main server compatibility module | **352 passed in 237.713 seconds**, including OpenAI/Anthropic HTTP handling and ordinary streaming, control/configuration and engine lifecycle checks. |

The repository owner intentionally disabled CI and instructed this follow-up to proceed without it. Completion
uses the recorded local verification above; no remote CI run or GitHub Actions result is claimed.

The broader historical failures remain documented below. Only the selected suites and `serve.test_server`
were rerun for this follow-up. Native code was unchanged; GPU inference was not rerun for a rejection that occurs
before preparation/loading. The earlier hardware measurements remain historical evidence for their recorded
revisions. [Before/after reproduction and complete raw test logs](structured-output-evidence/review-d98723f/README.md)
are committed with checksums and the tested source hashes.

The existing seeded campaign is retained. Its 14 schema structures are fixed fixtures; selection, order,
temperature and per-request sampling seeds vary. Property-based generation of schema structures remains future
coverage, as the review recommends. This follow-up makes no new schema-fuzzing or production-schema claim.

## Review follow-up: format semantics and native recovery

The review of `afb498ec470330899db27161e35eb18beb199787` found weak independent `format` semantics and missing
native failure-recovery coverage. The reviewed validator accepted `a@`, an email domain containing a space,
a time without a timezone, and a UUID with trailing hyphens in direct reproductions. These are validator
reproductions, not a claim that the native constrained decoder generated those values successfully.

The independent validator now admits an explicit verified set of eight formats. Email uses SMTP mailbox
syntax and valid DNS/IP domains; hostname A-labels use the existing pinned `idna` implementation. Date/time
and UUID checks enforce their documented syntax. Unknown/unverified formats are rejected regardless of optional
packages. The supported subset rejects leap seconds, year zero, internationalized formats and generalized
email address literals; there is no DNS or deliverability check. See [the format contract](STRUCTURED_OUTPUT.md).

Existing unittest/HTTP fixtures were extended rather than adding another runner. All 442 optional format cases
for those formats from JSON-Schema-Test-Suite revision `89e83a74014617ffbe79a56d83d14d054cd11417` are vendored
unmodified with their MIT notice and exercised offline. Eight upstream-valid leap-second values are explicitly
expected to fail under the documented stricter policy. All other upstream expectations hold. The corpus caught
invalid Punycode names that a DNS-label regex alone accepted; the `idna` checks fixed that gap. Post-decoding
email corruption returns 502 for both API routes and streaming modes in HTTP fault regressions.

The native recovery tool reuses the server's engine/configuration path, tokenizer loader, HTTP client and mask
fixtures. It starts a private native process, sends an invalid mask on its actual stdin, and also injects matcher
and mask-write failures after consuming a query. Each of the three faults is tested on both APIs and streaming
modes. Every failure must cancel the native request and return non-2xx; the immediate next request must produce
a validated object on the identical live process, with restart attempts rejected. The CPU fake-process suite
also executes this harness. No production injection endpoint or native code change was needed.

| Follow-up check | Observed result |
| --- | --- |
| Exact selected CI command, CPython 3.12.3 | **103 passed**, including all 442 official format cases under the documented policy. llguidance 1.9.1, jsonschema 4.26.0, idna 3.20. |
| Single RTX 4090, GPU 0 | **12/12 native fault/recovery pairs**, all failures HTTP 502 and immediate recoveries HTTP 200; native PID 494780 throughout. Existing **28/28 acceptance checks** passed. |
| Dual RTX 4090, GPUs 0,1 | **12/12 native fault/recovery pairs** with the same status requirements; native PID 511840 throughout. Existing **28/28 acceptance checks** passed. Native log confirms layers 0–23 on CUDA0, 24–47 on CUDA1. |
| Seeded randomized campaign, seed 3800 | **128/128 HTTP 200 responses per GPU configuration**, all independently validated; **256 successes, zero errors** total. Fourteen repository fixtures cover both APIs and streaming modes, with varied request order, sampling temperature and per-request seeds. |

The user selected repository fixtures for the campaign. They include the application's Story shape, nested
objects, bounds, unions, references, literal tool syntax and the eight formats. Each format fixture enumerates
known-valid RFC example strings, giving an independent finite-value check in addition to structural validation.
The client deliberately avoids jsonschema's global legacy Draft-3 time fallback; date/IP/UUID checks use the
verified built-ins, and the other format fixtures cannot accept arbitrary strings outside their valid enums.
The original schemas are retained, and every successful response must be a complete object with no duplicate
keys or non-finite values. Responses stream deltas must match the final document.

Buffered structured streaming is retained. Client read/first-response and proxy idle timeouts must cover queue
wait, prompt processing, reasoning, complete generation and validation. The checkers expose `--timeout`; the
documentation gives a concrete client configuration. No successful headers or heartbeat bytes are sent early.

[Raw evidence is now committed](structured-output-evidence/review-afb498ec/README.md): compressed complete
native/server/CPU logs, recovery reports, campaign schemas/seeds/values, checksums and runtime source hashes.
The initial campaign client rejected a valid modern time because it used the legacy checker; that client was
corrected. An initial supposed dual config used `gpus` instead of Strata's `gpu` key and ran on one card; it is
not counted above. The corrected run uses `gpu: [0, 1]` and asserts the native two-stage placement.
The existing CUDA build is unchanged. Both private engines and HTTP servers were closed after the runs.
The broader server/terminal failures below are historical, were not rerun for this follow-up, and remain listed
as failures rather than being included in the new passing counts.

Reproduce on an available GPU window, using the existing pinned weights and a new output directory:

```bash
.venv/bin/python tools/check_structured_recovery.py --config strata-q2_0.json --output /tmp/review-recovery --random-cases 128 --timeout 240 --seed 3800 --run
```

For a dual run, use a run config with `gpu: [0, 1]` and `layer_split: "auto"`. The checker requires its native log
to confirm both stages. This is finite testing on one host and repository schemas; it does not establish every
production schema, another host/backend, or universal successful completion.

## Review follow-up: Responses parser and validator boundary

The review of `6b827077c07a9c9a3cd8a96292c56cdde13c7c2b` identified a real bypass.
New HTTP regressions reproduced HTTP 200 responses containing undeclared function calls for `json_object`,
and incorrectly split messages for the reviewer's `json_schema` literal string, in both streaming modes.
The original results below did not cover this interaction and do not establish that this boundary was safe.

Structured Responses now pass `declared_only=True` to the output parser. The assembler rejects parsed tool
events before releasing text, rejects recorded tool-call state at completion, and requires independent validation
before releasing any structured answer. Plain Responses retain their existing parser options and tool behavior.

Real dual-GPU testing then found a separate literal-tokenization defect: llguidance's tokenizer callback
recognized `<tool_call>` as a type-4 control token inside a required JSON string. This failed safely with HTTP 502
during constrained decoding. The callback now encodes grammar text with an explicit plain-text span, so forced
literal text uses ordinary tokens. A small type-4 tokenizer regression reproduces the old callback failure.
No native mask or schema checks were relaxed, and no native source was changed.

| Follow-up check | Observed result |
| --- | --- |
| New parser/assembler and HTTP regressions | **18 passed**. Both formats and streaming modes; unmasked `json_object`; masked literal schema; injected parser faults; required validator; plain-tool compatibility; type-4 tokenizer. |
| Combined CPU check, including all existing Responses tests | **124 passed in 48.138 seconds** on CPython 3.12.3. An earlier run failed the unrelated sampler thread-count assertion; that test passed in isolation and the combined rerun passed. |
| Exact lightweight CI command, executed locally | **93 passed**, including the new module, grammar/validation/fault suites, structured-tool rejection, tool round trips and compaction compatibility. |
| Single RTX 4090, GPU 0 | **28/28 passed**, including HTTP 200 for the reviewer's literal schema in both streaming modes. |
| Dual RTX 4090, GPUs 0,1 | **28/28 passed**, including the same literal schema; automatic 24/24 layer split. |

The hardware runs use the pinned model, dependencies and existing CUDA build recorded below. Each now checks
16 successful requests and 12 explicit errors. The added positive tests require HTTP 200 and independently
validate the complete returned object against the original literal schema. A safe HTTP 502 is not a passing
result for those tests. Test servers were stopped after validation.

`.github/workflows/structured-output.yml` runs on every push and pull request with Python 3.12, the pinned
runtime dependencies, no model download and no GPU. Missing validation or grammar dependencies fail the job.
Each selected test group runs in a fresh process. This focused job excludes the unrelated hardware-sampler
lifecycle test, whose thread-count race also occurred when the whole Responses module ran in isolation.
The workflow uploads its raw test log for 14 days, including failures.

Local raw evidence is retained under `logs/pr3800-review/` (ignored by Git), including the before-fix reproduction,
the type-4 callback reproduction, the initial safe GPU failure, final CPU logs, `single.json`, `dual.json`,
and hardware server/engine logs and configs. Reproduce the new HTTP checks with
`python -m unittest -v serve.test_responses_structured`; the expanded hardware client remains
`python tools/check_structured_output.py --base-url http://127.0.0.1:18080 --output /tmp/structured-output.json`.

## Revision and environment

| Item | Evidence |
| --- | --- |
| Working branch | `harden/pr3800-structured-output` |
| Upstream base | `4b9e05a2a806a3fe24f7b2560d98626c37dade35` (`main`; upstream has no `master`) |
| Integrated PR head | `8979f87f582e00ceaa8dbe33fe490c6b6f658095`, [PR #3800](https://github.com/Niko1221/Strata/pull/3800) |
| Integration commit | `d5de79b4aaf054d5b404a44f33ee515537c112ed`; merge preserves the PR history |
| Hardening commits | `f71f4f2d`, `0dd1a2be`, `5061ae3e`; subsequent changes add audit tests and this report |
| Hardware | Linux; 2 × RTX 4090, 24,564 MiB each; driver 590.48.01; Threadripper PRO 5955WX; 251.5 GiB RAM |
| Build | CUDA 13.1.115, `sm_89`, Strata engine 0.1.51; 165/165 build steps completed |
| Dependencies tested | llguidance 1.9.1, jsonschema 4.26.0; focused tests on CPython 3.14.4 and 3.12.3 |
| Model | ISTA-DASLab Qwen3.8-Flash-Next GSQ-RCO Q2_0; revision `ed59f92082b1e93c0e96d60a8b11aab089b52f09` |

The requested workspace was initially empty. It now contains a fresh checkout and the dedicated branch.
Existing cached checkouts were inspected and left untouched. The merge resolved conflicts in `serve/server.py`
and `docs/DETAILS.md`, preserving current family support and current documentation. The original 450-test suite
passed before behavior changes. No remote branch was pushed.

The user authorized downloading missing weights. `hf download` acquired both pinned Q2_0 shards (66.4 GB).
The source setup separately acquired the necessary 5.21 GB of MTP tensor slices and prepared the draft layer.
Models and prepared artifacts live under `/mnt/large_storage_pool/data/tivek/strata-pr3800/`.

## Changes

| Area | Files and decisions |
| --- | --- |
| Admission and HTTP boundary | `serve/structured.py`, `serve/server.py`: require independent schema validation; strict complete-document parsing; reject unsupported combinations; delay structured SSE success headers until validation; reject interrupted Responses generations. |
| Grammar and protocol | `serve/constrain.py`, `serve/fake_mask_engine.py`: fatal matcher/mask failures; strict masks and queries; isolated request state; STOP plus cancellation reply after a failed query; normalize the backend's zero padding before transport. |
| Native sampling | `include/strata/core/token_mask.hpp`, `include/strata/core/verify.hpp`, `src/core/verify.cpp`, `sycl/src/core/verify.cpp`: strict transport parsing, mask dimensions covering every sampled row, fatal application/copy/sampler failures. |
| Dependencies and packaging | `requirements.txt`, `setup.py`, `packaging/lock_wheels.py`, three runtime locks: pin the validator and dependencies, include the llguidance wheel tag, evaluate cross-platform input markers correctly. Regenerated transitive locks also select platformdirs 4.13.5. |
| Tests and documentation | `serve/test_constrain.py`, `serve/test_structured.py`, `serve/test_responses.py`, new `serve/test_structured_hardening.py`, `tools/test_setup_pins.py`, `tests/terminal/test_packaging_pins.py`, new `tools/check_structured_output.py`, `docs/DETAILS.md`, `docs/STRUCTURED_OUTPUT.md`, this report. |

The existing independent-validation HTTP tests explicitly isolate the validator from masking. The new boundary,
fake-process and CUDA tests exercise operational masking and backend admission separately. Invalid output is
not made compliant by weakening its schema or by extracting a valid-looking substring.

## Failure paths addressed

| Failure | Result |
| --- | --- |
| llguidance or jsonschema absent; engine without masking; batch slots | Explicit non-2xx schema request error; no prompt-only schema fallback. Plain inference remains available. |
| Invalid/unsupported schema, compiler error/warning, lenient user options | Reject before generation. Draft 2020-12 keyword admission and strict compilation; original schema retained for independent validation. |
| Matcher failure, invalid draft lookahead or rollback failure | Fatal request error; no transition to free answer sampling. |
| Missing/empty/wrong-width masks, malformed MQ/MK/MKN, tokens without a query, ignored masks | Fatal request error. Python checks transport and every emitted token; native code checks dimensions and sampler results. |
| Failure after consuming MQ | Send STOP and a cancellation reply before draining. STOP alone left the native reader waiting indefinitely. A following request succeeds in regression tests. |
| Broken mask transport | Explicit 502 identifying mask transport; no misleading out-of-memory diagnosis. |
| Word-aligned real tokenizer | First hardware capability probe failed closed: llguidance returned 31,044 bytes for 248,320 tokens. Its trie reserves capacity for `vocab_size + 1`; the zero spare word is now removed before strict 31,040-byte transport. Nonzero or unexpected padding is rejected. |
| Malformed JSON, prose/fences, duplicate keys, non-finite numbers, schema violation | Independent validation fails; no HTTP success. |
| Incomplete output, exhausted budget, absent completion event | Non-2xx error, including structured streams. |
| Tools/MCP/images, wrong structured API fields, unsupported family | Explicit rejection rather than bypassing validation or masking. |

The padding finding was reproduced with a small word-aligned tokenizer and with the real prepared tokenizer.
The pinned library source confirms the allocation in
[`toktrie/src/toktree.rs`](https://github.com/guidance-ai/llguidance/blob/v1.9.1/toktrie/src/toktree.rs).
No native relaxation of mask dimensions was needed.

## Executed validation

| Check | Observed result |
| --- | --- |
| Original PR suites after integration | 450 tests passed; `/tmp/strata-pr3800-baseline-tests.log`. |
| Focused tests after the real-tokenizer fix | 136 passed on CPython 3.14.4 and 136 on CPython 3.12.3. |
| Final audit additions | Image rejection before preparation, compiler-warning rejection and dead-engine status checks passed. Final combined result recorded below. |
| Full server discovery | 1,085 run: 1,072 passed, 12 skipped, one keep-awake timing failure. The same failure reproduced in the same 383-test ordering on untouched base `main`; all six keep-awake tests pass in isolation. |
| Full terminal suite | 1,723 passed, 47 skipped, 23 failed; 338 subtests passed. Of the failures, 20 reproduced on untouched base `main`, two more reproduced after copying a generated model config to that baseline, and one uv virtualenv import-graph failure passes with the standard Python 3.12 venv outside the checkout. No unrelated product changes were made to hide these results. |
| CUDA compilation | Passed, 165/165 steps. Current native source hash `af8f67533b576bf2` matches `engine/BUILD.json`. |
| Runtime locks | Linux, Windows and macOS locks generated; offline pin/hash tests passed. Final generator checks recorded below. |
| Single RTX 4090, GPU 0 | **26/26 checks passed:** 14 HTTP 200 responses, eight HTTP 400 errors and four HTTP 502 errors. |
| Dual RTX 4090, GPUs 0,1 | **26/26 checks passed:** the same status counts. Automatic layer split: layers 0–23 on CUDA0, layers 24–47 and sampling head on CUDA1. |

Each hardware run includes 12 schema-constrained successful generations, one valid `json_object`, ordinary
inference, and 12 deliberately rejected requests. Schemas include nested required fields, forbidden additional
fields, enums, arrays with item/length constraints, numeric bounds, string length/pattern constraints, a disjoint
`oneOf`, and local references. Prompts ask the model to violate the schema. Four simultaneous requests use
different `const` schemas. Both APIs and their structured SSE modes are checked. Reasoning is closed at a
64-token budget before masked answering. One-token output budgets and unsupported schema/tool combinations
return non-2xx responses even with `stream: true`.

The CUDA logs contain multi-row mask exchanges and accepted speculative tokens. Both successful capability
probes report `constrained_decoding: true`. The first, unsuccessful single-GPU capability probe is preserved as
`logs/pr3800/single-before-padding-fix.json`.

Final combined focused result: **138 tests passed on CPython 3.12.3 in 41.366 seconds**.
Final runtime-lock checks: **all three `--check` commands passed**.

Raw local evidence is retained in `logs/pr3800/` (ignored by Git): `single.json`, `dual.json`, per-configuration
server/engine logs and configs, `focused.log`, `python312-focused.log`, `serve-all.log`, `terminal-all.log`,
baseline comparison logs, build/setup logs and `environment.json`. The test servers are stopped after validation;
the unrelated service on port 8080 is preserved. The generated launcher uses loopback port 18080.

## Reproduction

From this checkout, CPU checks:

```bash
python3 -m venv /tmp/strata-pr3800-test-venv
uv pip install --python /tmp/strata-pr3800-test-venv/bin/python -r requirements.txt -r requirements-monitor.txt pytest
/tmp/strata-pr3800-test-venv/bin/python -m unittest serve.test_constrain serve.test_structured serve.test_structured_hardening serve.test_responses tools.test_setup_pins tests.terminal.test_packaging_pins
/tmp/strata-pr3800-test-venv/bin/python -m unittest discover -s serve -t .
/tmp/strata-pr3800-test-venv/bin/python -m pytest tests/terminal -q
```

The broad commands have the baseline failures described above. The fake-process tests need a POSIX shell;
the native parser test needs a C++20 compiler. No model or GPU is needed for these CPU checks.

Acquire exactly the tested model files with the Hugging Face CLI:

```bash
hf download ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF \
  --revision ed59f92082b1e93c0e96d60a8b11aab089b52f09 --include 'Q2_0/*.gguf' \
  --local-dir /mnt/large_storage_pool/data/tivek/strata-pr3800/models/qwen-q2_0 --max-workers 2
```

Set up and run the single-card configuration (`--gpu 0`, since `--gpus 0` is rejected by this checkout):

```bash
PATH="$PWD/.venv/bin:$PATH" ./setup.sh --build --no-start --family qwen --model Q2_0 \
  --context 4096 --vision no --gpu 0 --port 18080 --yes --no-browser \
  --data-dir /mnt/large_storage_pool/data/tivek/strata-pr3800/data \
  --gguf-dir /mnt/large_storage_pool/data/tivek/strata-pr3800/models/qwen-q2_0/Q2_0
STRATA_MASK_STATS=1 ./run-q2_0.sh
```

After `ready`, in a second terminal:

```bash
.venv/bin/python tools/check_structured_output.py --base-url http://127.0.0.1:18080 --output logs/pr3800/single.json
```

Stop that launcher with Ctrl+C. Repeat setup with **`--gpus 0,1` in place of `--gpu 0`**, run the generated launcher,
and execute:

```bash
.venv/bin/python tools/check_structured_output.py --base-url http://127.0.0.1:18080 --output logs/pr3800/dual.json
```

The setup command can prepare MTP files on first use. The first model load took about 10.5 minutes from network
storage; warm single/dual loads took about 18/50 seconds. These are observed startup times, not throughput claims.
The final generated config is the dual-card configuration. Its single-card predecessor is preserved in
`logs/pr3800/single-config-backup.json`.

## Verified guarantees, untested assumptions and remaining risks

**Verified:** schema admission and fault checks fail closed in the deterministic tests; independent validation is
mandatory for schema requests; the tested successful CUDA responses pass client-side validation against their
original schemas; ordinary CUDA inference works on both configurations; mask exchanges include speculative rows
and run across the actual two-device layer split. No invalid tested generation was returned as HTTP success.

**Not tested:** HIP, SYCL and Metal builds/runtimes (toolchains or hardware unavailable); other models and
quantizations; long-context/endurance campaigns; every possible supported schema, prompt or runtime/device fault.
The schema validator, llguidance and device/runtime correctness are dependencies, not formally verified components.
Finite successful model tests do not establish universal successful completion or production-grade reliability.

**Limits:** schema roots remain object-only. Unsupported constraints and combinations are rejected; see the
[feature matrix](STRUCTURED_OUTPUT.md#json-schema-support). `if/then/else`, `not`, unique/contains/dependent/
unevaluated constraints, external references and lenient extensions are unsupported. Formats require available
independent checkers. Structured SSE delays all headers until validation and needs an adequate client timeout.
Continuous batching, structured tool/MCP turns and structured image inputs are not supported. The baseline
regression failures remain visible above. Library upgrades or changes to sampling/window protocols require
rerunning both the fault suite and hardware validation.
