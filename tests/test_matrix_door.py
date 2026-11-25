"""Matrix door behaviour with a fake client API and a fake engine. All ids are made up."""
import asyncio
import json
import time
from pathlib import Path

import pytest

from core import afterturn, jobs, running
from core.config import Config
from core.conversations import Registry
from core.door import KEY_REQUEST, KEY_REQUEST_EN
from core.engine import Event, TurnResult
from core.status import TurnStatus
from doors.matrix import media
from doors.matrix.api import MxError
from doors.matrix.bot import ENCRYPTED_NOTICE, GREETING, Door, strip_reply_fallback
from doors.matrix.live import LiveStatus
from test_telegram_door import FakeRunner, go, settle

OWNER, BOT, STRANGER = "@owner:example.org", "@archi:example.org", "@stranger:example.org"
ROOM, ROOM2, ROOM3 = "!room1:example.org", "!room2:example.org", "!room3:example.org"
V12 = "!v12room"                       # room version 12 ids have no server part
KEY, KEY2, KEY3 = f"mx:{ROOM}", f"mx:{ROOM2}", f"mx:{ROOM3}"
DEAD_PID = 2 ** 22 + 777
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
       + b"\x08\x02\x00\x00\x00" + b"\x00" * 40)


class FakeMx:
    def __init__(self, user_id=BOT):
        self.user_id = user_id
        self.sent = []          # (room, event type, content)
        self.redactions = []    # (room, event id)
        self.joined, self.left, self.uploads, self.typings = [], [], [], []
        self.fail = {}
        self.media = {}
        self.syncs = []
        self.sync_calls = []
        self.limit = None
        self.n = 0

    def _maybe_fail(self, method, *args):
        rule = self.fail.get(method)
        if rule is not None:
            r = rule(*args) if callable(rule) else rule
            if isinstance(r, Exception):
                raise r

    async def whoami(self):
        return {"user_id": self.user_id, "device_id": "DEVICE1"}

    async def sync(self, since, timeout_ms, sync_filter=None):
        self.sync_calls.append((since, timeout_ms))
        return self.syncs.pop(0) if self.syncs else {"next_batch": since or "s0"}

    async def send(self, room_id, event_type, content):
        self._maybe_fail("send", room_id, event_type, content)
        self.n += 1
        self.sent.append((room_id, event_type, content))
        return f"$ev{self.n}"

    async def redact(self, room_id, event_id, reason=None):
        self._maybe_fail("redact", room_id, event_id)
        self.redactions.append((room_id, event_id))
        self.n += 1
        return f"$red{self.n}"

    async def typing(self, room_id, on=True, timeout_ms=30000):
        self.typings.append((room_id, on))

    async def join(self, room_id):
        self._maybe_fail("join", room_id)
        self.joined.append(room_id)
        return room_id

    async def leave(self, room_id):
        self.left.append(room_id)

    async def upload(self, data, content_type, filename):
        self.n += 1
        self.uploads.append((content_type, filename, len(data)))
        return f"mxc://example.org/up{self.n}"

    async def download(self, mxc):
        if mxc not in self.media:
            raise MxError("M_NOT_FOUND", "no such media", 404)
        return self.media[mxc]

    async def upload_limit(self):
        return self.limit

    async def close(self):
        pass

    # ---- views
    def messages(self, room=None, msgtype=None):
        out = []
        for r, t, c in self.sent:
            if t != "m.room.message" or (room and r != room) or "m.relates_to" in c:
                continue
            if msgtype and c.get("msgtype") != msgtype:
                continue
            out.append(c)
        return out

    def texts(self, room=None):
        return [c["body"] for c in self.messages(room) if c.get("msgtype") in ("m.text", "m.notice")]

    def reactions(self):
        return [(c["m.relates_to"]["event_id"], c["m.relates_to"]["key"]) for _r, t, c in self.sent
                if t == "m.reaction"]

    def edits(self):
        return [c for _r, t, c in self.sent if t == "m.room.message"
                and (c.get("m.relates_to") or {}).get("rel_type") == "m.replace"]


