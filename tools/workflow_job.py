#!/usr/bin/env python3
"""workflow_job.py: run a Claude Code Workflow script as a DURABLE job (Claude only).

A workflow started with the Workflow tool runs inside the turn that started it, and the turn ends with its
final answer: a workflow still running then is killed and its report is lost. This launches a durable job
(core/jobs.py) whose only task is a nested `claude -p` that runs the one workflow to completion and prints
its result. The job runs detached from the door; when it ends, the door starts a turn in the conversation
that launched it, and the agent reports the result.

Usage:
    python3 tools/workflow_job.py launch --script <path.js> [--args-file <json>] [--label L]
                                         [--resume <runId>] [--model M] [--effort E] [--timeout S] [--force]
    python3 tools/workflow_job.py list

Notes:
- `--args-file` is a JSON file with the workflow's `args`. They are baked into a copy of the script
  (`globalThis.args = …` right after `export const meta = {…}`) instead of going through the Workflow tool's
  `args` parameter: the nested model reliably passes that as a string, so `args.foo` came back undefined.
  The copy sits next to the script as <stem>.wfjob-<hash of the args>.js: other args get another file, the
  same args the same path, so a resume re-wraps it identically.
- `--resume <runId>` asks the Workflow tool to resume an interrupted run (finished agents replay from cache).
- The nested turn waits for background work without the CLI's default ceiling
  (CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0): the ceiling used to cut long swarms off even inside a job.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import jobs  # noqa: E402


def inject_args(script_text: str, args_json: str) -> str:
    """Insert `globalThis.args = <literal>;` right after the mandatory `export const meta = {…}` block, so the
    workflow's global `args` is set by the script itself, deterministically and identically on a resume."""
    m = re.search(r"export\s+const\s+meta\s*=\s*\{", script_text)
    if not m:
        raise ValueError("a workflow script must begin with `export const meta = { … }`")
    i = m.end() - 1                 # the opening brace of the meta literal
    depth, n = 0, len(script_text)
    while i < n:
        ch = script_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        raise ValueError("could not find the end of the meta literal (unbalanced braces)")
    j = i + 1
    if j < n and script_text[j] == ";":
        j += 1
    # meta is a plain literal (no functions), so matching its braces is safe.
    return script_text[:j] + f"\nglobalThis.args = {args_json};\n" + script_text[j:]


def args_copy(script: Path, wrapped: str, args_json: str) -> Path:
    """Write the script with its args baked in next to it. One copy per distinct args: launches with other
    args never share (or race on) a file, and the same args give the same path again, which keeps a resume
    identical. Written atomically, so a concurrent launch with the same args reads a whole file."""
    digest = hashlib.sha256(args_json.encode("utf-8")).hexdigest()[:12]
    target = script.with_name(f"{script.stem}.wfjob-{digest}.js")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(wrapped, encoding="utf-8")
    os.replace(tmp, target)
    return target


def build_prompt(script: str, resume: str | None) -> str:
    """The nested turn's whole task: one Workflow call, wait, print only the result."""
    call = ["1. Invoke the Workflow tool in ONE call with:", f"     - scriptPath = {script}"]
    if resume:
        call.append(f"     - resumeFromRunId = {resume} (completed agents replay from cache)")
    return "\n".join([
        "You are a headless workflow runner. Do EXACTLY this and nothing else:",
        "",
        *call,
        "",
        "2. Wait for the workflow to finish (you are notified on completion).",
        "3. Output ONLY the workflow's final returned value as JSON: no preamble, no explanation,",
        "   no markdown fences. If it is already text, print it verbatim.",
        "",
        "Do not do any other work. Do not ask questions.",
    ])


def cmd_launch(a: argparse.Namespace) -> int:
    script = Path(a.script).resolve()
    if not script.is_file():
        print(f"script not found: {script}", file=sys.stderr)
        return 2
    if a.args_file:
        args_path = Path(a.args_file).resolve()
        try:
            args_json = json.dumps(json.loads(args_path.read_text(encoding="utf-8")), ensure_ascii=False)
        except OSError as e:
            print(f"args file not readable: {e}", file=sys.stderr)
            return 2
        except ValueError as e:
            print(f"args file is not valid JSON: {e}", file=sys.stderr)
            return 2
        try:
            wrapped = inject_args(script.read_text(encoding="utf-8"), args_json)
        except ValueError as e:
            print(f"cannot inject args: {e}", file=sys.stderr)
            return 2
        script = args_copy(script, wrapped, args_json)
    try:
        job = jobs.launch("workflow", prompt=build_prompt(str(script), a.resume),
                          label=a.label or f"workflow {Path(a.script).stem}", model=a.model or "",
                          effort=a.effort or "", conversation=a.conversation, hard_s=a.timeout, force=a.force,
                          extra_env={"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"},
                          extra={"script": str(script), "resume": a.resume or ""})
    except jobs.JobError as e:
        print(f"not started: {e}", file=sys.stderr)
        return 1
    print(f"job {job['id']} started: «{job['label']}» runs {script.name} detached; the result comes back as a new "
          f"turn in {job['conversation']}. Check: archi job show {job['id']}")
    return 0


def cmd_list(_a: argparse.Namespace) -> int:
    lines = jobs.status_lines(limit=20)
    print("\n".join(lines) if lines else "no jobs")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="run a Claude Code Workflow as a durable job")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("launch")
    p.add_argument("--script", required=True, help="path to the workflow script (.js)")
    p.add_argument("--args-file", help="JSON file with the workflow's `args`")
    p.add_argument("--label", help="short name shown in lists and in the report")
    p.add_argument("--resume", help="runId of an interrupted workflow to resume")
    p.add_argument("--model", help="model of the nested runner (default: CLAUDE_MODEL from the config)")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--conversation", help="report to this conversation (default: the current one)")
    p.add_argument("--timeout", type=float, metavar="S", help="hard limit in seconds (default 24 h)")
    p.add_argument("--force", action="store_true", help="start even when the limit of running jobs is reached")
    p.set_defaults(func=cmd_launch)
    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
