import asyncio
import json
import time

from conftest import pid_alive

from core import engine
from core.engine import Event, Limits, run_turn


def run(coro):
    return asyncio.run(coro)


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_claude_turn_events_answer_and_session(fake_cli, tmp_path):
    events: list[Event] = []
    res = run(run_turn("claude", "hello there", None, cwd=tmp_path, on_event=events.append,
                       env={"ARCHI_CHAT_ID": "42"}, system_prompt="SYS", model="m-1", effort="high"))
    assert res.ok and res.session_id == "sess-new"
    assert res.answer.startswith("ANSWER") and "hello there" in res.answer
    steps = [e.text for e in events if e.kind == "step"]
    assert steps == ["Listing files", "Reading notes.md"]          # ToolSearch is plumbing, hidden
    texts = [e.text for e in events if e.kind == "text"]
    assert "Hel" in texts and "Hello" in texts                       # partial text accumulates
    assert events[-1].kind == "done" and events[-1].result is res
    call = calls(fake_cli)[0]
    argv = call["argv"]
    assert argv[:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert argv[argv.index("--model") + 1] == "m-1" and argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--append-system-prompt") + 1] == "SYS"
    assert "--resume" not in argv and "hello there" not in argv     # the prompt travels via stdin
    assert call["stdin"] == "hello there" and call["env"]["ARCHI_CHAT_ID"] == "42"


def test_claude_resume_and_slash_neutralised(fake_cli, tmp_path):
    res = run(run_turn("claude", "/clear everything", "sess-old", cwd=tmp_path))
    assert res.ok and res.session_id == "sess-old"
    call = calls(fake_cli)[0]
    assert call["argv"][call["argv"].index("--resume") + 1] == "sess-old"
    assert call["stdin"].startswith("[user message, verbatim")


def test_stale_session_retried_fresh(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "stale")
    res = run(run_turn("claude", "hi", "dead-session", cwd=tmp_path))
    assert res.ok and res.session_id == "sess-new"
    assert len(calls(fake_cli)) == 2 and "--resume" not in calls(fake_cli)[1]["argv"]


def test_images_go_through_stream_json(fake_cli, tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG fake")
    res = run(run_turn("claude", "what is this", None, cwd=tmp_path, images=[str(img)]))
    assert res.ok
    call = calls(fake_cli)[0]
    assert "--input-format" in call["argv"]
    msg = json.loads(call["stdin"])
    kinds = [c["type"] for c in msg["message"]["content"]]
    assert kinds == ["image", "text"] and msg["message"]["content"][0]["source"]["media_type"] == "image/png"


def test_error_result_and_failed_exit(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "error_result")
    res = run(run_turn("claude", "hi", None, cwd=tmp_path))
    assert not res.ok and "overloaded" in res.error
    monkeypatch.setenv("FAKE_MODE", "fail")
    res = run(run_turn("claude", "hi", None, cwd=tmp_path))
    assert not res.ok and "exited with 2" in res.error and "boom" in res.error


def test_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "nope"))
    res = run(run_turn("claude", "hi", None, cwd=tmp_path))
    assert not res.ok and "not found" in res.error


def _pids(tmp_path):
    for _ in range(100):
        p = tmp_path / "pids"
        if p.exists() and p.read_text().strip():
            return [int(x) for x in p.read_text().split()]
        time.sleep(0.05)
    raise AssertionError("fake CLI did not start")


