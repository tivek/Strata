# Evidence for the review of 64fa76b3

`summary.json` records the tested source hashes, unchanged native binary checksum, dependency versions,
per-configuration counts, process identities and the confirmed dual-GPU stage plan. The single and dual runs
used the same 64 generated schemas (seed 3800), with 62 shape signatures after ignoring names/literal values.
Each schema ran in all four API/stream combinations. All 512 generated HTTP 200 responses passed independent
validation, and every schema had successes on both configurations.

The compressed `single-http.json.gz` and `dual-http.json.gz` reports retain the complete schemas, satisfying
witnesses, feature categories, fingerprints, per-request sampling seeds, statuses and actual generated values.
The full witness object is not supplied to the model; 228 single-GPU and 230 dual-GPU responses differ from it.
There were no generated-campaign errors. Every generated schema admitted without compiler warnings.

The native `engine.log.gz` and Python `server.log.gz` files preserve full execution logs. Recovery reports record
12 injected native fault/recovery pairs per configuration with unchanged process identity. The same processes
also passed the existing 28 acceptance checks and 128 fixed-fixture requests. The dual log confirms layers 0–23
on CUDA0 and 24–47 on CUDA1. Both owned native processes exited after the tests.

`tool-events-before.log.gz` records 14 failing assertions before the Chat tool-delta guard;
`tool-events-after.log.gz` records both targeted regressions passing. `sse-client-before.log.gz` records the
Unicode line-separator client parsing failures discovered by generated HTTP witnesses. `generator-tests.log.gz`
records the five targeted generator/SSE tests passing after the LF-framing fix. `cpu-tests.log.gz` records
all 125 selected local tests passing. `generator-admission.log.gz` records all 64 schema admission checks.

For example:

```bash
gzip -dc docs/structured-output-evidence/review-64fa76b3/dual-http.json.gz
gzip -dc docs/structured-output-evidence/review-64fa76b3/dual-engine.log.gz
```

The recursive generator is bounded, format-free, and has no automatic shrinking. The fixed-fixture campaign
still supplies the repository's Story and format cases. These results do not establish all schemas, all value
spellings or another backend/host. Prior broader-suite failures retain their recorded scope. CI is intentionally
disabled; these are measured local runs, not GitHub Actions results.
