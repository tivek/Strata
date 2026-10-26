# Evidence for the review of afb498ec

These files contain measured results from one Linux host with two RTX 4090s. The single run uses GPU 0;
the dual run uses GPUs 0 and 1 with native layer placement confirmed in the log. Both use the model revision,
CUDA build and 4096-token context documented in [the report](../../STRUCTURED_OUTPUT_REPORT.md).

`summary.json` records counts, native process IDs, the confirmed dual stage plan, dependency versions,
runtime source hashes and checksums of the adjacent files. The `check_recovery` function is hashed separately:
the single run preceded the additional startup stage-reporting assertions used for the dual run; its recovery
function and tested runtime sources are unchanged. No native code was changed or rebuilt for this follow-up.

The gzip files contain the complete native/server logs, HTTP and recovery JSON reports, campaign logs,
and the selected CPU test log. For example:

```bash
gzip -dc docs/structured-output-evidence/review-afb498ec/dual-recovery.json.gz
gzip -dc docs/structured-output-evidence/review-afb498ec/dual-engine.log.gz
```

Each recovery report records the actual injected write, HTTP error, native cancellation finish, immediate
HTTP 200 recovery and unchanged native process ID. HTTP reports retain schemas, seeds, statuses and every
successful campaign value. The before/after format report compares the reviewed module with the fixed module.
The official-case report records the pinned upstream corpus and the eight deliberately unsupported leap-second
cases; that corpus is also included with its MIT notice under `serve/fixtures/structured-formats`.

Only the final confirmed runs are included here. Local ignored logs also preserve an initial campaign-client
failure caused by jsonschema's legacy time checker and an incorrectly configured supposed dual run that used
only one GPU. Neither is counted as a passing dual-GPU run. Broader historical suite failures remain listed in
the report and are not included in these passing counts. These logs permit inspection of the measurements;
they do not replace independently rerunning the hardware test.
