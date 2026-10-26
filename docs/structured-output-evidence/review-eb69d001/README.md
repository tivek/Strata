# Evidence for the review of eb69d001

`http-before.json.gz` retains the exact reviewer request with and without an explicit Chat text selector,
in both streaming modes. On the reviewed source, absent selectors returned 400 and explicit text selectors
returned 200 with ordinary text. `http-after.json.gz` repeats those same four requests after the guard fix;
all return 400. These are local HTTP/service executions with the existing mock engine, not GPU inference.

`request-hardening.log.gz` records all 18 tests passing, including three new regression methods. Its invalid
request matrix checks 50 combinations and asserts no model loading or prompt preparation. Positive cases
cover the correct API selectors, ordinary text options and nested/literal format data.
`cpu-tests.log.gz` records all 128 selected local tests passing, with no skips. The existing local runner
executes eight isolated groups. No remote CI run was requested or used; CI is intentionally disabled.

`regressions-before.log.gz` records the new tests on the original guard: 32 failures and 11 errors.
The errors include the fixture attempting to parse accepted SSE as an ordinary JSON rejection. These are
pre-fix regression failures, not additional server defects. The exact before/after HTTP evidence uses the
existing SSE-aware acceptance client instead.

`summary.json` records dependency versions, source hashes, counts and artifact checksums. The only production
change is Chat admission before model loading. Native code and generation behavior are unchanged; earlier
GPU and broader-suite results are historical and were not rerun for this follow-up.

Inspect the repaired responses:

```bash
gzip -dc docs/structured-output-evidence/review-eb69d001/http-after.json.gz
```
