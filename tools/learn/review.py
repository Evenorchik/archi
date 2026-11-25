#!/usr/bin/env python3
"""A learning review: what is worth remembering or learning from a conversation's recent turns.

    review.py [--home DIR] [--conversation KEY] [--kind memory|skill|combined] [--dry-run] [--no-notify]
              [--explicit] [--lock-wait S] [--json]

Started detached by core/afterturn.py when a conversation crosses a threshold (or by /learn), or by hand
through `archi learn review`. Without --conversation every conversation with unreviewed turns is reviewed.

 1. one run at a time (state/learn/.review.lock); a run that cannot get the lock within --lock-wait gives
    up, and its turns wait for the next review (the watermark did not move);
 2. a digest of the conversation's turn log since its watermark (digest.py);
 3. a backup and staged copies of the memory store and, for skill reviews, of <home>/skills (stores.py);
 4. the reviewer CLI edits the staged copies (reviewer.py);
 5. validation, index repair, rollback of broken skill edits;
 6. what is valid is applied (three-way), the watermark advances, state/learn/runs/<name>.json is written
    and a silent notice goes into the conversation when something changed.
A dry run does 1-5 on copies under state/learn/dryrun/, prints what would change, and changes nothing real:
no store, no watermark, no notice. A reviewer that fails (CLI error, timeout) applies nothing either.
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
from core.redact import redact  # noqa: E402
from core.config import Config, home as default_home  # noqa: E402
from core.prompt import _tz  # noqa: E402
from tools.learn import digest, prompts, reviewer, stores  # noqa: E402

NOTICE_LINES = 8


def conversation_label(home: Path, key: str) -> str:
    try:
        from core.conversations import Registry
        conv = Registry(home / "state").get(key)
        title = conv.title if conv else ""
    except Exception:  # noqa: BLE001 - a label is cosmetic
        title = ""
    return f"«{title}» ({key})" if title else key


def reviewer_settings(cfg: dict, archi: Config) -> tuple[str, str, str]:
    """(provider, model, effort) for reviewer runs: the learning config, else config/archi.env."""
    provider = str(cfg.get("provider") or "").strip().lower() or archi.provider()
    if provider not in ("claude", "codex"):
        provider = archi.provider()
    model = str(cfg.get("model") or "").strip() or archi.get("CODEX_MODEL" if provider == "codex" else "CLAUDE_MODEL")
    effort = str(cfg.get("effort") or "").strip() if provider == "claude" else ""
    return provider, model, effort


def notice_text(lines: list[str], head: str = "🧠 **Session review**") -> str:
    """The notice: at most NOTICE_LINES lines of at most 300 characters, secrets removed (descriptions and
    errors are text the reviewer wrote)."""
    body = [f"· {redact(x)[:300]}" for x in lines[:NOTICE_LINES]]
    if len(lines) > NOTICE_LINES:
        body.append(f"· … and {len(lines) - NOTICE_LINES} more (archi learn status)")
    return "\n".join([head, *body])


def review_one(home: Path, key: str, *, kind: str = "combined", dry: bool = False, notify_owner: bool = True,
               explicit: bool = False, cfg: dict | None = None) -> dict:
    cfg = cfg or learnlog.load_config(home)
    archi = Config(home)
    skill_mode = cfg.get("skill_mode", "auto")
    if skill_mode == "off" and kind in ("skill", "combined"):
        kind = "memory"
    turns = learnlog.pending_turns(home, key, kind)
    if not turns:
        # /learn promised a notice: when another review took these turns meanwhile, say so.
        notified = bool(explicit and notify_owner and not dry and cfg.get("notify", True) and notify.post(
            key, "🧠 **Session review**: nothing new since the last review.", silent=True, home=home))
        return {"conversation": key, "skipped": "nothing new since the last review", "notified": notified}
    with_skills = kind in ("skill", "combined") and skill_mode != "off"
    name = f"{learnlog.run_stamp()}-{secrets.token_hex(2)}"
    d = digest.build(turns, max_turns=int(learnlog.number(cfg, "max_turns")),
                     budget=int(learnlog.number(cfg, "digest_chars")), tz=_tz(archi))
    real_memory, real_skills = memory.memory_dir(home), home / "skills"
    st = stores.prepare(home, name, real_memory=real_memory, real_skills=real_skills, dry=dry,
                        skills=with_skills and skill_mode == "auto", proposals=with_skills and skill_mode == "propose",
                        keep_backups=int(learnlog.number(cfg, "keep_backups")))
    provider, model, effort = reviewer_settings(cfg, archi)
    prompt = prompts.review_prompt(
        kind, d["text"], agent=archi.agent_name(), conversation=conversation_label(home, key),
        memory_dir=str(st.memory), home=str(home), memory_index=stores.read(st.memory / stores.INDEX),
        skills_dir=str(st.skills or real_skills),
        skills_index=stores.skills_index(st.skills or real_skills) if with_skills else "",
        proposals_dir=str(st.proposals or ""), skill_mode=skill_mode)
    res = reviewer.run(provider, prompt, workdir=st.root, write_dirs=st.write_dirs(), model=model, effort=effort,
                       timeout_s=learnlog.number(cfg, "timeout_s"), cfg=archi)

    restored = stores.undo_memory_deletions(st)
    problems = stores.memory_problems(st)
    fixes = [] if problems else stores.repair_index(st.memory) + stores.finalize_archives(st.memory, st.base / "memory")
    memory_lines = [] if problems else stores.describe_memory(st)
    small, big, rolled = stores.settle_skills(st, int(learnlog.number(cfg, "big_skill_lines")))
    rolled = restored + rolled
    proposals = sorted(stores.snapshot(st.proposals)) if st.proposals else []

    applied: dict = {}
    if res.ok and not dry:
        if not problems:
            applied["memory"] = stores.apply_memory(st, real_memory)
        if st.skills is not None:
            applied["skills"] = stores.apply_skills(st, real_skills)
        if proposals:
            dest = learnlog.learn_dir(home) / "proposals"
            for rel in proposals:
                stores.copy_file(st.proposals / rel, dest / rel)
        for rel in st.held:            # non-Markdown skill files: for the owner to look at, never live
            stores.copy_file(st.root / "held" / rel, learnlog.learn_dir(home) / "proposals" / name / rel)
        learnlog.set_watermark(home, key, d["last_seq"], kind)

    skipped_memory = set((applied.get("memory") or {}).get("skipped") or [])
    applied_skills = set((applied.get("skills") or {}).get("applied") or []) if not dry else {n for n, _ in small + big}
    lines = [line for rel, line in memory_lines if rel not in skipped_memory]
    lines += [line for n, line in small if n in applied_skills]
    lines += [f"**big change**: {line}" for n, line in big if n in applied_skills]
    lines += rolled
    lines += [f"kept the live version of {x}: it changed while the review ran"
              for x in sorted(skipped_memory | set((applied.get("skills") or {}).get("skipped") or []))]
    lines += [f"skill proposal: state/learn/proposals/{p}" for p in proposals]
    for skill in sorted({r.split("/")[0] for r in st.held}):
        names = ", ".join(r.split("/", 1)[1] for r in st.held if r.split("/")[0] == skill)
        lines.append(f"skill `{skill}`: not applied, only Markdown changes go live automatically: {names}"
                     + ("" if dry else f" (waiting in state/learn/proposals/{name}/{skill}/)"))
    if problems:
        lines.insert(0, "⚠️ the memory changes failed validation and were not applied: " + "; ".join(problems[:3]))

    notified = False
    if notify_owner and not dry and cfg.get("notify", True):
        text = ""
        if not res.ok:
            text = f"🧠 **Session review** failed: {redact(res.error)[:300]}" if explicit else ""
        elif lines:
            text = notice_text(lines)
        elif explicit:
            text = "🧠 **Session review**: nothing new worth saving."
        if text:
            notified = notify.post(key, text, silent=True, home=home)

    record = {"name": name, "type": "review", "conversation": key, "kind": kind, "dry_run": dry,
              "provider": provider, "model": model or "CLI default", "turns": d["turns"], "shown": d["shown"],
              "tool_calls": d["tool_calls"], "from_seq": d["first_seq"], "to_seq": d["last_seq"],
              "digest_chars": len(d["text"]), "ok": res.ok, "error": res.error, "seconds": round(res.seconds),
              "cost_usd": res.cost_usd, "problems": problems, "fixes": fixes, "lines": lines,
              "applied": applied, "notified": notified, "summary": res.text[:2000], "stage": str(st.root),
              "base": str(st.base)}
    learnlog.record_run(home, name, record)
    if not dry and res.ok:
        shutil.rmtree(st.root, ignore_errors=True)    # applied; the backup keeps the pre-run state
    return record


def run(home: Path | str | None = None, conversation: str | None = None, *, kind: str = "combined",
        dry: bool = False, notify_owner: bool = True, explicit: bool = False, lock_wait: float = 1800) -> list[dict]:
    home = Path(home) if home else default_home()
    cfg = learnlog.load_config(home)
    if not cfg.get("enabled", True) and not dry:
        return [{"skipped": "the learning loop is disabled" + (f" ({cfg['_broken']})" if cfg.get("_broken") else "")}]
    keys = [conversation] if conversation else [k for k in learnlog.conversations(home)
                                                if learnlog.pending_turns(home, k, kind)]
    keys = [k for k in keys if not learnlog.excluded(cfg, k)]
    if not keys:
        return [{"conversation": conversation, "skipped": "nothing to review"}]
    with learnlog.review_lock(home, wait_s=lock_wait) as got:
        if not got:
            return [{"conversation": conversation, "skipped": "another review is running"}]
        out = []
        for key in keys:
            # Settings can change while a run waits for the lock (or for the run before this one): read
            # them again, so a disable or an exclusion stops queued work too.
            cfg = learnlog.load_config(home, create=False)
            if not cfg.get("enabled", True) and not dry:
                out.append({"conversation": key, "skipped": "the learning loop is disabled"})
                continue
            if learnlog.excluded(cfg, key):
                out.append({"conversation": key, "skipped": "excluded from the learning loop"})
                continue
            try:
                out.append(review_one(home, key, kind=kind, dry=dry, notify_owner=notify_owner, explicit=explicit,
                                      cfg=cfg))
            except Exception as e:  # noqa: BLE001 - one broken conversation must not stop the others
                out.append({"conversation": key, "ok": False, "error": f"review crashed: {e!r}"})
        return out


def summary(rec: dict) -> str:
    """Human lines for one record (printed by `archi learn review` and into logs/learn.log)."""
    key = rec.get("conversation") or "all conversations"
    if rec.get("skipped"):
        return f"{key}: skipped ({rec['skipped']})"
    head = (f"{key} · {rec.get('kind')} · {rec.get('turns')} turn(s)"
            + (f" · {'ok' if rec.get('ok') else 'FAILED: ' + str(rec.get('error'))}" if "ok" in rec else "")
            + (f" · {rec['seconds']} s" if rec.get("seconds") is not None else ""))
    lines = [head] + [f"  {x}" for x in rec.get("lines") or (["nothing to save"] if rec.get("ok") else [])]
    if rec.get("dry_run"):
        lines.append(f"  (dry run: nothing was applied; the copies are in {rec.get('stage')})")
    if rec.get("summary"):
        lines.append("  reviewer: " + " ".join(str(rec["summary"]).split())[:400])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="learning review of a conversation")
    ap.add_argument("--home")
    ap.add_argument("--conversation")
    ap.add_argument("--kind", default="combined", choices=learnlog.KINDS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--explicit", action="store_true", help="asked for by the owner: report even 'nothing new'")
    ap.add_argument("--lock-wait", type=float, default=1800)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    recs = run(a.home, a.conversation, kind=a.kind, dry=a.dry_run, notify_owner=not a.no_notify,
               explicit=a.explicit, lock_wait=a.lock_wait)
    for rec in recs:
        print(json.dumps(rec, ensure_ascii=False, indent=1) if a.json else summary(rec), flush=True)
    return 1 if any(r.get("ok") is False for r in recs) else 0


if __name__ == "__main__":
    sys.exit(main())
