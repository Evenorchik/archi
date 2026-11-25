"""After a turn: the hook between the doors and the self-learning loop (tools/learn).

CONTRACT (doors call these; keep the signatures):
  turn_finished(door, key, provider=, session_id=, tool_calls=, home=) -> str | None
      Called by a door right after the answer of a turn was DELIVERED (never before: a review must not
      compete with the turn it reviews). Counts the turn for conversation `key`; when a threshold is crossed
      it starts a detached background review and returns its kind ("memory" | "skill" | "combined"),
      else None. Must return within a second and must never raise.
  review_now(door, key, provider=, session_id=, home=) -> str
      The /learn command: start a review of this conversation's session now. Returns a one-line message for
      the owner (started / nothing to review / disabled). Must never raise.

Until the learning loop is installed these are no-ops.
"""
from __future__ import annotations

import os


def turn_finished(door: str, key: str, *, provider: str, session_id: str | None, tool_calls: int,
                  home: str | os.PathLike | None = None) -> str | None:
    return None


def review_now(door: str, key: str, *, provider: str, session_id: str | None,
               home: str | os.PathLike | None = None) -> str:
    return "The learning loop is not installed in this version."
