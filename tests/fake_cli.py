#!/usr/bin/env python3
"""Stand-in for the `claude` and `codex` CLIs in tests. Behaviour comes from FAKE_MODE; FAKE_FLAVOR is
"claude" or "codex". Everything it receives is echoed back so tests can assert on it."""
import json
import os
import subprocess
import sys
import time

flavor = os.environ.get("FAKE_FLAVOR", "claude")
mode = os.environ.get("FAKE_MODE", "ok")
argv = sys.argv[1:]
stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
with open(os.environ.get("FAKE_LOG", "/dev/null"), "a") as fh:
    fh.write(json.dumps({"argv": argv, "stdin": stdin, "cwd": os.getcwd(),
                         "env": {k: v for k, v in os.environ.items() if k.startswith("ARCHI_")}}) + "\n")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if mode == "hang" or mode == "stream_forever":
    child = subprocess.Popen(["sleep", "300"])
    with open(os.environ["FAKE_PIDFILE"], "w") as fh:
        fh.write(f"{os.getpid()} {child.pid}")
    if mode == "hang":
        time.sleep(300)
    while True:
        out({"type": "system", "subtype": "ping"})
        time.sleep(0.05)

if mode == "fail":
    sys.stderr.write("boom: something broke\n")
    sys.exit(2)

if flavor == "claude":
    resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
    if mode == "stale" and resume:
        sys.stderr.write(f"No conversation found with session ID: {resume}\n")
        sys.exit(1)
    sid = resume or "sess-new"
    out({"type": "system", "subtype": "init", "session_id": sid})
    out({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls", "description": "Listing files"}},
        {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/x/notes.md"}},
        {"type": "tool_use", "id": "t3", "name": "ToolSearch", "input": {}},
    ]}, "session_id": sid})
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a\nb"}]}})
    out({"type": "stream_event", "event": {"type": "message_start"}})
    for piece in ("Hel", "lo"):
        out({"type": "stream_event", "event": {"type": "content_block_delta",
                                                "delta": {"type": "text_delta", "text": piece}}})
    if mode == "error_result":
        out({"type": "result", "subtype": "error_during_execution", "is_error": True,
             "result": "API Error: overloaded", "session_id": sid})
        sys.exit(1)
    answer = "ANSWER stdin=" + json.dumps(stdin[-300:])
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}, "session_id": sid})
    # one very long line: the reader must not choke on lines over 64 KB
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t9",
                                                  "content": "z" * 200000}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": answer, "session_id": sid})
    sys.exit(0)

# codex
resume = None
if "resume" in argv:
    rest = [a for a in argv[argv.index("resume") + 1:] if not a.startswith("-")]
    resume = rest[0] if rest else None
if mode == "stale" and resume:
    sys.stderr.write(f"Error: no rollout found for thread id {resume}\n")
    sys.exit(1)
tid = resume or "thread-new"
out({"type": "thread.started", "thread_id": tid})
out({"type": "turn.started"})
out({"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "bash -lc 'pytest -q'"}})
out({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "exit_code": 0}})
out({"type": "item.started", "item": {"id": "i2", "type": "command_execution", "command": "bash -lc 'cat x'"}})
out({"type": "item.completed", "item": {"id": "i3", "type": "file_change", "changes": [{"path": "/a/b/app.py", "kind": "update"}]}})
out({"type": "item.completed", "item": {"id": "i4", "type": "agent_message", "text": "CODEX stdin=" + json.dumps(stdin[-200:])}})
out({"type": "turn.completed", "usage": {"input_tokens": 1}})