def make_door(tmp_path, runner=None, owner=OWNER, **values):
    cfg = Config(tmp_path)
    vals = {"MATRIX_HOMESERVER": "https://matrix.example.org", "MATRIX_USER_ID": BOT, "MATRIX_ACCESS_TOKEN": "tok",
            "MATRIX_OWNER_ID": owner or ""}
    vals.update(values)
    cfg.update(vals)
    api = FakeMx()
    door = Door(api, cfg, Registry(tmp_path / "state"), runner=runner or FakeRunner(), log=lambda *a: None,
                debounce_s=0, album_s=0)
    return door, api


_n = [0]


def ev(etype, content, sender=OWNER, state_key=None, ts=None, eid=None):
    _n[0] += 1
    e = {"type": etype, "content": content, "sender": sender, "event_id": eid or f"$in{_n[0]}",
         "origin_server_ts": int((ts if ts is not None else time.time()) * 1000)}
    if state_key is not None:
        e["state_key"] = state_key
    return e


def msg(body, sender=OWNER, **kw):
    return ev("m.room.message", {"msgtype": "m.text", "body": body}, sender=sender, **kw)


def media_msg(msgtype, sender=OWNER, **content):
    return ev("m.room.message", {"msgtype": msgtype, **content}, sender=sender)


def name_ev(name, sender=OWNER):
    return ev("m.room.name", {"name": name}, sender=sender, state_key="")


def member(user, membership, sender=None):
    return ev("m.room.member", {"membership": membership}, sender=sender or user, state_key=user)


def joined(room, *timeline, state=(), members=2, batch="s1"):
    return {"next_batch": batch, "rooms": {"join": {room: {
        "state": {"events": list(state)}, "timeline": {"events": list(timeline), "limited": False},
        "summary": {"m.joined_member_count": members}}}}}


def invited(room, inviter, name=None, encrypted=False):
    events = [ev("m.room.create", {"room_version": "12"}, sender=inviter, state_key="")]
    if name:
        events.append(name_ev(name, sender=inviter))
    if encrypted:
        events.append(ev("m.room.encryption", {"algorithm": "m.megolm.v1.aes-sha2"}, sender=inviter, state_key=""))
    events.append(member(BOT, "invite", sender=inviter))
    return {"next_batch": "s1", "rooms": {"invite": {room: {"invite_state": {"events": events}}}}}


def run_sync(door, *syncs):
    async def scenario():
        for s in syncs:
            await door.handle_sync(s)
            await settle(door)
    go(scenario())


