#!/usr/bin/env python3
"""Inject mask faults into a private native engine and verify immediate HTTP recovery.

  .venv/bin/python tools/check_structured_recovery.py --config strata-q2_0.json --output /tmp/recovery --run

Never attaches to an existing server. Uses the run config's weights and GPU selection,
an ephemeral loopback port, and a new output directory. No injection endpoint is installed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

from jsonschema import Draft202012Validator
from conversation_cache_parity import load_tokenizer
from check_structured_output import SIMPLE, request, response_value, schema_body
from serve import constrain
from serve.frontend import ChatTemplate
from serve.server import Service, StrataEngine, child_env, engine_args, gpu_list, read_engine_stage_log, serve
from serve.structured import StructuredOutputError


def check_recovery(engine, base, report, timeout=240):
    """The fake-process suite exercises this same harness; native runs also record the real process identity."""
    proc, generation = engine.proc, engine.gen
    report.update(pid=proc.pid, generation=generation, checks=[], passed=False)
    for fault in ("invalid-wire-mask", "matcher-failure", "mask-write-failure"):
        for responses in (False, True):
            for stream in (False, True):
                path = "/v1/responses" if responses else "/v1/chat/completions"
                writes, injected = [], []
                original_write = proc.stdin.write

                def write(line):
                    if line.startswith(("MK ", "MKN ")) and not injected and fault != "matcher-failure":
                        injected.append(fault)
                        if fault == "mask-write-failure":
                            raise BrokenPipeError("injected mask write failure; pipe remains open for STOP")
                        line = "MK invalid\n"
                    writes.append(line if not line.startswith(("MK ", "MKN ", "GEN ")) else line.split()[0])
                    return original_write(line)

                def matcher_failure(*args, **kwargs):
                    injected.append(fault)
                    raise StructuredOutputError("injected matcher failure after MQ")

                patch = (mock.patch.object(constrain.Constraint, "reply", side_effect=matcher_failure)
                         if fault == "matcher-failure" else mock.patch.object(proc.stdin, "write", side_effect=write))
                # Record STOP/cancellation replies even when the matcher itself fails.
                with mock.patch.object(engine, "restart", side_effect=AssertionError("recovery attempted an engine restart")):
                    with patch:
                        if fault == "matcher-failure":
                            with mock.patch.object(proc.stdin, "write", side_effect=write):
                                code, body = request(base, path, schema_body(SIMPLE, stream, responses), timeout)
                        else:
                            code, body = request(base, path, schema_body(SIMPLE, stream, responses), timeout)
                    record = {"fault": fault, "responses": responses, "stream": stream,
                              "status": code, "injected": len(injected), "writes": writes,
                              "finish_after_fault": dict(engine.last)}
                    report["checks"].append(record)
                    assert injected == [fault], record
                    assert code >= 400 and isinstance(body, dict) and "error" in body, body
                    record["error"] = body["error"]
                    assert engine.last.get("finish") == "cancel", record
                    if fault != "invalid-wire-mask":
                        assert "STOP\nMF\n" in writes, record
                    # This must be the immediate next request, on the identical live native process.
                    code, body = request(base, path, schema_body(SIMPLE, stream, responses), timeout)
                    record["recovery_status"] = code
                    assert code == 200, body
                    value = response_value(body, stream, responses)
                    Draft202012Validator(SIMPLE).validate(value)
                    record["recovery_value"] = value
                    assert engine.proc is proc and engine.gen == generation and proc.poll() is None, record
                    assert engine.last.get("finish") == "stop", engine.last
                    record["recovery_pid"] = engine.proc.pid
                    print(json.dumps({"fault": fault, "responses": responses, "stream": stream,
                                      "recovery_status": code, "pid": proc.pid}), flush=True)
    report["passed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory for raw evidence")
    parser.add_argument("--timeout", type=float, default=240, help="seconds for full generation and validation")
    parser.add_argument("--random-cases", type=int, default=128)
    parser.add_argument("--seed", type=int, default=3800)
    parser.add_argument("--generated-cases", type=int, default=0)
    parser.add_argument("--run", action="store_true", help="load the private engine and execute the checks")
    args = parser.parse_args()
    if args.timeout <= 0 or args.random_cases < 0 or (args.generated_cases != 0 and args.generated_cases < 4):
        parser.error("timeout must be positive, random-cases nonnegative, and generated-cases zero or at least four")
    if not args.run:
        print("Dry run: no engine started. Add --run during an available GPU window.")
        return
    cfg = json.loads(args.config.read_text())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    tokenizer = Path(cfg["tokenizer"])
    tok = load_tokenizer(tokenizer)
    template = ChatTemplate(tokenizer / "chat_template.jinja")
    env = child_env(cfg)
    env["STRATA_MASK_STATS"] = "1"
    report = {"passed": False}
    engine = httpd = None
    try:
        # A private test must not clean up other installs' leftover engines.
        with mock.patch.dict(os.environ, {"STRATA_KEEP_LEFTOVERS": "1"}):
            engine = StrataEngine(cfg["exe"], engine_args(cfg), cwd=cfg.get("cwd"),
                                  log=str(output / "engine.log"), env=env)
        assert engine.supports_mask and not engine.batch, "a native serial mask-capable engine is required"
        report["engine_info"] = dict(engine.info)
        report["configured_gpus"] = gpu_list(cfg)
        report["stages"] = read_engine_stage_log(str(output / "engine.log"), engine.log_start)
        if len(report["configured_gpus"]) > 1:
            assert len(report["stages"]) == len(report["configured_gpus"]), "native log did not confirm the GPU split"
            assert len({s["engine_ordinal"] for s in report["stages"]}) == len(report["configured_gpus"])
        svc = Service(engine, tok, template, model_name=cfg.get("model_name", "test"))
        httpd = serve(svc, port=0)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        check_recovery(engine, base, report, args.timeout)
        # Keep the same native process for ordinary acceptance and the randomized campaign too.
        report["passed"] = False
        command = [sys.executable, str(ROOT / "tools/check_structured_output.py"), "--base-url", base,
                   "--output", str(output / "http.json"), "--timeout", str(args.timeout),
                   "--random-cases", str(args.random_cases), "--seed", str(args.seed),
                   "--generated-cases", str(args.generated_cases)]
        with (output / "http.log").open("w") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        assert engine.proc.pid == report["pid"] and engine.gen == report["generation"] and engine.alive()
        report["passed"] = True
    except Exception as exc:
        report["passed"] = False
        report["failure"] = repr(exc)
        raise
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if engine is not None:
            engine.close()
        (output / "recovery.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: recovery, acceptance and campaign; raw evidence: {output}")


if __name__ == "__main__":
    main()
