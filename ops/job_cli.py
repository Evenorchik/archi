"""`archi job ...`: durable jobs, work that must outlive the turn that starts it (core/jobs.py). Stdlib only.

  archi job run [--label L] [--cwd DIR] [--timeout S] [--force] -- <command …>
  archi job agent [--label L] [--provider claude|codex] [--model M] [--timeout S] [--force] "<task>"
  archi job list [--conversation KEY] | show <id> | cancel <id> [--reason R] | reap
A job belongs to the conversation of the turn that starts it (ARCHI_CONVERSATION) or to --conversation KEY;
when it ends, the door starts a turn there with the result.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from core import jobs


def _started(job: dict) -> int:
    how = f"systemd unit {job['unit']}" if job.get("unit") else "a detached process"
    print(f"job {job['id']} started: «{job['label']}» (runs outside this turn, as {how})")
    print(f"When it ends, a new turn in {job['conversation']} gets the result and reports it.")
    print(f"Check it: archi job show {job['id']} · stop it: archi job cancel {job['id']}")
    return 0


def cmd_run(args) -> int:
    cmd = list(args.command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("usage: archi job run [--label L] [--cwd DIR] -- <command …>", file=sys.stderr)
        return 2
    argv, shell = (None, cmd[0]) if len(cmd) == 1 else (cmd, None)
    try:
        job = jobs.launch("run", argv=argv, shell=shell, label=args.label or "", cwd=args.cwd or os.getcwd(),
                          conversation=args.conversation, hard_s=args.timeout, force=args.force)
    except jobs.JobError as e:
        print(f"not started: {e}", file=sys.stderr)
        return 1
    return _started(job)


def cmd_agent(args) -> int:
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    try:
        job = jobs.launch("agent", prompt=prompt, label=args.label or "", provider=args.provider,
                          model=args.model or "", effort=args.effort or "", conversation=args.conversation,
                          hard_s=args.timeout, force=args.force)
    except jobs.JobError as e:
        print(f"not started: {e}", file=sys.stderr)
        return 1
    return _started(job)


def cmd_list(args) -> int:
    lines = jobs.status_lines(conversation=args.conversation, limit=args.limit)
    print("\n".join(lines) if lines else "no jobs")
    return 0


def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts))) if ts else "-"


def cmd_show(args) -> int:
    try:
        jobs.reap()
        j = jobs.get(args.id)
    except jobs.JobError as e:
        print(e, file=sys.stderr)
        return 1
    rows = [
        ("job", f"{j['id']} «{j['label']}» ({j.get('kind')})"),
        ("state", j["state"] + (f" · {j['error']}" if j.get("error") else "")),
        ("conversation", j.get("conversation")),
        ("ran", j.get("command")),
        ("created", _when(j.get("created_at"))),
        ("started", _when(j.get("started_at"))),
        ("ended", _when(j.get("ended_at"))),
        ("duration", jobs.human_s(j.get("duration_s"))),
        ("exit code", j.get("exit_code")),
        ("runs as", j.get("unit") or (f"pid {j['pid']}" if j.get("pid") else j.get("via") or "-")),
        ("reported", _when(j.get("reported_at")) if j.get("reported_at") else "not yet"),
        ("output", j["output_path"]),
    ]
    for k, v in rows:
        print(f"{k:13} {v if v not in (None, '') else '-'}")
    tail = jobs.result_tail(j, 2000)
    if tail:
        print("\n--- result (end) ---\n" + tail)
    return 0


def cmd_cancel(args) -> int:
    try:
        print(jobs.cancel(args.id, reason=args.reason or ""))
    except jobs.JobError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def cmd_reap(args) -> int:
    healed = jobs.reap()
    print("marked failed (runner gone): " + ", ".join(healed) if healed else "nothing to reap")
    return 0


def cmd_runner(args) -> int:
    return jobs.run_job(args.id)


def add_parser(sub) -> None:
    p = sub.add_parser("job", help="durable jobs: long work that outlives the turn (run, agent, list, show, cancel)")
    jsub = p.add_subparsers(dest="job_cmd", required=True)

    def common(s):
        s.add_argument("--label", help="short name shown in lists and in the report")
        s.add_argument("--conversation", help="report to this conversation (default: the current one)")
        s.add_argument("--timeout", type=float, metavar="S", help="hard limit in seconds")
        s.add_argument("--force", action="store_true", help="start even when the limit of running jobs is reached")

    s = jsub.add_parser("run", help="run a long shell command as a durable job: archi job run -- <command …>")
    common(s)
    s.add_argument("--cwd", help="working directory (default: the current one)")
    s.add_argument("command", nargs=argparse.REMAINDER,
                   help="after --: the command; a single argument runs through bash -c")
    s.set_defaults(func=cmd_run)

    s = jsub.add_parser("agent", help="a background agent turn in a fresh session; its answer is the result")
    common(s)
    s.add_argument("--provider", choices=["claude", "codex"])
    s.add_argument("--model")
    s.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    s.add_argument("prompt", help="the task, or - to read it from stdin")
    s.set_defaults(func=cmd_agent)

    s = jsub.add_parser("list", help="recent jobs, newest first")
    s.add_argument("--conversation")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.set_defaults(func=cmd_list)

    s = jsub.add_parser("show", help="one job: state, times, exit code, the end of its result")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    s = jsub.add_parser("cancel", help="stop a running job and everything it started")
    s.add_argument("id")
    s.add_argument("--reason")
    s.set_defaults(func=cmd_cancel)

    s = jsub.add_parser("reap", help="mark jobs whose runner died as failed (also done automatically)")
    s.set_defaults(func=cmd_reap)

    s = jsub.add_parser("_run", help=argparse.SUPPRESS)        # the detached runner itself
    s.add_argument("id")
    s.set_defaults(func=cmd_runner)