def _all_dead(pids, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if not any(pid_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


def test_silence_limit_kills_process_group(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    t0 = time.monotonic()
    res = run(run_turn("claude", "hi", None, cwd=tmp_path, limits=Limits(silence_s=1.0, hard_s=30, kill_grace_s=1)))
    assert time.monotonic() - t0 < 10
    assert not res.ok and res.timed_out and "no output" in res.error
    assert _all_dead(_pids(tmp_path)), "the CLI or its child survived"


def test_hard_limit_while_streaming(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "stream_forever")
    res = run(run_turn("claude", "hi", None, cwd=tmp_path, limits=Limits(silence_s=5, hard_s=1.0, kill_grace_s=1)))
    assert res.timed_out and "longer than" in res.error
    assert _all_dead(_pids(tmp_path))


def test_cancel_event_kills_tree(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")

    async def scenario():
        cancel = asyncio.Event()
        task = asyncio.create_task(run_turn("claude", "hi", None, cwd=tmp_path, cancel=cancel,
                                            limits=Limits(silence_s=60, hard_s=60, kill_grace_s=1)))
        await asyncio.to_thread(_pids, tmp_path)
        cancel.set()
        return await asyncio.wait_for(task, 10)

    res = run(scenario())
    assert res.cancelled and not res.ok
    assert _all_dead(_pids(tmp_path))


def test_task_cancel_kills_tree(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")

    async def scenario():
        task = asyncio.create_task(run_turn("claude", "hi", None, cwd=tmp_path,
                                            limits=Limits(silence_s=60, hard_s=60, kill_grace_s=1)))
        await asyncio.to_thread(_pids, tmp_path)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "finished"

    assert run(scenario()) == "cancelled"
    assert _all_dead(_pids(tmp_path))


def test_codex_turn(fake_cli, tmp_path):
    events = []
    res = run(run_turn("codex", "fix it", None, cwd=tmp_path, on_event=events.append, system_prompt="RULES",
                       model="gpt-x"))
    assert res.ok and res.session_id == "thread-new"
    assert res.answer.startswith("CODEX") and "fix it" in res.answer and "RULES" in res.answer
    assert [e.text for e in events if e.kind == "step"] == ["Running tests", "Editing app.py"]
    argv = calls(fake_cli)[0]["argv"]
    assert argv[:5] == ["-a", "never", "exec", "-s", "danger-full-access"]
    assert "--json" in argv and argv[argv.index("-m") + 1] == "gpt-x" and argv[-1] == "-"


def test_codex_resume_and_stale(fake_cli, tmp_path, monkeypatch):
    res = run(run_turn("codex", "again", "thread-old", cwd=tmp_path))
    assert res.ok and res.session_id == "thread-old"
    argv = calls(fake_cli)[0]["argv"]
    assert argv[argv.index("resume") + 1] == "--json" and argv[-2:] == ["thread-old", "-"]
    monkeypatch.setenv("FAKE_MODE", "stale")
    res = run(run_turn("codex", "again", "thread-gone", cwd=tmp_path))
    assert res.ok and res.session_id == "thread-new"


def test_unknown_provider(tmp_path):
    res = run(run_turn("gpt", "x", None, cwd=tmp_path))
    assert not res.ok and "unknown provider" in res.error


def test_labels():
    assert engine.claude_step_label("WebFetch", {"url": "https://example.com/a"}) == "Opening example.com"
    assert engine.claude_step_label("Edit", {"file_path": "/a/b.py"}) == "Editing b.py"
    assert engine.claude_step_label("mcp__srv__do_thing", {}) == "srv: do_thing"
    assert engine.codex_command_label("bash -lc 'ls -la'") is None
    assert engine.codex_command_label("bash -lc 'pip install x'") == "Installing packages"
    assert engine.codex_command_label("make build").startswith("Running: make")


def test_claude_gets_the_oauth_token_from_the_config(fake_cli, home, monkeypatch):
    from core.config import Config
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    Config(home).set("CLAUDE_CODE_OAUTH_TOKEN", "tok-from-config")
    res = run(run_turn("claude", "hi", None, cwd=home))
    assert res.ok and calls(fake_cli)[0]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-from-config"
    res = run(run_turn("codex", "hi", None, cwd=home))
    assert res.ok and "CLAUDE_CODE_OAUTH_TOKEN" not in calls(fake_cli)[1]["env"]
    Config(home).set("CLAUDE_CODE_OAUTH_TOKEN", "")
    run(run_turn("claude", "hi", None, cwd=home))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in calls(fake_cli)[2]["env"]     # empty = the CLI's own login
