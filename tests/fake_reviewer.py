#!/usr/bin/env python3
"""Stand-in for the reviewer CLI (claude or codex) in the learning-loop tests.

It does what a plan says, then answers like the real CLI. FAKE_FLAVOR = claude | codex; FAKE_PLAN = path of a
JSON list of operations; FAKE_REVIEW_MODE = ok | error | garbage | hang; FAKE_LOG = where every call is
logged (argv, cwd, the prompt). Operations:
  {"op": "write",   "store": "memory|skills|proposals|abs", "path": "rel/or/abs", "text": "..."}
  {"op": "replace", "store": ..., "path": ..., "old": "...", "new": "..."}
  {"op": "delete",  "store": ..., "path": ...}
A store is found the way the real CLI is confined: claude through its --add-dir directories (by name),
codex inside its -C working root. "abs" writes an absolute path: it stands for the live agent changing a
real store while the review runs.
"""
import json
import os
import sys
import time
from pathlib import Path

flavor = os.environ.get("FAKE_FLAVOR", "claude")
mode = os.environ.get("FAKE_REVIEW_MODE", "ok")
argv = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ.get("FAKE_LOG", "/dev/null"), "a") as fh:
    fh.write(json.dumps({"flavor": flavor, "argv": argv, "cwd": os.getcwd(), "prompt": prompt,
                         "env": {k: os.environ.get(k) for k in ("CLAUDE_CODE_DISABLE_AUTO_MEMORY",
                                                                 "ARCHI_CONVERSATION", "CLAUDECODE")}}) + "\n")

if mode == "hang":
    time.sleep(120)
if mode == "garbage":
    sys.stderr.write("fatal: something broke\n")
    sys.exit(3)


def store_dir(name: str) -> Path:
    if flavor == "codex":
        return Path(argv[argv.index("-C") + 1]) / name
    dirs = [Path(argv[i + 1]) for i, a in enumerate(argv) if a == "--add-dir"]
    for d in dirs:
        if d.name == name:
            return d
    raise SystemExit(f"fake reviewer: no writable {name} directory in {dirs}")


plan = json.loads(Path(os.environ["FAKE_PLAN"]).read_text()) if os.environ.get("FAKE_PLAN") else []
for op in plan:
    path = Path(op["path"]) if op["store"] == "abs" else store_dir(op["store"]) / op["path"]
    if op["op"] == "write":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(op["text"])
    elif op["op"] == "replace":
        path.write_text(path.read_text().replace(op["old"], op["new"]))
    elif op["op"] == "delete":          # what a Codex reviewer could do inside its sandbox
        path.unlink()

summary = os.environ.get("FAKE_SUMMARY", f"Did {len(plan)} change(s).")
if flavor == "codex":
    if mode == "error":
        print(json.dumps({"type": "turn.failed", "error": {"message": "model overloaded"}}))
        sys.exit(1)
    print(json.dumps({"type": "thread.started", "thread_id": "th-review"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": summary}}))
    print(json.dumps({"type": "turn.completed", "usage": {}}))
    sys.exit(0)
if mode == "error":
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "result": "API Error: overloaded"}))
    sys.exit(1)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": summary,
                  "total_cost_usd": 0.01}))
