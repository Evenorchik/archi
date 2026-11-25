"""Door behaviour with a fake Bot API and a fake engine. All ids are made up."""
import asyncio
import json

import pytest

from core import running
from core.config import Config
from core.conversations import Registry
from core.engine import Event, TurnResult
from doors.telegram.api import TgError
from doors.telegram.bot import PAIR_MAX_FAILURES, Door, parse_model_args

OWNER, STRANGER, BOT = 1001, 2002, 999
GROUP = -100500


class FakeApi:
    def __init__(self):
        self.calls = []
        self.fail = {}

    async def call(self, method, **params):
        params = {k: v for k, v in params.items() if v is not None}
        self.calls.append((method, params))
        rule = self.fail.get(method)
        if rule is not None:
            r = rule(params) if callable(rule) else rule
            if isinstance(r, Exception):
                raise r
        if method == "getMe":
            return {"id": BOT, "is_bot": True, "username": "test_archi_bot"}
        if method == "sendMessage":
            return {"message_id": len(self.calls)}
        return True

    async def download(self, file_id, dest):
        dest.write_bytes(b"data")
        return dest

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]

    def texts(self):
        return [p["text"] for p in self.sent()]


class FakeRunner:
    def __init__(self, answer="Hi from the agent", wait_cancel=False):
        self.answer, self.wait_cancel, self.calls = answer, wait_cancel, []
        self.started = asyncio.Event()

    async def __call__(self, provider, prompt, session_id, *, cwd, images=None, on_event=None, env=None,
                       system_prompt="", model="", effort="", cancel=None, limits=None):
        self.calls.append(dict(provider=provider, prompt=prompt, session_id=session_id, env=env, images=images,
                               system_prompt=system_prompt, model=model, effort=effort, cwd=str(cwd)))
        on_event(Event("step", "Reading notes.md"))
        self.started.set()
        if self.wait_cancel:
            await cancel.wait()
            return TurnResult("", "sess-1", False, "cancelled", cancelled=True)
        return TurnResult(self.answer, "sess-1", True)


def make_door(tmp_path, runner=None, owner=OWNER, **cfg_values):
    cfg = Config(tmp_path)
    values = {"TELEGRAM_BOT_TOKEN": "x", "OWNER_ID": str(owner) if owner else ""}
    values.update(cfg_values)
    cfg.update(values)
    api = FakeApi()
    door = Door(api, cfg, Registry(tmp_path / "state"), runner=runner or FakeRunner(), log=lambda *a: None,
                debounce_s=0, album_s=0)
    door.me = {"id": BOT, "username": "test_archi_bot"}
    return door, api


async def settle(door):
    for _ in range(200):
        pending = [t for t in door.tasks if not t.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=5)


def user(uid=OWNER, lang="en"):
    return {"id": uid, "is_bot": False, "first_name": "U", "language_code": lang}


def dm(text=None, uid=OWNER, thread=None, topic_name=None, implicit=False, mid=50, **extra):
    msg = {"message_id": mid, "from": user(uid, extra.pop("lang", "en")), "chat": {"id": uid, "type": "private"},
           "date": 1}
    if text is not None:
        msg["text"] = text
    if thread:
        msg.update(message_thread_id=thread, is_topic_message=True)
        if topic_name:
            created = {"name": topic_name, "icon_color": 1}
            if implicit:
                created["is_name_implicit"] = True
            msg["reply_to_message"] = {"message_id": thread, "chat": msg["chat"], "date": 1,
                                       "message_thread_id": thread, "forum_topic_created": created,
                                       "is_topic_message": True}
    msg.update(extra)
    return {"update_id": mid, "message": msg}


def grp(text=None, uid=OWNER, thread=None, mid=60, **extra):
    msg = {"message_id": mid, "from": user(uid), "date": 1,
           "chat": {"id": GROUP, "type": "supergroup", "title": "Test group", "is_forum": True}}
    if text is not None:
        msg["text"] = text
    if thread:
        msg.update(message_thread_id=thread, is_topic_message=True)
    msg.update(extra)
    return {"update_id": mid, "message": msg}


