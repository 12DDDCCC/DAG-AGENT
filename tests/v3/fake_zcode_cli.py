"""Deterministic ZCode wire-protocol fixture; never calls a model."""
import json
import os
from pathlib import Path
import sys
import time

if "--version" in sys.argv:
    print("0.17.0-test")
    raise SystemExit(0)
wd = Path(sys.argv[sys.argv.index("--cwd") + 1])
prompt = sys.argv[sys.argv.index("--prompt") + 1]
(wd / "process.json").write_text(json.dumps({"pid": os.getpid(), "home": os.environ["HOME"],
    "profile": os.environ["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"], "args": sys.argv[1:]}), encoding="utf8")
if "TEST_SLEEP" in prompt:
    time.sleep(60)
if "TEST_GARBAGE" in prompt:
    print("not a result")
    raise SystemExit(0)
if "TEST_SECRET" in prompt:
    cfg = json.loads(Path(os.environ['ZCODE_PERSONAL_PROVIDER_CONFIG_FILE']).read_text())
    print(cfg['test_secret'], file=sys.stderr)
(wd / "hello.txt").write_text("DAG-OK", encoding="utf8")
print(json.dumps({"sessionId": "sess-" + wd.name, "traceId": "trace-test",
                  "response": "DONE", "usage": {"totalTokens": 10}}))
if "TEST_NONZERO" in prompt:
    raise SystemExit(2)
