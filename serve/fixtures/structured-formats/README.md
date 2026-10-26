# JSON Schema format fixtures

Unmodified optional Draft 2020-12 format cases from
[JSON-Schema-Test-Suite](https://github.com/json-schema-org/JSON-Schema-Test-Suite/tree/89e83a74014617ffbe79a56d83d14d054cd11417/tests/draft2020-12/optional/format),
revision `89e83a74014617ffbe79a56d83d14d054cd11417`. Upstream's `test-schema.json` defines the case structure:
groups contain `schema` and `tests`; tests contain `data` and the expected `valid` boolean.
No upstream runner or runtime library is included.

The adjacent MIT [LICENSE](LICENSE) covers these files. Its copyright and permission notice are retained.
The existing unittest runner in `serve/test_structured_hardening.py` checks all 442 cases offline.
Eight valid leap-second examples are explicitly expected to fail under Strata's documented narrower time policy.
All other upstream expectations are retained. Do not replace unsupported semantics with permissive acceptance.