def go(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ owner and pairing
def test_pairing_code_makes_sender_owner(tmp_path):
    door, api = make_door(tmp_path, owner=None, PAIRING_CODE="tiger-1234")

    async def scenario():
        await door.handle_update(dm("wrong code", uid=STRANGER))
        assert api.calls == [] and door.cfg.owner_id() is None
        await door.handle_update(dm("tiger-1234", uid=STRANGER, mid=51))
        await settle(door)

    go(scenario())
    assert door.cfg.owner_id() == STRANGER and door.cfg.get("PAIRING_CODE") == ""
    assert "Paired" in api.texts()[0]


def test_strangers_are_ignored_silently(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)

    async def scenario():
        await door.handle_update(dm("hello", uid=STRANGER))
        await door.handle_update(grp("hello", uid=STRANGER))
        await settle(door)

    go(scenario())
    assert api.calls == [] and runner.calls == []


# ------------------------------------------------------------------ turns
def test_topic_message_runs_turn_with_env_and_prompt(tmp_path):
    runner = FakeRunner("**Done**")
    door, api = make_door(tmp_path, runner)

    async def scenario():
        await door.handle_update(dm("check the logs", thread=77, topic_name="Server work"))
        await settle(door)

    go(scenario())
    call = runner.calls[0]
    assert call["env"]["ARCHI_CHAT_ID"] == str(OWNER) and call["env"]["ARCHI_THREAD_ID"] == "77"
    assert call["env"]["ARCHI_CONVERSATION"] == f"tg:{OWNER}:77" and call["env"]["ARCHI_DOOR"] == "telegram"
    assert "Server work" in call["system_prompt"] and 'archi tg topic create "<name>"' in call["system_prompt"]
    assert call["prompt"].startswith("[") and call["prompt"].endswith("check the logs")
    final = api.sent()[-1]
    assert final["message_thread_id"] == 77 and final["text"] == "<b>Done</b>" and final["parse_mode"] == "HTML"
    assert api.sent("sendMessageDraft")[0]["can_stop"] is True
    conv = door.registry.get(f"tg:{OWNER}:77")
    assert conv.sessions == {"claude": "sess-1"} and conv.title == "Server work"
    assert conv.title_confirmed is False           # learned from an old message: never used to probe


def test_main_conversation_and_group_general(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)

    async def scenario():
        await door.handle_update(dm("hi"))
        await door.handle_update(grp("hi group"))
        await settle(door)

    go(scenario())
    keys = sorted(c["env"]["ARCHI_CONVERSATION"] for c in runner.calls)
    assert keys == sorted([f"tg:{OWNER}:main", f"tg:{GROUP}:main"])
    assert {c["env"]["ARCHI_THREAD_ID"] for c in runner.calls} == {""}
    assert door.registry.chat(GROUP)["owner_seen"] is True


def test_split_messages_are_merged(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    door.debounce_s = 0.2

    async def scenario():
        await door.handle_update(dm("part one", mid=1))
        await door.handle_update(dm("part two", mid=2))
        await settle(door)

    go(scenario())
    assert len(runner.calls) == 1 and runner.calls[0]["prompt"].endswith("part one\npart two")


def test_answer_markers_send_files(tmp_path):
    f = tmp_path / "report.txt"
    f.write_text("r")
    runner = FakeRunner(f"Here it is.\n[FILE {f}]\n[FILE /nonexistent/x.txt]")
    door, api = make_door(tmp_path, runner)

    async def scenario():
        await door.handle_update(dm("send report"))
        await settle(door)

    go(scenario())
    assert api.sent("sendDocument")[0]["document"] == f
    assert any("File not found" in t for t in api.texts())


# ------------------------------------------------------------------ topic lifecycle
def test_topic_lifecycle_events(tmp_path):
    door, api = make_door(tmp_path)
    key = f"tg:{GROUP}:5"

    async def scenario():
        await door.handle_update(grp(thread=5, forum_topic_created={"name": "Ideas", "icon_color": 1}))
        assert door.registry.get(key).title_confirmed is True
        await door.handle_update(grp(thread=5, mid=61, forum_topic_edited={"name": "Big ideas"}))
        assert door.registry.get(key).title == "Big ideas"
        await door.handle_update(grp(thread=5, mid=62, forum_topic_closed={}))
        assert door.registry.get(key).state == "closed"
        await door.handle_update(grp(thread=5, mid=63, forum_topic_reopened={}))
        assert door.registry.get(key).state == "open"

    go(scenario())
    assert door.registry.get(key).kind == "group"


def test_bot_created_topic_event_in_private_chat_is_learned(tmp_path):
    door, api = make_door(tmp_path)
    upd = dm(thread=88, forum_topic_created={"name": "Made by me", "icon_color": 1})
    upd["message"]["from"] = {"id": BOT, "is_bot": True, "first_name": "Bot"}
    go(door.handle_update(upd))
    conv = door.registry.get(f"tg:{OWNER}:88")
    assert conv.title == "Made by me" and conv.title_confirmed


def test_implicit_topic_gets_a_title(tmp_path):
    door, api = make_door(tmp_path)

    async def scenario():
        await door.handle_update(dm(thread=90, forum_topic_created={"name": "New Chat", "icon_color": 1,
                                                                     "is_name_implicit": True}))
        await door.handle_update(dm("Plan the database migration for next week please", thread=90, mid=52))
        await settle(door)

    go(scenario())
    edit = api.sent("editForumTopic")[0]
    assert edit["message_thread_id"] == 90 and edit["name"].startswith("Plan the database migration")
    conv = door.registry.get(f"tg:{OWNER}:90")
    assert conv.title == edit["name"] and conv.title_confirmed and not conv.name_implicit


# ------------------------------------------------------------------ deletion detection
def test_probe_alive_and_deleted(tmp_path):
    door, api = make_door(tmp_path)
    reg = door.registry
    reg.upsert(f"tg:{OWNER}:10", title="Alive dm", title_confirmed=True, kind="dm")
    reg.upsert(f"tg:{GROUP}:11", title="Alive group", title_confirmed=True, kind="group")
    reg.upsert(f"tg:{GROUP}:12", title="Gone", title_confirmed=True, kind="group")
    reg.upsert(f"tg:{OWNER}:13", title="Unconfirmed", title_confirmed=False, kind="dm")
    reg.upsert(f"tg:{OWNER}:main", title="main", kind="dm")

    def edit(params):
        if params["message_thread_id"] == 11:
            return TgError("Bad Request: TOPIC_NOT_MODIFIED", 400)
        if params["message_thread_id"] == 12:
            return TgError("Bad Request: TOPIC_ID_INVALID", 400)
        return True

    api.fail["editForumTopic"] = edit
    results = go(door.topics.probe_due(interval=0, pause=0))
    assert results == {f"tg:{OWNER}:10": "alive", f"tg:{GROUP}:11": "alive", f"tg:{GROUP}:12": "deleted"}
    probed = {p["message_thread_id"]: p["name"] for p in api.sent("editForumTopic")}
    assert probed == {10: "Alive dm", 11: "Alive group", 12: "Gone"}    # stored names only, main never
    assert reg.get(f"tg:{GROUP}:12").state == "deleted"
    notice = api.sent()[-1]
    assert notice["chat_id"] == GROUP and "message_thread_id" not in notice and "Gone" in notice["text"]
    assert (tmp_path / "state" / "archive.jsonl").exists()
    assert go(door.topics.probe_due(interval=0, pause=0)) == {f"tg:{OWNER}:10": "alive",
                                                              f"tg:{GROUP}:11": "alive"}


def test_send_error_marks_topic_deleted(tmp_path):
    door, api = make_door(tmp_path)

    def send(params):
        if params.get("message_thread_id") == 33:
            return TgError("Bad Request: message thread not found", 400)
        return {"message_id": 1}

    api.fail["sendMessage"] = send

    async def scenario():
        await door.handle_update(dm("hello", thread=33, topic_name="Doomed"))
        await settle(door)

    go(scenario())
    assert door.registry.get(f"tg:{OWNER}:33").state == "deleted"
    assert any("Doomed" in p["text"] and "message_thread_id" not in p for p in api.sent())


# ------------------------------------------------------------------ stop button and commands
def test_stopped_message_generation_cancels_turn(tmp_path):
    runner = FakeRunner(wait_cancel=True)
    door, api = make_door(tmp_path, runner)

    async def scenario():
        await door.handle_update(dm("long task", thread=40, topic_name="Work"))
        await asyncio.wait_for(runner.started.wait(), 5)
        draft_id = door.running[f"tg:{OWNER}:40"].live.draft_id
        await door.handle_update({"update_id": 9, "stopped_message_generation": {
            "chat": {"id": OWNER, "type": "private"}, "message_thread_id": 40, "draft_id": draft_id}})
        await settle(door)

    go(scenario())
    assert any(t.startswith("⏹ Stopped") for t in api.texts())


def test_stop_command_in_group(tmp_path):
    runner = FakeRunner(wait_cancel=True)
    door, api = make_door(tmp_path, runner)

    async def scenario():
        await door.handle_update(grp("long task", thread=7))
        await asyncio.wait_for(runner.started.wait(), 5)
        await door.handle_update(grp("/stop@test_archi_bot", thread=7, mid=70))
        await settle(door)

    go(scenario())
    assert any(t.startswith("⏹ Stopped") for t in api.texts())
    assert api.sent("sendMessageDraft") == []      # drafts are private-chat only


def test_commands(tmp_path):
    door, api = make_door(tmp_path)
    key = f"tg:{OWNER}:main"

    async def scenario():
        await door.handle_update(dm("hello"))
        await settle(door)
        await door.handle_update(dm("/provider codex", mid=2))
        await settle(door)
        await door.handle_update(dm("/new", mid=3))
        await door.handle_update(dm("/topics", mid=4))
        await door.handle_update(dm("/help", mid=5))
        await door.handle_update(dm("/status", mid=6))
        await door.handle_update(dm("/other@some_other_bot", mid=7))
        await settle(door)

    go(scenario())
    conv = door.registry.get(key)
    texts = api.texts()
    assert conv.sessions == {}
    assert any("New session" in t for t in texts) and any("Topics in this chat" in t for t in texts)
    assert any("/provider" in t for t in texts)
    if conv.provider == "codex":
        assert any("codex" in t for t in texts)
    else:   # codex is not installed on this machine: the switch is refused, not faked
        assert any("not installed" in t for t in texts)


def test_parse_model_args():
    assert parse_model_args(["opus-5.5", "high"], "claude") == ("claude-opus-5-5", "high", None)
    assert parse_model_args(["sonnet"], "claude") == ("sonnet", None, None)
    assert parse_model_args(["max"], "claude") == (None, "max", None)
    assert parse_model_args(["default"], "claude") == ("default", None, None)
    assert parse_model_args(["high"], "codex")[2]


# ------------------------------------------------------------------ voice key flow
@pytest.mark.parametrize("lang,expected", [("ru", "Дай мне ключ, я внесу его в конфиг"),
                                           ("uk", "Дай мне ключ, я внесу его в конфиг"),
                                           ("en", "Send me an OpenAI API key and I will add it to the config")])
def test_voice_without_key_asks_for_it(tmp_path, lang, expected):
    door, api = make_door(tmp_path)

    async def scenario():
        await door.handle_update(dm(voice={"file_id": "v1", "file_unique_id": "u", "duration": 3}, lang=lang))
        await settle(door)

    go(scenario())
    assert api.texts() == [expected]


def test_key_message_is_stored_and_deleted(tmp_path, monkeypatch):
    door, api = make_door(tmp_path)

    async def fake_check(key):
        return True

    monkeypatch.setattr("core.voice.check_key", fake_check)
    key = "sk-proj-" + "A1b2" * 10

    async def scenario():
        await door.handle_update(dm(key, mid=77, lang="ru"))
        await settle(door)

    go(scenario())
    assert door.cfg.get("OPENAI_API_KEY") == key
    assert api.sent("deleteMessage")[0]["message_id"] == 77
    assert "Пришли голосовое ещё раз" in api.texts()[-1]
    assert key not in json.dumps(api.calls[-1])


def test_restart_notice_for_interrupted_turns(tmp_path):
    door, api = make_door(tmp_path)
    dead = 2 ** 22 + 12345                      # beyond pid_max on any normal system: never alive
    running.start("telegram", f"tg:{OWNER}:5", tmp_path, pid=dead)
    running.start("telegram", f"tg:{OWNER}:6", tmp_path)          # a live process (this one): not ours to report
    go(door._report_interrupted())
    assert len(api.sent()) == 1
    assert api.sent()[0]["message_thread_id"] == 5 and "interrupted" in api.sent()[0]["text"]
    assert [m["key"] for m in running.active(tmp_path)] == [f"tg:{OWNER}:6"]
    assert running.leftovers("telegram", tmp_path) == []


def test_run_marker_covers_delivery_and_survives_shutdown(tmp_path):
    seen = []

    class Api(FakeApi):
        async def call(self, method, **params):
            if method == "sendMessage" and "Hi from the agent" in params.get("text", ""):
                seen.append([m["key"] for m in running.active(tmp_path)])
            return await super().call(method, **params)

    door, _ = make_door(tmp_path)
    door.api = Api()
    door.topics.api = door.api

    async def scenario():
        await door.handle_update(dm("hello"))
        await settle(door)

    go(scenario())
    assert seen == [[f"tg:{OWNER}:main"]]        # still "running" while the answer is being sent
    assert running.active(tmp_path) == []          # and gone afterwards

    runner = FakeRunner(wait_cancel=True)
    door2, api2 = make_door(tmp_path / "b", runner)

    async def shutdown():
        await door2.handle_update(dm("long task"))
        await asyncio.wait_for(runner.started.wait(), 5)
        door2.stopping = True
        for t in list(door2.running.values()):
            t.cancel.set()
        await settle(door2)

    go(shutdown())
    assert not any("Stopped" in t for t in api2.texts())        # nothing sent while shutting down
    assert [m["key"] for m in running.active(tmp_path / "b")] == [f"tg:{OWNER}:main"]


# ------------------------------------------------------------------ first run
def test_pairing_starts_the_first_run_turn(tmp_path):
    runner = FakeRunner("Hello, I run on this server.")
    door, api = make_door(tmp_path, runner, owner=None, PAIRING_CODE="123456")

    async def scenario():
        await door.handle_update(dm("/start 123456", lang="ru"))
        await settle(door)

    go(scenario())
    assert door.cfg.owner_id() == OWNER
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert "First run" in call["prompt"] and "ru" in call["prompt"]
    assert call["env"]["ARCHI_CONVERSATION"] == f"tg:{OWNER}:main"
    assert "without a name yet" in call["system_prompt"]
    assert api.texts()[0].startswith("✅ Paired") and api.texts()[-1] == "Hello, I run on this server."


def test_pairing_again_with_a_name_greets_instead(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner, owner=None, PAIRING_CODE="654321", AGENT_NAME="Max")

    async def scenario():
        await door.handle_update(dm("654321"))
        await settle(door)

    go(scenario())
    assert "paired with you again" in runner.calls[0]["prompt"]
    assert "You are Max" in runner.calls[0]["system_prompt"]


def test_too_many_wrong_codes_revoke_the_pairing_code(tmp_path):
    door, api = make_door(tmp_path, owner=None, PAIRING_CODE="111111")

    async def scenario():
        await door.handle_update(dm("/start", uid=STRANGER))           # not a guess
        for i in range(PAIR_MAX_FAILURES):
            await door.handle_update(dm(f"{200000 + i}", uid=STRANGER, mid=100 + i))
        await door.handle_update(dm("111111", uid=STRANGER, mid=999))  # too late: revoked
        await settle(door)

    go(scenario())
    assert door.cfg.owner_id() is None and door.cfg.get("PAIRING_CODE") == "" and api.calls == []


# ------------------------------------------------------------------ an answer never vanishes
def test_undelivered_answer_is_kept_and_sent_when_telegram_answers_again(tmp_path, monkeypatch):
    from core import afterturn
    calls = []
    monkeypatch.setattr(afterturn, "turn_finished", lambda door, key, **kw: calls.append(kw["answer"]) or None)
    door, api = make_door(tmp_path, FakeRunner("Here is the report."))
    door.send_retry_s = (0, 0)
    tries = []

    def send(params):
        if "Here is the report." in params.get("text", ""):
            tries.append(1)
            return TgError("network error: Cannot connect to host api.telegram.org", 0)
        return {"message_id": 1}

    api.fail["sendMessage"] = send

    async def scenario():
        await door.handle_update(dm("the report please"))
        await settle(door)

    go(scenario())
    assert len(tries) == 3                                       # the first try and two retries
    kept = list((tmp_path / "state" / "outbox").glob("telegram--*.json"))
    assert len(kept) == 1 and (kept[0].stat().st_mode & 0o777) == 0o600 and calls == []
    assert running.active(tmp_path) == []
    api.fail.pop("sendMessage")
    door2, api2 = make_door(tmp_path)                            # the next start delivers it
    go(door2.flush_outbox())
    texts = api2.texts()
    assert texts[0].startswith("<i>(Delivered late: this answer was ready at") and texts[1] == "Here is the report."
    assert not list((tmp_path / "state" / "outbox").glob("*.json")) and calls == ["Here is the report."]


def test_command_reply_failure_is_logged_not_raised(tmp_path):
    logs = []
    door, api = make_door(tmp_path)
    door.log = lambda *a: logs.append(" ".join(map(str, a)))
    door.send_retry_s = (0,)
    api.fail["sendMessage"] = TgError("network error: timeout", 0)

    async def scenario():
        await door.handle_update(dm("/help"))
        await settle(door)

    go(scenario())
    assert any("message not delivered" in line for line in logs)
    assert not (tmp_path / "state" / "outbox").exists()          # a command's reply is not an answer to keep


def test_group_status_footer_says_the_answer_is_late():
    from core.status import TurnStatus
    from doors.telegram.live import LiveStatus
    api = FakeApi()
    st = TurnStatus()
    live = LiveStatus(api, GROUP, 7, "group", st, log=lambda *a: None)
    live.msg_id = 42
    st.finish(TurnResult("x", "s", True))
    go(live.done(True, "queued"))
    edit = api.sent("editMessageText")[0]
    assert edit["message_id"] == 42 and "could not be delivered yet" in edit["text"]
    go(live.done(True, "delivered"))
    assert api.sent("deleteMessage")[0]["message_id"] == 42
