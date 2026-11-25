"""The self-learning loop: the turn log and its thresholds, the review pipeline (staging, validation, skill
rollback, the three-way apply) with a fake reviewer CLI, the curator, `archi learn` and the timer units."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import afterturn, detach, learnlog, notify
from ops import cli as ops_cli
from ops import timers
from tools.learn import curator, digest, review, reviewer, stores

FAKE_REVIEWER = Path(__file__).resolve().parent / "fake_reviewer.py"
KEY = "tg:1001:77"

SKILL = """---
name: writing-style
description: How the owner wants written answers to look
user-invocable: true
---

# Writing style
1. Lead with the answer.
2. Keep it short.
"""
LEGACY_SKILL = """---
name: legacy-tool
description: A third-party skill that already fails validation (no user-invocable)
---

Use the legacy tool like this: run it, then read the output carefully.
"""
FACT = """---
name: owner-timezone
description: The owner works in the Europe/Berlin time zone
metadata:
  type: user
---

The owner works in the Europe/Berlin time zone.
"""
NEW_FACT = """---
name: owner-deploy-day
description: The owner deploys on Fridays
metadata:
  type: project
---

The owner deploys on Fridays. **Why:** stated on 2026-11-25. **How to apply:** plan releases for Friday.
"""
INDEX = "- [Owner timezone](owner-timezone.md) — Europe/Berlin\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for name, text in (("writing-style", SKILL), ("legacy-tool", LEGACY_SKILL)):
        (home / "skills" / name).mkdir(parents=True)
        (home / "skills" / name / "SKILL.md").write_text(text)
    (home / "skills" / "writing-style" / ".history").mkdir()
    (home / "skills" / "writing-style" / ".history" / "SKILL.old.md").write_text("an older version")
    (home / "skills" / "NOTICE.md").write_text("notice\n")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "owner-timezone.md").write_text(FACT)
    (mem / "legacy-note.md").write_text("an old note without frontmatter\n")
    (mem / "MEMORY.md").write_text(INDEX)
    monkeypatch.setenv("ARCHI_HOME", str(home))
    monkeypatch.setenv("ARCHI_MEMORY_DIR", str(mem))
    log = tmp_path / "reviewer.log"
    for flavor in ("claude", "codex"):
        w = tmp_path / f"fake-{flavor}"
        w.write_text(f"#!/bin/sh\nFAKE_FLAVOR={flavor} exec {sys.executable} {FAKE_REVIEWER} \"$@\"\n")
        w.chmod(0o755)
        monkeypatch.setenv(f"{flavor.upper()}_BIN", str(w))
    monkeypatch.setenv("FAKE_LOG", str(log))
    posts = []
    monkeypatch.setattr(notify, "post", lambda key, text, silent=True, home=None: posts.append((key, text, silent)) or True)
    plan_path = tmp_path / "plan.json"

    def plan(ops):
        plan_path.write_text(json.dumps(ops))
        monkeypatch.setenv("FAKE_PLAN", str(plan_path))

    def calls():
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    return SimpleNamespace(home=home, mem=mem, skills=home / "skills", posts=posts, plan=plan, calls=calls, tmp=tmp_path)


def turns(home, n, key=KEY, tool_calls=2, prompt="owner message"):
    for i in range(n):
        afterturn.turn_finished("telegram", key, provider="claude", session_id="s1", tool_calls=tool_calls, home=home,
                                prompt=f"{prompt} {i}", answer=f"answer {i}", steps=[f"step {j}" for j in range(tool_calls)])


def started_reviews(monkeypatch):
    started = []
    monkeypatch.setattr(afterturn, "spawn_review", lambda home, key, kind, explicit=False:
                        started.append((key, kind, explicit)) or detach.Spawned(True, "test"))
    return started


# ------------------------------------------------------------------ config, turn log, thresholds
def test_config_is_created_with_the_defaults(tmp_path):
    cfg = learnlog.load_config(tmp_path)
    path = learnlog.learn_dir(tmp_path) / "config.json"
    assert path.exists() and cfg["enabled"] and cfg["notify"] and cfg["skill_mode"] == "auto"
    assert (cfg["memory_every_turns"], cfg["skill_every_tool_calls"], cfg["exclude_conversations"]) == (10, 60, [])
    learnlog.update_config(tmp_path, memory_every_turns=3, custom="kept")
    assert learnlog.load_config(tmp_path)["memory_every_turns"] == 3
    assert json.loads(path.read_text())["custom"] == "kept"
    path.write_text("{broken")
    assert learnlog.load_config(tmp_path)["memory_every_turns"] == 10 and path.read_text() == "{broken"


def test_thresholds_start_the_right_review(env, monkeypatch):
    started = started_reviews(monkeypatch)
    finish = lambda n_tools, prompt="msg": afterturn.turn_finished(  # noqa: E731
        "telegram", KEY, provider="claude", session_id="s1", tool_calls=n_tools, home=env.home, prompt=prompt,
        answer="a", steps=["x"] * n_tools)
    assert [finish(1) for _ in range(10)] == [None] * 9 + ["memory"]
    c = learnlog.load_counters(env.home)[KEY]
    assert (c["turns"], c["tools"], c["seq"]) == (0, 10, 10)
    assert finish(55) == "skill"                                   # 65 tool calls
    c = learnlog.load_counters(env.home)[KEY]
    assert (c["turns"], c["tools"]) == (1, 0)
    assert [finish(7) for _ in range(8)][-1] is None
    assert finish(7) == "combined"                                 # 10 owner turns and 63 tool calls together
    # a turn the door started (a job report) is logged and its tools count, but it is not an owner turn
    finish(1, prompt="[Message from the Archi door, not written by the owner] A durable job ended")
    c = learnlog.load_counters(env.home)[KEY]
    assert (c["turns"], c["tools"]) == (0, 1)
    assert [k for _, k, _ in started] == ["memory", "skill", "combined"]
    log = learnlog.read_turns(env.home, KEY)
    assert len(log) == 21 and log[-1]["from_door"] and [t["seq"] for t in log] == list(range(1, 22))


def test_a_review_that_cannot_start_is_retried_and_the_hook_never_raises(env):
    turns(env.home, 10)                                            # ARCHI_DETACH=off: nothing can start
    assert learnlog.load_counters(env.home)[KEY]["turns"] == 10
    turns(env.home, 1)
    assert learnlog.load_counters(env.home)[KEY]["turns"] == 11    # still counting, so the next turn tries again
    blocker = env.tmp / "a-file"
    blocker.write_text("x")
    assert afterturn.turn_finished("telegram", KEY, provider="claude", session_id="s", tool_calls=1,
                                   home=blocker) is None


def test_excluded_and_disabled_conversations_are_not_even_logged(env, monkeypatch):
    started = started_reviews(monkeypatch)
    learnlog.update_config(env.home, exclude_conversations=["tg:1001:*"], memory_every_turns=1)
    turns(env.home, 3)
    turns(env.home, 1, key="tg:2002:main")
    assert learnlog.read_turns(env.home, KEY) == [] and len(learnlog.read_turns(env.home, "tg:2002:main")) == 1
    learnlog.update_config(env.home, enabled=False)
    turns(env.home, 3, key="tg:2002:main")
    assert len(learnlog.read_turns(env.home, "tg:2002:main")) == 1
    assert [k for k, _, _ in started] == ["tg:2002:main"]


def test_turn_log_is_bounded_and_survives_lost_counters(env, monkeypatch):
    monkeypatch.setattr(learnlog, "MAX_LOG_TURNS", 30)
    monkeypatch.setattr(learnlog, "KEEP_LOG_TURNS", 20)
    turns(env.home, 60, prompt="x" * 50)
    seqs = [t["seq"] for t in learnlog.read_turns(env.home, KEY)]
    assert len(seqs) <= 30 and seqs == sorted(seqs) and seqs[-1] == 60
    learnlog.counters_path(env.home).unlink()
    turns(env.home, 1)
    assert learnlog.read_turns(env.home, KEY)[-1]["seq"] == 61    # numbered from the log, not from scratch
    learnlog.set_watermark(env.home, KEY, 58)
    assert [t["seq"] for t in learnlog.pending_turns(env.home, KEY)] == [59, 60, 61]
    learnlog.set_watermark(env.home, KEY, 1000)                    # a watermark past the end: the log was reset
    assert len(learnlog.pending_turns(env.home, KEY)) == len(seqs) + 1
    entry = learnlog.read_turns(env.home, KEY)[-1]
    assert set(entry) >= {"seq", "key", "ts", "door", "provider", "session", "prompt", "answer", "steps", "tool_calls"}


def test_review_now_answers_in_one_line(env, monkeypatch):
    ask = lambda: afterturn.review_now("telegram", KEY, provider="claude", session_id=None, home=env.home)  # noqa: E731
    assert "Nothing new" in ask()
    turns(env.home, 2)
    assert ask().startswith("⚠️ Could not start the review")      # ARCHI_DETACH=off
    started = started_reviews(monkeypatch)
    assert "Review started for the last 2 turns" in ask() and started == [(KEY, "combined", True)]
    assert learnlog.load_counters(env.home)[KEY]["turns"] == 0
    learnlog.update_config(env.home, exclude_conversations=[KEY])
    assert "excluded" in ask()
    learnlog.update_config(env.home, enabled=False)
    assert "off" in ask()


def test_spawn_review_runs_the_script_detached_without_secrets(env, monkeypatch):
    seen = {}

    def fake_spawn(argv, **kw):
        seen.update(argv=argv, **kw)
        return detach.Spawned(True, "systemd-run", unit=kw["name"] + ".service")

    monkeypatch.setattr(detach, "spawn", fake_spawn)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t" * 40)
    monkeypatch.setenv("ARCHI_UNIT_PREFIX", "archi-test")
    assert afterturn.spawn_review(env.home, KEY, "memory").ok
    assert seen["argv"][1].endswith("tools/learn/review.py") and seen["argv"][-4:] == ["--conversation", KEY,
                                                                                        "--kind", "memory"]
    assert seen["name"].startswith("archi-test-learn-") and seen["nice"] == 10
    assert seen["env"]["ARCHI_HOME"] == str(env.home) and seen["env"]["ARCHI_MEMORY_DIR"] == str(env.mem)
    assert not any("TOKEN" in k or v == "t" * 40 for k, v in seen["env"].items())


# ------------------------------------------------------------------ digest
def test_digest_marks_door_turns_sessions_and_removes_secrets():
    key = "sk-" + "a" * 30
    entries = [
        {"seq": 1, "ts": 1764075900, "session": "a", "steps": ["Reading a.md"], "tool_calls": 1,
         "prompt": "[25.11.2026 14:05, Wednesday · Europe/Berlin]\nplease stop adding tables", "answer": "ok"},
        {"seq": 2, "ts": 1764075960, "session": "b", "from_door": True, "steps": [], "tool_calls": 0,
         "prompt": "[Message from the Archi door, not written by the owner] a job ended", "answer": "reported"},
        {"seq": 3, "ts": 1764076020, "session": "b", "steps": [], "tool_calls": 0,
         "prompt": f"here is the key {key}", "answer": "x" * 5000},
    ]
    d = digest.build(entries)
    t = d["text"]
    assert "please stop adding tables" in t and "Wednesday ·" not in t
    assert "### DOOR (an automatic message from the door, not the owner)" in t and "a new session starts here" in t
    assert key not in t and "[secret removed]" in t and "(cut)" in t and "[steps ×1: Reading a.md]" in t
    assert (d["first_seq"], d["last_seq"], d["turns"], d["tool_calls"]) == (1, 3, 3, 1)
    assert digest.build(entries, budget=200)["text"].startswith("…(older part trimmed)…")
    assert "1 older turn(s)" in digest.build(entries, max_turns=2)["text"]
    assert learnlog.from_door("[25.11.2026 14:05, Wednesday · Europe/Berlin]\n" + entries[1]["prompt"])
    assert not learnlog.from_door(entries[0]["prompt"]) and not learnlog.from_door("quote: [Message from the Archi door")


# ------------------------------------------------------------------ stores
def test_frontmatter_secrets_index_merge_and_repair(tmp_path):
    assert stores.frontmatter(FACT)["type"] == "user"
    assert stores.frontmatter("---\nname: a\ndescription: b\ntype: feedback\n---\nx")["type"] == "feedback"
    native = "---\nname: n\ndescription: d\nmetadata:\n  node_type: memory\n  type: project\n  originSessionId: x\n---\n\nbody\n"
    assert stores.frontmatter(native)["type"] == "project" and stores.frontmatter("no block") is None
    assert not stores.SECRET_RE.search("The bot token: stored in config/archi.env, never in chat")
    assert stores.SECRET_RE.search("password = " + "Hunter2Hunter2Hunter2") and stores.SECRET_RE.search("sk-" + "b" * 25)
    base = "- [A](a.md) — a\n- [B](b.md) — b\n"
    ours = "- [A](a.md) — a, sharper\n- [C](c.md) — c\n"               # the review rewrote A, dropped B, added C
    theirs = "- [A](a.md) — a\n- [B](b.md) — b\n- [D](d.md) — d\n"     # the live agent added D meanwhile
    assert stores.merge_index(base, ours, theirs) == "- [A](a.md) — a, sharper\n- [D](d.md) — d\n- [C](c.md) — c\n"
    mem = tmp_path / "m"
    mem.mkdir()
    (mem / "a.md").write_text(FACT.replace("owner-timezone", "a"))
    (mem / "stub.md").write_text("---\nname: stub\ndescription: s\narchived: true\n---\n")
    (mem / "MEMORY.md").write_text("# Index\n- [Gone](gone.md) — x\n- [Stub](stub.md) — s\n- [A](a.md) — a\n- [A](a.md) — again\n")
    fixes = stores.repair_index(mem)
    assert (mem / "MEMORY.md").read_text() == "# Index\n- [A](a.md) — a\n" and len(fixes) == 3


# ------------------------------------------------------------------ the review pipeline
def test_review_applies_valid_memory_and_skill_edits(env, monkeypatch):
    turns(env.home, 3, prompt="stop putting a summary at the end, keep answers short")
    monkeypatch.setenv("ARCHI_CONVERSATION", KEY)       # as when the agent runs `archi learn review` in a turn
    monkeypatch.setenv("CLAUDECODE", "1")
    env.plan([
        {"op": "write", "store": "memory", "path": "owner-deploy-day.md", "text": NEW_FACT},
        {"op": "replace", "store": "skills", "path": "writing-style/SKILL.md", "old": "2. Keep it short.",
         "new": "2. Keep it short.\n3. No summary at the end: the owner reads the answer, not a recap."},
        {"op": "replace", "store": "skills", "path": "legacy-tool/SKILL.md", "old": "carefully.", "new": "carefully, twice."},
    ])
    [rec] = review.run(env.home, KEY)
    assert rec["ok"] and not rec["problems"], rec
    assert (env.mem / "owner-deploy-day.md").read_text() == NEW_FACT
    idx = (env.mem / "MEMORY.md").read_text()
    assert idx.startswith(INDEX) and "(owner-deploy-day.md) — The owner deploys on Fridays" in idx   # index repaired
    assert "No summary at the end" in (env.skills / "writing-style" / "SKILL.md").read_text()
    hist = list((env.skills / "writing-style" / ".history").iterdir())
    assert any(p.name.endswith(".review.md") and p.read_text() == SKILL for p in hist)
    assert (env.skills / "writing-style" / ".history" / "SKILL.old.md").read_text() == "an older version"
    assert "twice" in (env.skills / "legacy-tool" / "SKILL.md").read_text()   # its old problem is not blamed on the edit
    assert learnlog.watermark(env.home, KEY) == 3 and learnlog.pending_turns(env.home, KEY) == []
    [(key, text, silent)] = env.posts
    assert key == KEY and silent and text.startswith("🧠 **Session review**")
    assert "saved: The owner deploys on Fridays" in text and "skill `writing-style`: 1 line changed" in text
    # the reviewer only ever saw and could write the staged copies
    call = env.calls()[-1]
    argv = call["argv"]
    add_dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert len(add_dirs) == 2 and all("/state/learn/stage/" in d for d in add_dirs) and call["cwd"] in add_dirs[0]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits" and "Bash" not in argv[argv.index("--tools") + 1]
    assert call["env"] == {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "ARCHI_CONVERSATION": None, "CLAUDECODE": None}
    assert str(env.mem) + "/" not in call["prompt"] and "stop putting a summary at the end" in call["prompt"]
    assert "## Skills" in call["prompt"] and "- writing-style — How the owner wants" in call["prompt"]
    runs = learnlog.recent_runs(env.home)
    assert runs[0]["conversation"] == KEY and Path(runs[0]["base"], "memory", "owner-timezone.md").exists()


def test_review_rolls_back_broken_skills_and_rejects_unsafe_memory(env):
    turns(env.home, 2)
    leaked = NEW_FACT.replace("The owner deploys on Fridays.", "The owner's key is " + "sk-" + "c" * 30)
    env.plan([
        {"op": "write", "store": "memory", "path": "owner-deploy-day.md", "text": leaked},
        {"op": "replace", "store": "skills", "path": "writing-style/SKILL.md",
         "old": "description: How the owner wants written answers to look\n", "new": ""},
        {"op": "write", "store": "skills", "path": "half-skill/notes.md", "text": "no SKILL.md here"},
        {"op": "write", "store": "skills", "path": "NOTICE.md", "text": "rewritten\n"},
    ])
    before = stores.snapshot(env.mem), stores.snapshot(env.skills)
    [rec] = review.run(env.home, KEY)
    assert rec["ok"] and rec["problems"] == ["owner-deploy-day.md: looks like it contains a secret"]
    assert (stores.snapshot(env.mem), stores.snapshot(env.skills)) == before        # nothing reached the real stores
    assert not (env.skills / "half-skill").exists()
    text = env.posts[-1][1]
    assert "failed validation and were not applied" in text
    assert "skill `writing-style`: the edit failed validation" in text and "skill `half-skill`" in text
    assert "`NOTICE.md`: only files inside a skill's directory may change" in text
    assert learnlog.watermark(env.home, KEY) == 2          # reviewed: the same unsafe output is not retried forever


def test_live_changes_during_a_review_win(env):
    turns(env.home, 2)
    live_fact = FACT.replace("owner-timezone", "owner-editor").replace("Europe/Berlin time zone", "editor vim")
    env.plan([
        {"op": "write", "store": "memory", "path": "owner-timezone.md", "text": FACT.replace("zone.\n", "zone (CET).\n")},
        {"op": "write", "store": "memory", "path": "owner-deploy-day.md", "text": NEW_FACT},
        # meanwhile the live agent edits the same fact, writes a new one and indexes it
        {"op": "write", "store": "abs", "path": str(env.mem / "owner-timezone.md"), "text": FACT + "\nLive edit.\n"},
        {"op": "write", "store": "abs", "path": str(env.mem / "owner-editor.md"), "text": live_fact},
        {"op": "write", "store": "abs", "path": str(env.mem / "MEMORY.md"),
         "text": INDEX + "- [Owner editor](owner-editor.md) — vim\n"},
    ])
    [rec] = review.run(env.home, KEY)
    assert rec["ok"]
    assert (env.mem / "owner-timezone.md").read_text().endswith("Live edit.\n")
    assert (env.mem / "owner-editor.md").read_text() == live_fact and (env.mem / "owner-deploy-day.md").exists()
    idx = (env.mem / "MEMORY.md").read_text()
    assert "(owner-editor.md)" in idx and "(owner-deploy-day.md)" in idx and idx.count("(owner-timezone.md)") == 1
    assert "kept the live version of owner-timezone.md: it changed while the review ran" in rec["lines"]


def test_dry_run_changes_nothing_real(env):
    turns(env.home, 2)
    env.plan([
        {"op": "write", "store": "memory", "path": "owner-deploy-day.md", "text": NEW_FACT},
        {"op": "replace", "store": "skills", "path": "writing-style/SKILL.md", "old": "2. Keep it short.", "new": "2. Short."},
    ])
    before = stores.snapshot(env.mem), stores.snapshot(env.skills)
    [rec] = review.run(env.home, KEY, dry=True)
    assert rec["dry_run"] and rec["ok"]
    assert "saved: The owner deploys on Fridays" in rec["lines"] and "skill `writing-style`: 2 lines changed" in rec["lines"]
    assert (stores.snapshot(env.mem), stores.snapshot(env.skills)) == before
    assert learnlog.watermark(env.home, KEY) == 0 and env.posts == []
    assert "/state/learn/dryrun/" in rec["stage"] and Path(rec["stage"], "memory", "owner-deploy-day.md").exists()
    assert not (learnlog.learn_dir(env.home) / "backups").exists()
    assert "dry run: nothing was applied" in review.summary(rec)


def test_a_failed_reviewer_applies_nothing(env, monkeypatch):
    turns(env.home, 2)
    env.plan([{"op": "write", "store": "memory", "path": "owner-deploy-day.md", "text": NEW_FACT}])
    monkeypatch.setenv("FAKE_REVIEW_MODE", "error")
    [rec] = review.run(env.home, KEY)
    assert rec["ok"] is False and "overloaded" in rec["error"]
    assert not (env.mem / "owner-deploy-day.md").exists() and learnlog.watermark(env.home, KEY) == 0
    assert env.posts == []                                        # automatic runs fail silently (see archi learn status)
    monkeypatch.setenv("FAKE_REVIEW_MODE", "garbage")
    [rec] = review.run(env.home, KEY, explicit=True)              # /learn: the owner hears that it failed
    assert "claude exited with 3" in rec["error"] and "failed" in env.posts[-1][1]
    assert review.main(["--home", str(env.home), "--conversation", KEY, "--no-notify"]) == 1


def test_reviewer_commands_are_sandboxed(tmp_path):
    cmd = reviewer.claude_command([tmp_path / "memory", tmp_path / "skills"], model="claude-x", effort="high")
    assert cmd[1:3] == ["-p", "--output-format"] and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert cmd[cmd.index("--tools") + 1] == "Read,Write,Edit,Glob,Grep"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Glob,Grep"          # writes are never pre-approved
    assert {"Bash", "WebFetch", "Agent", "Skill"} <= set(cmd[cmd.index("--disallowedTools") + 1].split(","))
    assert cmd.count("--add-dir") == 2 and cmd[cmd.index("--model") + 1] == "claude-x" and "--strict-mcp-config" in cmd
    cx = reviewer.codex_command(tmp_path, model="gpt-x")
    assert cx[1:4] == ["-a", "never", "exec"] and cx[cx.index("-s") + 1] == "workspace-write"
    assert cx[cx.index("-C") + 1] == str(tmp_path) and "--skip-git-repo-check" in cx and "--json" in cx
    assert "danger-full-access" not in cx and cx[-3:] == ["-m", "gpt-x", "-"]


def test_review_with_the_codex_provider(env):
    learnlog.update_config(env.home, provider="codex")
    turns(env.home, 2)
    env.plan([{"op": "write", "store": "memory", "path": "owner-deploy-day.md", "text": NEW_FACT}])
    [rec] = review.run(env.home, KEY, kind="memory")
    assert rec["ok"] and rec["provider"] == "codex" and (env.mem / "owner-deploy-day.md").exists()
    call = env.calls()[-1]
    assert call["flavor"] == "codex" and "/state/learn/stage/" in call["argv"][call["argv"].index("-C") + 1]
    assert "## Skills" not in call["prompt"]                      # a memory review gets no skill section


def test_propose_mode_writes_proposals_instead_of_editing_skills(env):
    learnlog.update_config(env.home, skill_mode="propose")
    turns(env.home, 2)
    env.plan([{"op": "write", "store": "proposals", "path": "writing-style.md", "text": "# patch skill `writing-style`\n"}])
    before = stores.snapshot(env.skills)
    [rec] = review.run(env.home, KEY)
    assert rec["ok"] and stores.snapshot(env.skills) == before
    assert (learnlog.learn_dir(env.home) / "proposals" / "writing-style.md").exists()
    assert "skill proposal: state/learn/proposals/writing-style.md" in env.posts[-1][1]


# ------------------------------------------------------------------ curator
def test_curator_first_run_is_a_dry_run_then_it_archives(env):
    dup = FACT.replace("owner-timezone", "owner-tz-dup")
    (env.mem / "owner-tz-dup.md").write_text(dup)
    (env.mem / "MEMORY.md").write_text(INDEX + "- [Dup](owner-tz-dup.md) — dup\n")
    turns(env.home, 1)                                            # gives the report a conversation to go to
    env.plan([
        {"op": "write", "store": "memory", "path": ".archive/owner-tz-dup.md", "text": dup},
        {"op": "write", "store": "memory", "path": "owner-tz-dup.md",
         "text": "---\nname: owner-tz-dup\ndescription: duplicate of owner-timezone\narchived: true\n---\n"},
        {"op": "replace", "store": "memory", "path": "MEMORY.md", "old": "- [Dup](owner-tz-dup.md) — dup\n", "new": ""},
    ])
    before = stores.snapshot(env.mem)
    rec = curator.run(env.home)
    assert rec["dry_run"] and rec["first_run"] and rec["ok"] and stores.snapshot(env.mem) == before
    key, text, silent = env.posts[-1]
    assert key == KEY and silent and "dry run, nothing was changed" in text and "first run" in text
    rec = curator.run(env.home)
    assert rec["ok"] and not rec["dry_run"] and not rec["first_run"]
    assert not (env.mem / "owner-tz-dup.md").exists() and (env.mem / ".archive" / "owner-tz-dup.md").read_text() == dup
    assert (env.mem / "MEMORY.md").read_text() == INDEX
    assert "archived: The owner works in the Europe/Berlin time zone" in rec["lines"]
    prompts = [c["prompt"] for c in env.calls()]
    assert prompts[0].startswith("DRY RUN") and not prompts[1].startswith("DRY RUN") and "MEMORY CURATOR" in prompts[1]


def test_curator_skips_an_empty_store_and_a_disabled_loop(env):
    learnlog.update_config(env.home, enabled=False)
    assert curator.run(env.home)["skipped"] == "the learning loop is disabled"
    learnlog.update_config(env.home, enabled=True)
    for f in env.mem.iterdir():
        f.unlink()
    assert curator.run(env.home)["skipped"] == "the memory store is empty"


# ------------------------------------------------------------------ timers and the CLI
def test_curator_timer_units(tmp_path, monkeypatch):
    units = timers.curator_units("/opt/archi", python="/opt/archi/.venv/bin/python", user_home="/root",
                                 code_dir=Path("/opt/archi"))
    svc, tmr = units["archi-learn-curator.service"], units["archi-learn-curator.timer"]
    for line in ("Type=oneshot", "Environment=ARCHI_HOME=/opt/archi", "Nice=10", "WorkingDirectory=/opt/archi",
                 "ExecStart=/opt/archi/.venv/bin/python /opt/archi/tools/learn/curator.py --home /opt/archi"):
        assert line in svc
    for line in ("OnCalendar=Mon *-*-* 05:00:00", "Persistent=true", "Unit=archi-learn-curator.service",
                 "WantedBy=timers.target"):
        assert line in tmr
    calls = []
    monkeypatch.setattr(timers.service, "systemctl", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setenv("ARCHI_UNIT_DIR", str(tmp_path / "units"))
    assert timers.install_curator(tmp_path / "home", out=lambda s: None) == 0
    assert (tmp_path / "units" / "archi-learn-curator.timer").exists() and calls == []   # not systemd's directory
    assert timers.curator_state()["installed"]
    assert timers.remove_curator(out=lambda s: None) == 0 and not list((tmp_path / "units").iterdir())
    monkeypatch.setattr(timers, "systemd_managed", lambda d=None: True)
    assert timers.install_curator(tmp_path / "home", out=lambda s: None) == 0
    assert calls == [("daemon-reload",), ("enable", "--now", "archi-learn-curator.timer")]
    timers.remove_curator(out=lambda s: None)
    assert calls[-2:] == [("disable", "--now", "archi-learn-curator.timer"), ("daemon-reload",)]


def test_archi_learn_enable_disable_and_status(env, monkeypatch, capsys):
    monkeypatch.setenv("ARCHI_UNIT_DIR", str(env.tmp / "units"))
    assert ops_cli.main(["learn", "disable"]) == 0
    assert learnlog.load_config(env.home)["enabled"] is False
    turns(env.home, 2)
    assert learnlog.read_turns(env.home, KEY) == []
    assert ops_cli.main(["learn", "enable"]) == 0 and (env.tmp / "units" / "archi-learn-curator.timer").exists()
    turns(env.home, 2)
    capsys.readouterr()
    assert ops_cli.main(["learn"]) == 0
    out = capsys.readouterr().out
    assert "Self-learning loop: on" in out and "weekly curator: installed" in out
    assert f"{KEY} · 2 turn(s)" in out and "2 not reviewed yet" in out
    env.plan([])
    monkeypatch.setenv("ARCHI_CONVERSATION", KEY)
    assert ops_cli.main(["learn", "review", "--dry-run"]) == 0
    assert "dry run: nothing was applied" in capsys.readouterr().out
