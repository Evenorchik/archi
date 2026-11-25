"""`archi learn ...`: the self-learning loop (core/afterturn.py, core/learnlog.py, tools/learn/). Stdlib only.

  archi learn [status]                               settings, counters, recent runs, the curator timer
  archi learn review [--conversation KEY | --all] [--dry-run] [--kind K]
                                                     review now, in the foreground (default: this conversation)
  archi learn curate [--dry-run]                     the memory curator now
  archi learn enable | disable                       turn the whole loop on or off (incl. the weekly curator)
"""
from __future__ import annotations

import os
import time

from core import learnlog, memory
from core.config import Config

from . import timers


def _ago(ts) -> str:
    if not ts:
        return "never"
    s = int(time.time() - float(ts))
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def cmd_status(args, cfg: Config) -> int:
    home = cfg.home
    lc = learnlog.load_config(home)
    from tools.learn.review import reviewer_settings
    provider, model, effort = reviewer_settings(lc, cfg)
    mem = memory.memory_dir(home)
    facts = [f for f in mem.glob("*.md") if f.name != "MEMORY.md"] if mem.is_dir() else []
    if lc.get("_broken"):
        print(f"WARNING: {lc['_broken']}: the loop is paused until the file is fixed (nothing is logged or "
              "reviewed meanwhile)")
    print(f"Self-learning loop: {'on' if lc.get('enabled', True) else 'OFF'} · notices "
          f"{'on' if lc.get('notify', True) else 'off'} · skills: {lc.get('skill_mode')}")
    print(f"  memory review every {lc.get('memory_every_turns')} owner turns · skill review every "
          f"{lc.get('skill_every_tool_calls')} tool calls")
    print(f"  reviewer: {provider} · model {model or 'CLI default'}" + (f" · effort {effort}" if effort else "")
          + f" · limit {int(learnlog.number(lc, 'timeout_s'))} s")
    print(f"  memory store: {mem} ({len(facts)} fact file(s))")
    excl = lc.get("exclude_conversations") or []
    print(f"  excluded conversations: {', '.join(map(str, excl)) if excl else 'none'}")
    print(f"  settings: {learnlog.learn_dir(home) / 'config.json'}")
    st = timers.curator_state()
    if st.get("installed"):
        nxt = st.get("NextElapseUSecRealtime") or ""
        print(f"  weekly curator: installed ({st.get('ActiveState') or 'unit files present'})"
              + (f", next run {nxt}" if nxt and nxt != "0" else ""))
    else:
        print("  weekly curator: not installed (`archi learn enable` installs it)")
    counters = learnlog.load_counters(home)
    keys = learnlog.conversations(home)
    if keys:
        print("Conversations:")
        for key in keys:
            c = counters.get(key) or {}
            pending = len(learnlog.pending_turns(home, key))
            mark = " · EXCLUDED" if learnlog.excluded(lc, key) else ""
            print(f"  {key} · {c.get('turns', 0)} turn(s) / {c.get('tools', 0)} tool call(s) toward the next "
                  f"review · {pending} not reviewed yet · last turn {_ago(c.get('last_turn_at'))}{mark}")
    runs = learnlog.recent_runs(home, 6)
    if runs:
        print("Recent runs:")
        for r in runs:
            what = r.get("conversation") or ("curator" + (" (dry run)" if r.get("dry_run") else ""))
            if r.get("ok") is False:
                result = f"FAILED: {str(r.get('error'))[:120]}"
            else:
                n = len(r.get("lines") or [])
                result = (f"{n} change(s)" if n else "nothing saved") + (" (dry run)" if r.get("dry_run") else "")
            print(f"  {r.get('name', '?')[:15]} · {r.get('type')} · {what} · {result}")
    return 0


def cmd_review(args, cfg: Config) -> int:
    from tools.learn import review
    key = None if args.all else (args.conversation or os.environ.get("ARCHI_CONVERSATION") or None)
    recs = review.run(cfg.home, key, kind=args.kind, dry=args.dry_run, explicit=True, lock_wait=60)
    for rec in recs:
        print(review.summary(rec))
    return 1 if any(r.get("ok") is False for r in recs) else 0


def cmd_curate(args, cfg: Config) -> int:
    from tools.learn import curator
    rec = curator.run(cfg.home, dry=args.dry_run, lock_wait=60)
    print(curator.summary(rec))
    return 1 if rec.get("ok") is False else 0


def cmd_enable(args, cfg: Config) -> int:
    try:
        learnlog.update_config(cfg.home, enabled=True)
    except ValueError as e:
        print(f"not changed: {e}")
        return 1
    print("self-learning loop: on (reviews start after 10 owner turns or 60 tool calls of a conversation)")
    return timers.install_curator(cfg.home)


def cmd_disable(args, cfg: Config) -> int:
    try:
        learnlog.update_config(cfg.home, enabled=False)
    except ValueError as e:
        # The loop is already paused while its settings cannot be read; the timer goes anyway.
        print(f"not changed: {e}")
        timers.remove_curator()
        return 1
    print("self-learning loop: off (no turns are logged or reviewed; `archi learn enable` turns it back on)")
    return timers.remove_curator()


def cmd_learn(args) -> int:
    cfg = Config()
    handler = {"review": cmd_review, "curate": cmd_curate, "enable": cmd_enable,
               "disable": cmd_disable}.get(args.learn_cmd or "status", cmd_status)
    return handler(args, cfg)


def add_parser(sub) -> None:
    p = sub.add_parser("learn", help="the self-learning loop: status, review, curate, enable, disable")
    lsub = p.add_subparsers(dest="learn_cmd")
    lsub.add_parser("status", help="settings, counters, recent runs, the curator timer")
    s = lsub.add_parser("review", help="review now (default: the current conversation)")
    s.add_argument("--conversation", metavar="KEY")
    s.add_argument("--all", action="store_true", help="every conversation with unreviewed turns")
    s.add_argument("--dry-run", action="store_true", help="work on copies, change nothing, print what would change")
    s.add_argument("--kind", default="combined", choices=learnlog.KINDS)
    s = lsub.add_parser("curate", help="run the memory curator now (its first run is always a dry run)")
    s.add_argument("--dry-run", action="store_true")
    lsub.add_parser("enable", help="turn the loop on and install the weekly curator timer")
    lsub.add_parser("disable", help="turn the loop off and remove the curator timer")
    p.set_defaults(func=cmd_learn)
