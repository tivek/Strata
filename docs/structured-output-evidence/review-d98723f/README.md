# Evidence for the review of d98723f

`reproduced-overwrite.json` and `reproduction.log.gz` record the original HTTP 200 responses with `{"n":1}`
on both APIs when a duplicate schema key overwrites the earlier `const: 9`. These use the actual HTTP/service
path and the mask-capable mock engine. `before-fix.log.gz` records the failing new regressions before the fix.

`request-hardening.log.gz` records all 15 request-hardening tests passing after the fix. `ci-local.log.gz`
records the exact selected workflow command passing all 118 tests. `server-compatibility.log.gz` records
all 352 main server tests passing; its engine-start checks took several minutes and completed without interruption.

The new cases cover nested duplicate schema keywords on both APIs with `stream` true and false, identical
duplicate values, escaped key names, discarded structured-output selectors, duplicates outside the schema,
and unchanged handling of JSON-looking string data and identical names in separate objects. Rejection is HTTP
400 before model loading or prompt preparation. The workflow definition includes these tests if CI is enabled later.

`summary.json` contains the tested source and evidence checksums. For example:

```bash
gzip -dc docs/structured-output-evidence/review-d98723f/request-hardening.log.gz
```

These are local execution results. The repository owner intentionally disabled CI and instructed this follow-up
to proceed without it. No GitHub Actions result is claimed. The earlier GPU logs and broader baseline failures
retain their original scope.
