"""Durable jobs: work that outlives the turn that started it (long commands, detached agent turns,
Claude workflows). The job runs detached from the door; when it ends, the door that launched it runs a turn
in the conversation that launched it, so the agent itself reports the result.

CONTRACT (doors call these; keep the signatures):
  finished_unreported(door, home=) -> list[dict]
      Jobs launched from conversations of this door that have ended (ok, failed or cancelled) and whose
      result was not reported yet. Every dict has at least: id, conversation (the conversation key),
      state, label. Cheap: doors call it every few seconds.
  report_prompt(job) -> str
      The prompt of the turn that reports the job, in the conversation that launched it.
  mark_reported(job_id, home=) -> None
      Called by the door after the report turn was started, so a job is reported once.
  status_lines(home=, conversation=None) -> list[str]
      Short human lines for the /jobs command (recent jobs, newest first; running ones marked); with
      `conversation` only the jobs launched from that conversation. [] when there are none.

Until durable jobs are installed these are no-ops.
"""
from __future__ import annotations

import os


def finished_unreported(door: str, home: str | os.PathLike | None = None) -> list[dict]:
    return []


def report_prompt(job: dict) -> str:
    return f"[job {job.get('id')} ended: {job.get('state')}]"


def mark_reported(job_id: str, home: str | os.PathLike | None = None) -> None:
    return None


def status_lines(home: str | os.PathLike | None = None, conversation: str | None = None) -> list[str]:
    return []
