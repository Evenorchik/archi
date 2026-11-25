"""Door hooks shared by every door: conversation rules, memory index, learning hook, durable jobs, outages."""
import os
import time

from core import afterturn, jobs, memory
from core.config import Config
from core.prompt import base_system_prompt
from test_telegram_door import OWNER, FakeRunner, dm, go, make_door, settle


def test_persona_and_hint_in_system_prompt(tmp_path):
    runner = FakeRunner()
    door, _api = make_door(tmp_path, runner)
    path = memory.persona_path(tmp_path, f"tg:{OWNER}:77")
    path.parent.mkdir(parents=True)
    path.write_text("This topic is for server work. Answer in two lines.")

    async def scenario():
        await door.handle_update(dm("hello", thread=77, topic_name="Server"))
        await settle(door)
        await door.handle_update(dm("hi", thread=78, topic_name="Other", mid=51))
        await settle(door)

    go(scenario())
    with_rules, without = runner.calls[0]["system_prompt"], runner.calls[1]["system_prompt"]
    assert "Answer in two lines." in with_rules and str(path) in with_rules
    assert "no rules of its own yet" in without and f"tg:{OWNER}:78.md" in without


def test_memory_index_only_for_codex(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("- [Owner prefers short answers](short.md) — style\n")
    monkeypatch.setenv("ARCHI_MEMORY_DIR", str(mem))
    cfg = Config(tmp_path)
    assert "Owner prefers short answers" in base_system_prompt(cfg, provider="codex")
    assert "Owner prefers short answers" not in base_system_prompt(cfg, provider="claude")
    (mem / "MEMORY.md").write_text("x\n" * 20000)
    assert "index truncated" in memory.index_text(tmp_path, limit=100)


def test_claude_slug_matches_the_cli():
    assert memory.claude_slug("/opt/archi") == "-opt-archi"
    assert memory.claude_slug("/root/my_agent.v2") == "-root-my-agent-v2"


def test_long_outage_unconfirms_topic_titles(tmp_path):
    door, _api = make_door(tmp_path)
    reg = door.registry
    reg.upsert(f"tg:{OWNER}:10", title="Work", title_confirmed=True, kind="dm")
    reg.upsert(f"tg:{OWNER}:main", title="main", kind="dm")
    assert door.check_downtime() is None                     # first start: nothing to compare with
    door.heartbeat()
    assert door.check_downtime() < 60
    assert reg.get(f"tg:{OWNER}:10").title_confirmed is True
    old = time.time() - 30 * 3600
    os.utime(door.alive_path, (old, old))
    assert door.check_downtime() > 29 * 3600
    assert reg.get(f"tg:{OWNER}:10").title_confirmed is False   # never probed with a possibly stale name
    assert door.topics.probe_allowed(reg.get(f"tg:{OWNER}:10")) is False


def test_learning_hook_after_delivery_and_learn_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(afterturn, "turn_finished", lambda door, key, **kw: calls.append((door, key, kw)) or None)
    monkeypatch.setattr(afterturn, "review_now", lambda door, key, **kw: f"review started for {key}")
    door, api = make_door(tmp_path)

    async def scenario():
        await door.handle_update(dm("hello", thread=77, topic_name="Server"))
        await settle(door)
        await door.handle_update(dm("/learn", thread=77, mid=52))
        await settle(door)

    go(scenario())
    assert calls == [("telegram", f"tg:{OWNER}:77",
                      dict(provider="claude", session_id="sess-1", tool_calls=1, home=door.home))]
    assert api.texts()[-1] == f"review started for tg:{OWNER}:77"


def test_finished_jobs_are_reported_by_a_turn(tmp_path, monkeypatch):
    runner = FakeRunner("The backup finished: 3 GB.")
    door, api = make_door(tmp_path, runner)
    door.registry.upsert(f"tg:{OWNER}:77", title="Server", kind="dm", state="open")
    door.registry.upsert(f"tg:{OWNER}:78", title="Gone", kind="dm", state="deleted")
    queue = [{"id": "j1", "conversation": f"tg:{OWNER}:77", "state": "ok", "label": "backup"},
             {"id": "j2", "conversation": f"tg:{OWNER}:78", "state": "failed", "label": "build"}]
    reported = []
    monkeypatch.setattr(jobs, "finished_unreported", lambda d, home=None: [j for j in queue if j["id"] not in reported])
    monkeypatch.setattr(jobs, "mark_reported", lambda job_id, home=None: reported.append(job_id))
    monkeypatch.setattr(jobs, "report_prompt", lambda job: f"[job {job['id']} {job['state']}]")

    async def scenario():
        assert await door.report_jobs() == 2
        await settle(door)
        assert await door.report_jobs() == 0                # reported once

    go(scenario())
    assert reported == ["j1", "j2"]
    prompts = [c["prompt"] for c in runner.calls]
    assert any(p.endswith("[job j1 ok]") for p in prompts) and any(p.endswith("[job j2 failed]") for p in prompts)
    convs = {c["env"]["ARCHI_CONVERSATION"] for c in runner.calls}
    assert convs == {f"tg:{OWNER}:77", f"tg:{OWNER}:main"}      # a deleted topic reports in the main chat