# ------------------------------------------------------------------ owner only, invites
def test_strangers_and_own_messages_are_ignored_silently(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    run_sync(door, joined(ROOM, msg("hello", sender=STRANGER), msg("echo", sender=BOT),
                          ev("m.room.message", {"msgtype": "m.notice", "body": "a bot"}, sender=OWNER)))
    assert runner.calls == [] and api.sent == []
    assert door.registry.get(KEY).state == "open"           # the room itself is known


def test_nobody_is_answered_without_an_owner(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner, owner=None)
    run_sync(door, invited(ROOM, OWNER), joined(ROOM2, msg("hello")))
    assert runner.calls == [] and api.joined == [] and api.left == [ROOM] and api.sent == []


def test_owner_invite_is_accepted_and_registered(tmp_path):
    door, api = make_door(tmp_path)
    run_sync(door, invited(ROOM, OWNER, name="Ideas"))
    conv = door.registry.get(KEY)
    assert api.joined == [ROOM] and conv.title == "Ideas" and conv.title_confirmed and conv.state == "open"
    notice = api.messages(ROOM)[0]
    assert notice["msgtype"] == "m.notice" and notice["body"] == GREETING


def test_stranger_invite_is_rejected_silently(tmp_path):
    door, api = make_door(tmp_path)
    run_sync(door, invited(ROOM, STRANGER, name="Spam"))
    assert api.joined == [] and api.left == [ROOM] and api.sent == []
    assert door.registry.get(KEY) is None and door.registry.chat(ROOM) == {}
    run_sync(door, {"next_batch": "s2", "rooms": {"leave": {ROOM: {"timeline": {"events": [
        member(BOT, "leave", sender=BOT)]}}}}})
    assert door.registry.get(KEY) is None                     # the rejection leaves no trace


def test_encrypted_invite_is_explained_and_left(tmp_path):
    door, api = make_door(tmp_path)
    run_sync(door, invited(ROOM, OWNER, name="Secret", encrypted=True))
    assert api.joined == [ROOM] and api.left == [ROOM]
    assert api.texts(ROOM) == [ENCRYPTED_NOTICE] and api.messages(ROOM)[0]["msgtype"] == "m.text"
    assert door.registry.get(KEY) is None
    assert door.registry.chat(ROOM)["encrypted"] is True
    run_sync(door, joined(ROOM, msg("can you read me?"), state=[name_ev("Secret")]))   # a late join entry
    assert door.registry.get(KEY) is None and len(api.sent) == 1


def test_encryption_switched_on_in_a_room(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    run_sync(door, joined(ROOM, msg("hello"), state=[name_ev("Work")]))
    assert len(runner.calls) == 1
    run_sync(door, joined(ROOM, ev("m.room.encryption", {"algorithm": "m.megolm.v1.aes-sha2"}, state_key=""),
                          ev("m.room.encrypted", {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "x"})))
    assert api.left == [ROOM] and ENCRYPTED_NOTICE in api.texts(ROOM)
    assert door.registry.get(KEY).state == "deleted"
    archived = [json.loads(x) for x in (tmp_path / "state" / "archive.jsonl").read_text().splitlines()]
    assert archived[-1]["key"] == KEY and archived[-1]["reason"] == "encrypted"
    assert len(runner.calls) == 1


# ------------------------------------------------------------------ room lifecycle
def test_rename_owner_leave_and_kick(tmp_path):
    door, api = make_door(tmp_path)
    run_sync(door, joined(ROOM, state=[name_ev("First")]), joined(ROOM2, state=[name_ev("Second")]))
    assert door.registry.get(KEY).title == "First"
    run_sync(door, joined(ROOM, name_ev("Renamed")))
    conv = door.registry.get(KEY)
    assert conv.title == "Renamed" and conv.title_confirmed
    run_sync(door, joined(ROOM, member(OWNER, "leave")))
    assert door.registry.get(KEY).state == "deleted" and api.left == [ROOM]
    run_sync(door, {"next_batch": "s3", "rooms": {"leave": {ROOM2: {"timeline": {"events": [
        member(BOT, "leave", sender=OWNER)]}}}}})
    assert door.registry.get(KEY2).state == "deleted"
    reasons = [json.loads(x)["reason"] for x in (tmp_path / "state" / "archive.jsonl").read_text().splitlines()]
    assert reasons == ["owner left", "kicked"]
    assert door.registry.all(door="mx") == []


def test_room_the_owner_already_left_is_not_kept(tmp_path):
    door, api = make_door(tmp_path)
    run_sync(door, joined(ROOM, state=[name_ev("Old"), member(OWNER, "leave")]))
    assert door.registry.get(KEY) is None and api.left == [ROOM] and api.sent == []


def test_room_without_server_part_and_parallel_rooms(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    run_sync(door, joined(V12, msg("one")), joined(ROOM, msg("two")))
    assert sorted(c["env"]["ARCHI_CONVERSATION"] for c in runner.calls) == sorted([f"mx:{V12}", KEY])
    assert {c["env"]["ARCHI_ROOM_ID"] for c in runner.calls} == {V12, ROOM}
    assert door.registry.get(f"mx:{V12}").chat_id == V12


# ------------------------------------------------------------------ turns
def test_owner_message_runs_a_turn(tmp_path):
    runner = FakeRunner("**Done**")
    door, api = make_door(tmp_path, runner)
    first = msg("check the logs", eid="$own1")
    run_sync(door, joined(ROOM, first, state=[name_ev("Server work")]))
    call = runner.calls[0]
    env = call["env"]
    assert env["ARCHI_DOOR"] == "matrix" and env["ARCHI_CONVERSATION"] == KEY and env["ARCHI_ROOM_ID"] == ROOM
    assert env["ARCHI_TOPIC_TITLE"] == "Server work"
    assert "Server work" in call["system_prompt"] and 'archi mx room create "<name>"' in call["system_prompt"]
    assert call["prompt"].startswith("[") and call["prompt"].endswith("check the logs")
    answer = api.messages(ROOM)[-1]
    assert answer == {"msgtype": "m.text", "body": "**Done**", "format": "org.matrix.custom.html",
                      "formatted_body": "<p><b>Done</b></p>"}
    assert api.reactions() == [("$own1", "⏳"), ("$own1", "✅")]
    assert len(api.redactions) == 1                          # the ⏳ was taken back
    assert api.edits() == []                                 # a quick answer gets no status notice
    assert door.registry.get(KEY).sessions == {"claude": "sess-1"}
    assert running.active(tmp_path) == []


def test_reply_fallback_and_edits(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    reply = msg("> <@owner:example.org> earlier\n> text\n\nthe actual reply")
    edit = ev("m.room.message", {"msgtype": "m.text", "body": "* fixed", "m.new_content": {"body": "fixed"},
                                 "m.relates_to": {"rel_type": "m.replace", "event_id": "$x"}})
    run_sync(door, joined(ROOM, reply, edit))
    assert len(runner.calls) == 1 and runner.calls[0]["prompt"].endswith("\nthe actual reply")
    assert strip_reply_fallback("plain") == "plain"


def test_long_answers_are_split(tmp_path):
    runner = FakeRunner(("paragraph " * 400 + "\n\n") * 8)
    door, api = make_door(tmp_path, runner)
    run_sync(door, joined(ROOM, msg("write a lot")))
    parts = api.messages(ROOM, "m.text")
    assert len(parts) >= 2
    for p in parts:
        assert len(json.dumps(p).encode()) < 60000 and p["formatted_body"].startswith("<p>")


def test_old_messages_are_not_answered(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    run_sync(door, joined(ROOM, msg("from two days ago", ts=time.time() - 48 * 3600)))
    assert runner.calls == []


def test_first_sync_skips_history_and_the_token_survives_a_restart(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    api.syncs = [joined(ROOM, msg("history"), state=[name_ev("Old")], batch="s1"),
                 joined(ROOM, msg("new message"), batch="s2")]

    async def scenario():
        await door.startup()
        assert door.since is None
        await door.sync_once()
        await door.sync_once()
        await settle(door)

    go(scenario())
    assert [c["prompt"].split("\n", 1)[1] for c in runner.calls] == ["new message"]
    assert api.sync_calls == [(None, 0), ("s1", 30000)]
    assert door.registry.get(KEY).title == "Old"
    saved = json.loads((tmp_path / "state" / "matrix.json").read_text())
    assert saved["since"] == "s2" and saved["user_id"] == BOT
    door2, _ = make_door(tmp_path)
    go(door2.startup())
    assert door2.since == "s2"                               # nothing is replayed after a restart
    door3, api3 = make_door(tmp_path, MATRIX_USER_ID="@other:example.org")
    api3.user_id = "@other:example.org"
    go(door3.startup())
    assert door3.since is None                               # another account's token is not used


def test_token_of_another_account_stops_the_door(tmp_path):
    from doors.matrix.bot import DoorFatal
    door, api = make_door(tmp_path)
    api.user_id = "@somebody:example.org"
    with pytest.raises(DoorFatal):
        go(door.startup())


def test_sync_loop_survives_errors_and_stops_on_a_rejected_token(tmp_path):
    from doors.matrix.bot import DoorFatal
    door, api = make_door(tmp_path)
    calls = []

    async def sync(since, timeout_ms, sync_filter=None):
        calls.append(since)
        if len(calls) == 1:
            raise MxError("M_LIMIT_EXCEEDED", "slow down", 429, retry_after_ms=10)
        raise MxError("M_UNKNOWN_TOKEN", "token revoked", 401)

    api.sync = sync
    with pytest.raises(DoorFatal):
        go(door.sync_loop())
    assert len(calls) == 2


# ------------------------------------------------------------------ commands
def test_commands_answer_and_use_the_bang_prefix(tmp_path):
    door, api = make_door(tmp_path)
    run_sync(door, joined(ROOM, msg("hello")))
    run_sync(door, joined(ROOM, msg("!new"), msg("/help"), msg("!status"), msg("!jobs"), msg("!model"),
                          msg("!provider")))
    texts = api.texts(ROOM)
    assert any("New session for this room" in t for t in texts)
    assert any("every room is a separate conversation" in t and "!jobs" in t for t in texts)
    assert any("agent: `claude`" in t for t in texts)
    assert any(t.startswith("No jobs") for t in texts)
    assert any("Model in this room" in t and "`!model sonnet`" in t for t in texts)
    assert any("Agent in this room" in t and "`!provider codex`" in t for t in texts)
    assert door.registry.get(KEY).sessions == {}


def test_unknown_bang_text_is_a_normal_message(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    run_sync(door, joined(ROOM, msg("!important: restart nginx")))
    assert len(runner.calls) == 1 and runner.calls[0]["prompt"].endswith("!important: restart nginx")


def test_stop_command_cancels_the_running_turn(tmp_path):
    runner = FakeRunner(wait_cancel=True)
    door, api = make_door(tmp_path, runner)

    async def scenario():
        await door.handle_sync(joined(ROOM, msg("long task", eid="$long")))
        await asyncio.wait_for(runner.started.wait(), 5)
        await door.handle_sync(joined(ROOM, msg("!stop")))
        await settle(door)

    go(scenario())
    assert any(t.startswith("⏹ Stopped after") for t in api.texts(ROOM))
    assert api.reactions()[-1] == ("$long", "⚠️")


def test_jobs_command_lists_the_jobs_of_this_room(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(jobs, "status_lines", lambda home=None, conversation=None: seen.append(conversation)
                        or ["▶ j1 · running · backup"])
    door, api = make_door(tmp_path)
    run_sync(door, joined(ROOM, msg("!jobs")))
    assert seen == [KEY] and "j1 · running · backup" in api.texts(ROOM)[-1]


def test_learning_hook_after_delivery_and_learn_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(afterturn, "turn_finished", lambda door, key, **kw: calls.append((door, key, kw)) or None)
    monkeypatch.setattr(afterturn, "review_now", lambda door, key, **kw: f"review started for {key}")
    door, api = make_door(tmp_path)
    run_sync(door, joined(ROOM, msg("hello")))
    run_sync(door, joined(ROOM, msg("!learn")))
    assert len(calls) == 1
    d, key, kw = calls[0]
    assert (d, key) == ("matrix", KEY) and kw["prompt"] == "hello" and kw["answer"] == "Hi from the agent"
    assert kw["session_id"] == "sess-1" and kw["steps"] == ["Reading notes.md"] and kw["tool_calls"] == 1
    assert api.texts(ROOM)[-1] == f"review started for {KEY}"


# ------------------------------------------------------------------ media in
def test_images_become_one_turn_with_image_input(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    door.album_s = 0.2
    api.media["mxc://example.org/a"] = PNG
    api.media["mxc://example.org/b"] = PNG
    seen = []
    original = runner.__call__

    async def spy(*a, **kw):
        seen.extend(Path(p).exists() and Path(p).suffix for p in kw["images"])
        return await original(*a, **kw)

    door.runner = spy
    run_sync(door, joined(ROOM, media_msg("m.image", body="what is on these?", filename="a.png",
                                          url="mxc://example.org/a", info={"mimetype": "image/png", "size": 70}),
                          media_msg("m.image", body="b.png", url="mxc://example.org/b",
                                    info={"mimetype": "image/png", "size": 70})))
    assert len(runner.calls) == 1 and len(runner.calls[0]["images"]) == 2 and seen == [".png", ".png"]
    assert runner.calls[0]["prompt"].endswith("what is on these?")
    assert not any(Path(p).exists() for p in runner.calls[0]["images"])       # temp files are removed


def test_file_is_saved_to_the_inbox_and_never_run(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    api.media["mxc://example.org/f"] = b"#!/bin/sh\necho hi\n"
    run_sync(door, joined(ROOM, media_msg("m.file", body="look at it", filename="../../evil script.sh",
                                          url="mxc://example.org/f", info={"mimetype": "text/x-sh", "size": 19})))
    saved = list((tmp_path / ".inbox").iterdir())
    assert len(saved) == 1 and saved[0].name.endswith("_evil_script.sh") and saved[0].read_bytes().startswith(b"#!")
    prompt = runner.calls[0]["prompt"]
    assert str(saved[0]) in prompt and "Do NOT execute it" in prompt and "look at it" in prompt


def test_forwarded_encrypted_attachment_is_decrypted(tmp_path):
    from nio.crypto.attachments import encrypt_attachment
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    cipher, keys = encrypt_attachment(b"secret notes")
    api.media["mxc://example.org/enc"] = cipher
    run_sync(door, joined(ROOM, media_msg("m.file", body="notes.txt", file={
        "url": "mxc://example.org/enc", "key": keys["key"], "iv": keys["iv"], "hashes": keys["hashes"], "v": "v2"})))
    saved = list((tmp_path / ".inbox").iterdir())
    assert saved[0].read_bytes() == b"secret notes"


@pytest.mark.parametrize("before,expected", [(None, KEY_REQUEST_EN), ("привет", KEY_REQUEST["ru"])])
def test_voice_without_key_asks_for_it(tmp_path, before, expected):
    door, api = make_door(tmp_path)
    if before:
        run_sync(door, joined(ROOM, msg(before)))
    api.media["mxc://example.org/v"] = b"OggS"
    run_sync(door, joined(ROOM, media_msg("m.audio", body="Voice message", url="mxc://example.org/v",
                                          info={"mimetype": "audio/ogg"}, **{"org.matrix.msc3245.voice": {}})))
    assert api.texts(ROOM)[-1] == expected


def test_voice_with_key_is_transcribed(tmp_path, monkeypatch):
    async def fake_transcribe(path, cfg=None, language=None):
        assert Path(path).suffix == ".ogg" and Path(path).read_bytes() == b"OggS"
        return "turn on the lights"

    monkeypatch.setattr("core.voice.transcribe", fake_transcribe)
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner, OPENAI_API_KEY="sk-proj-" + "x" * 30)
    api.media["mxc://example.org/v"] = b"OggS"
    run_sync(door, joined(ROOM, media_msg("m.audio", body="Voice message", url="mxc://example.org/v",
                                          info={"mimetype": "audio/ogg"}, **{"org.matrix.msc3245.voice": {}})))
    assert runner.calls[0]["prompt"].endswith("[Voice message, transcribed automatically]\nturn on the lights")


@pytest.mark.parametrize("redact_ok", [True, False])
def test_pasted_key_is_stored_and_redacted(tmp_path, monkeypatch, redact_ok):
    async def fake_check(key):
        return True

    monkeypatch.setattr("core.voice.check_key", fake_check)
    door, api = make_door(tmp_path)
    if not redact_ok:
        api.fail["redact"] = MxError("M_FORBIDDEN", "User cannot redact this event.", 403)
    secret = "sk-proj-" + "Q7w" * 12
    run_sync(door, joined(ROOM, msg(secret, eid="$key")))
    assert door.cfg.get("OPENAI_API_KEY") == secret
    assert (api.redactions == [(ROOM, "$key")]) is redact_ok
    assert api.texts(ROOM)[-1].startswith("✅ Key saved")
    assert ("Please delete your message" in api.texts(ROOM)[-1]) is not redact_ok
    assert secret not in json.dumps(api.sent)


# ------------------------------------------------------------------ media out
def test_answer_markers_send_files_and_images(tmp_path):
    png, txt = tmp_path / "chart.png", tmp_path / "report.txt"
    png.write_bytes(PNG)
    txt.write_text("r")
    runner = FakeRunner(f"Here it is.\n[FILE {png}]\n[FILE {txt}]\n[FILE /nonexistent/x.txt]")
    door, api = make_door(tmp_path, runner)
    run_sync(door, joined(ROOM, msg("send the report")))
    images, files = api.messages(ROOM, "m.image"), api.messages(ROOM, "m.file")
    assert images[0]["info"] == {"mimetype": "image/png", "size": len(PNG), "w": 640, "h": 480}
    assert images[0]["url"].startswith("mxc://") and files[0]["body"] == "report.txt"
    assert [u[0] for u in api.uploads] == ["image/png", "text/plain"]
    assert any("File not found: /nonexistent/x.txt" in t for t in api.texts(ROOM))
    assert api.texts(ROOM)[0] == "Here it is."


def test_upload_limit_is_respected(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 2048)
    door, api = make_door(tmp_path, FakeRunner(f"[FILE {big}]"))
    api.limit = 1024
    run_sync(door, joined(ROOM, msg("send it")))
    assert api.uploads == [] and "accepts at most" in api.texts(ROOM)[-1]


def test_voice_answer_becomes_a_voice_message(tmp_path, monkeypatch):
    ogg = tmp_path / "tts.ogg"

    async def fake_synthesize(text, cfg=None, speed=1.0):
        ogg.write_bytes(b"OggS-tts")
        return ogg

    async def fake_duration(path):
        return 1.5

    monkeypatch.setattr("core.voice.synthesize", fake_synthesize)
    monkeypatch.setattr("core.voice.duration_s", fake_duration)
    runner = FakeRunner('Sure.\n[VOICE lang="en"]hello there[/VOICE]')
    door, api = make_door(tmp_path, runner, OPENAI_API_KEY="sk-proj-" + "y" * 30)
    run_sync(door, joined(ROOM, msg("say hi")))
    audio = api.messages(ROOM, "m.audio")[0]
    assert "org.matrix.msc3245.voice" in audio and audio["info"]["duration"] == 1500
    assert audio["url"].startswith("mxc://") and api.uploads[-1][0] == "audio/ogg" and not ogg.exists()

    door2, api2 = make_door(tmp_path / "nokey", FakeRunner('Sure.\n[VOICE lang="en"]hello there[/VOICE]'))
    run_sync(door2, joined(ROOM, msg("say hi")))
    assert api2.texts(ROOM) == ["Sure.", "hello there"]        # no key: the text instead


def test_send_to_a_room_the_bot_left_archives_it(tmp_path):
    door, api = make_door(tmp_path)
    run_sync(door, joined(ROOM, state=[name_ev("Gone soon")]))
    api.fail["send"] = MxError("M_FORBIDDEN", f"User {BOT} not in room {ROOM}", 403)
    assert go(door.send_text(KEY, "hello")) is False
    assert door.registry.get(KEY).state == "deleted"


# ------------------------------------------------------------------ live status
def test_live_status_throttles_edits_and_ends_with_a_footer():
    async def scenario():
        api = FakeMx()
        st = TurnStatus()
        live = LiveStatus(api, ROOM, "$src", st, log=lambda *a: None, delay=0.05, edit_min=0.25, tick=30,
                          typing_every=30)
        await live.start()
        for i in range(30):
            st.feed(Event("step", f"step {i}"))
            live.poke()
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.3)
        st.finish(TurnResult("x", "s", True))
        await live.stop(True)
        return api, live

    api, live = go(scenario())
    notices = api.messages(ROOM, "m.notice")
    edits = api.edits()
    assert len(notices) == 1 and notices[0]["body"].startswith("⏳ Working")
    assert 2 <= len(edits) <= 6                                # 30 steps in 0.9 s, at most one edit per 0.25 s
    assert edits[-1]["m.new_content"]["body"].startswith("✅ Done in") and "30 steps" in edits[-1]["m.new_content"]["body"]
    assert all(e["m.relates_to"]["event_id"] == "$ev2" for e in edits)       # $ev1 is the ⏳ reaction
    assert api.reactions() == [("$src", "⏳"), ("$src", "✅")] and api.typings[-1] == (ROOM, False)


def test_quick_turn_gets_no_status_notice():
    async def scenario():
        api = FakeMx()
        st = TurnStatus()
        live = LiveStatus(api, ROOM, "$src", st, log=lambda *a: None, delay=5)
        await live.start()
        st.feed(Event("step", "Reading a.py"))
        live.poke()
        st.finish(TurnResult("", None, False, "boom"))
        await live.stop(False)
        return api

    api = go(scenario())
    assert api.messages(ROOM, "m.notice") == [] and api.edits() == []
    assert api.reactions() == [("$src", "⏳"), ("$src", "⚠️")]


# ------------------------------------------------------------------ restarts and jobs
def test_restart_notice_for_interrupted_turns(tmp_path):
    door, api = make_door(tmp_path)
    running.start("matrix", KEY, tmp_path, pid=DEAD_PID)
    running.start("matrix", KEY2, tmp_path)                       # a live process (this one): not reported
    running.start("telegram", "tg:1:main", tmp_path, pid=DEAD_PID)  # the other door's: not ours to touch
    go(door._report_interrupted())
    assert len(api.messages()) == 1 and "interrupted" in api.texts(ROOM)[0]
    assert [m["key"] for m in running.active(tmp_path)] == [KEY2]
    assert [m["key"] for m in running.leftovers("telegram", tmp_path)] == ["tg:1:main"]


def test_run_marker_covers_delivery_and_survives_shutdown(tmp_path):
    seen = []
    door, api = make_door(tmp_path)
    original = api.send

    async def send(room_id, event_type, content):
        if content.get("body") == "Hi from the agent":
            seen.append([m["key"] for m in running.active(tmp_path)])
        return await original(room_id, event_type, content)

    api.send = send
    run_sync(door, joined(ROOM, msg("hello")))
    assert seen == [[KEY]] and running.active(tmp_path) == []

    runner = FakeRunner(wait_cancel=True)
    door2, api2 = make_door(tmp_path / "b", runner)

    async def shutdown():
        await door2.handle_sync(joined(ROOM, msg("long task")))
        await asyncio.wait_for(runner.started.wait(), 5)
        door2.stopping = True
        for t in list(door2.running.values()):
            t.cancel.set()
        await settle(door2)

    go(shutdown())
    assert not any("Stopped" in t for t in api2.texts())
    assert [m["key"] for m in running.active(tmp_path / "b")] == [KEY]


def test_finished_jobs_are_reported_by_a_turn(tmp_path, monkeypatch):
    runner = FakeRunner("The backup finished.")
    door, api = make_door(tmp_path, runner)
    door.registry.upsert(KEY, title="Server", state="open", last_turn_at=100.0)
    door.registry.upsert(KEY2, title="Recent", state="open", last_turn_at=200.0)
    door.registry.upsert(KEY3, title="Gone", state="deleted")
    queue = [{"id": "j1", "conversation": KEY, "state": "ok", "label": "backup"},
             {"id": "j2", "conversation": KEY3, "state": "failed", "label": "build"},
             {"id": "j3", "conversation": "mx:!unknown:example.org", "state": "ok", "label": "x"}]
    reported = []
    monkeypatch.setattr(jobs, "finished_unreported", lambda d, home=None: [j for j in queue if j["id"] not in reported]
                        if d == "matrix" else [])
    monkeypatch.setattr(jobs, "mark_reported", lambda job_id, home=None: reported.append(job_id))
    monkeypatch.setattr(jobs, "report_prompt", lambda job: f"[job {job['id']} {job['state']}]")

    async def scenario():
        assert await door.report_jobs() == 2
        await settle(door)
        assert await door.report_jobs() == 0

    go(scenario())
    assert reported == ["j1", "j2", "j3"]
    by_room = {c["env"]["ARCHI_CONVERSATION"]: c["prompt"] for c in runner.calls}
    assert by_room[KEY].endswith("[job j1 ok]") and by_room[KEY2].endswith("[job j2 failed]")   # most recent room
    assert len(runner.calls) == 2


def test_attachment_parsing():
    att = media.attachment({"msgtype": "m.image", "body": "caption here", "filename": "x.heic",
                            "url": "mxc://e/1", "info": {"mimetype": "image/heic", "size": 9}})
    assert att.kind == "file" and att.caption == "caption here" and att.name == "x.heic"
    assert media.attachment({"msgtype": "m.text", "body": "x"}) is None
    assert media.attachment({"msgtype": "m.file", "body": "y", "url": "https://evil"}) is None
    assert media.attachment({"msgtype": "m.audio", "body": "song.mp3", "url": "mxc://e/2"}).kind == "audio"
    assert media.image_size(PNG) == (640, 480) and media.image_size(b"nope") is None
