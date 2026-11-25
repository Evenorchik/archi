#!/usr/bin/env python3
"""The weekly memory curator: merge duplicate facts, archive stale or derivable ones, rewrite orders as facts.

    curator.py [--home DIR] [--dry-run] [--no-notify] [--json]

Run by the `archi-learn-curator.timer` (weekly, installed by `archi learn enable`) or by `archi learn curate`.
The FIRST run is always a dry run: nothing changes, and its report goes to the owner (into the conversation
they used last), so they see what the curator would do before it does anything. Later runs change the store.

It works like a review (stores.py): one run at a time, a backup, a staged copy the reviewer CLI edits, the
same validation, then a three-way apply. Archiving is a copy in .archive/ plus a stub that is removed once
the copy exists: nothing is ever lost. Skills are not curated.
"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import learnlog, memory, notify  # noqa: E402
from core.config import Config, home as default_home  # noqa: E402
from tools.learn import prompts, reviewer, stores  # noqa: E402
from tools.learn.review import reviewer_settings  # noqa: E402

REPORT_CHARS = 3000


def state_path(home: Path) -> Path:
    return learnlog.learn_dir(home) / "curator.json"


def run(home: Path | str | None = None, *, dry: bool = False, notify_owner: bool = True,
        lock_wait: float = 3600) -> dict:
    home = Path(home) if home else default_home()
    cfg = learnlog.load_config(home)
    if not cfg.get("enabled", True):
        return {"type": "curator", "skipped": "the learning loop is disabled"}
    real = memory.memory_dir(home)
    facts = [f for f in real.glob("*.md") if f.name != stores.INDEX] if real.is_dir() else []
    if not facts:
        return {"type": "curator", "skipped": "the memory store is empty"}
    with learnlog.review_lock(home, wait_s=lock_wait) as got:
        if not got:
            return {"type": "curator", "skipped": "a review is running"}
        return _run(home, cfg, real, dry=dry, notify_owner=notify_owner)


def _run(home: Path, cfg: dict, real: Path, *, dry: bool, notify_owner: bool) -> dict:
    archi = Config(home)
    state = learnlog.jload(state_path(home), {})
    first = int(state.get("runs") or 0) == 0 and not state.get("seeded")
    dry = dry or first
    name = f"curator-{learnlog.run_stamp()}-{secrets.token_hex(2)}"
    st = stores.prepare(home, name, real_memory=real, real_skills=home / "skills", dry=dry,
                        keep_backups=int(learnlog.number(cfg, "keep_backups")))
    provider, model, effort = reviewer_settings(cfg, archi)
    prompt = prompts.curator_prompt(agent=archi.agent_name(), memory_dir=str(st.memory), home=str(home),
                                    memory_index=stores.read(st.memory / stores.INDEX), dry=dry)
    res = reviewer.run(provider, prompt, workdir=st.root, write_dirs=[st.memory], model=model, effort=effort,
                       timeout_s=max(learnlog.number(cfg, "timeout_s"), 1800), cfg=archi)
    report = (res.text or "").strip()

    problems = stores.memory_problems(st)
    fixes = [] if problems else stores.repair_index(st.memory) + stores.finalize_archives(st.memory)
    lines = [] if problems else [line for _, line in stores.describe_memory(st)]
    applied = {}
    if res.ok and not dry and not problems:
        applied = stores.apply_memory(st, real)
        skipped = set(applied.get("skipped") or [])
        lines = [line for rel, line in stores.describe_memory(st) if rel not in skipped]
        lines += [f"kept the live version of {x}: it changed while the curator ran" for x in sorted(skipped)]

    if res.ok:
        state["runs"] = int(state.get("runs") or 0) + (0 if dry else 1)
        state["seeded"] = True                    # the first (dry) run happened: the next one is real
        state["last_run"], state["last_dry"] = name, dry
        learnlog.jsave(state_path(home), state, mode=0o644)

    where = learnlog.most_recent_conversation(home, cfg)
    notified = False
    if notify_owner and where and cfg.get("notify", True):
        if not res.ok:
            text = f"🧹 **Memory curator** failed: {res.error[:300]}"
        elif problems:
            text = ("🧹 **Memory curator**: its changes failed validation, the memory is unchanged: "
                    + "; ".join(problems[:3]))
        elif dry:
            text = ("🧹 **Memory curator** · dry run, nothing was changed"
                    + (" (the first run is always a dry run; the next weekly run applies its changes)" if first else "")
                    + "\n\n" + (report[:REPORT_CHARS] or "No report."))
        else:
            text = "\n".join(["🧹 **Memory curator** · weekly pass",
                              *[f"· {x}" for x in (lines[:10] or ["no changes"])],
                              *(["", report[:1500]] if report else [])])
        notified = notify.post(where, text, silent=True, home=home)

    record = {"name": name, "type": "curator", "dry_run": dry, "first_run": first, "provider": provider,
              "model": model or "CLI default", "ok": res.ok, "error": res.error, "seconds": round(res.seconds),
              "cost_usd": res.cost_usd, "problems": problems, "fixes": fixes, "lines": lines, "applied": applied,
              "report": report[:6000], "notified": notified, "notified_conversation": where if notified else None,
              "stage": str(st.root), "base": str(st.base)}
    learnlog.record_run(home, name, record)
    if res.ok and not dry:
        shutil.rmtree(st.root, ignore_errors=True)
    return record


def summary(rec: dict) -> str:
    if rec.get("skipped"):
        return f"curator: skipped ({rec['skipped']})"
    head = (f"curator · {'dry run' if rec.get('dry_run') else 'live run'} · "
            + ("ok" if rec.get("ok") else f"FAILED: {rec.get('error')}") + f" · {rec.get('seconds')} s")
    out = [head] + [f"  {x}" for x in rec.get("lines") or []]
    if rec.get("problems"):
        out.append("  not applied, validation failed: " + "; ".join(rec["problems"][:5]))
    if rec.get("report"):
        out += ["", rec["report"]]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="weekly memory curator")
    ap.add_argument("--home")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rec = run(a.home, dry=a.dry_run, notify_owner=not a.no_notify)
    print(json.dumps(rec, ensure_ascii=False, indent=1) if a.json else summary(rec), flush=True)
    return 1 if rec.get("ok") is False else 0


if __name__ == "__main__":
    sys.exit(main())
