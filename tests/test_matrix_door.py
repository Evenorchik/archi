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
        self.token = "tok"
        self.members_of = {}       # room -> {user: membership}; default: the owner and the bot joined
        self.txns = []
        self.created = []

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

    async def send(self, room_id, event_type, content, txn=None):
        self.txns.append(txn)
        self._maybe_fail("send", room_id, event_type, content)
        self.n += 1
        self.sent.append((room_id, event_type, content))
        return f"$ev{self.n}"

    def set_token(self, token):
        self.token = token

    async def members(self, room_id):
        self._maybe_fail("members", room_id)
        return dict(self.members_of.get(room_id, {OWNER: "join", BOT: "join"}))

    async def create_room(self, name, invite):
        self._maybe_fail("create_room", name)
        room = f"!made{len(self.created) + 1}:example.org"
        self.created.append((room, name, list(invite)))
        return room

    async def state_event(self, room_id, event_type, state_key=""):
        return None

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
    door.send_retry_s = (0, 0)
    door.join_retry_s = (0.01, 0.01, 0.01)
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
    assert door.registry.get(KEY).state == "deleted" and door.registry.chat(ROOM)["name"] == "Work"
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
    api.members_of[ROOM] = {BOT: "join"}
    run_sync(door, joined(ROOM, state=[name_ev("Old"), member(OWNER, "leave")]))
    assert door.registry.get(KEY) is None and api.left == [ROOM] and api.sent == []


def test_owner_leaving_and_coming_back_in_one_window_keeps_the_room(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    run_sync(door, joined(ROOM, member(OWNER, "leave"), member(OWNER, "join"), msg("I am back"),
                          state=[name_ev("Work")]))
    assert api.left == [] and door.registry.get(KEY).state == "open" and len(runner.calls) == 1


def test_failed_join_is_retried_in_the_background(tmp_path):
    door, api = make_door(tmp_path)
    attempts = []

    def flaky(room_id):
        attempts.append(room_id)
        return MxError("M_UNKNOWN", "temporarily unavailable", 502) if len(attempts) < 3 else None

    api.fail["join"] = flaky

    async def scenario():
        await door.handle_sync(invited(ROOM, OWNER, name="Later"))
        assert api.joined == [] and door.background
        await asyncio.wait(list(door.background), timeout=5)

    go(scenario())
    assert len(attempts) == 3 and api.joined == [ROOM] and door.registry.get(KEY).title == "Later"
    assert api.texts(ROOM) == [GREETING]
    door2, api2 = make_door(tmp_path / "b")
    api2.fail["join"] = MxError("M_FORBIDDEN", "You are not invited to this room.", 403)
    run_sync(door2, invited(ROOM, OWNER))
    assert not door2.background and api2.joined == []      # refused for good: no retries


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
    kinds = [(t, c.get("body") or (c.get("m.relates_to") or {}).get("key")) for _r, t, c in api.sent]
    assert kinds.index(("m.room.message", "**Done**")) < kinds.index(("m.reaction", "✅"))   # ✅ after delivery
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

    async def send(room_id, event_type, content, txn=None):
        if content.get("body") == "Hi from the agent":
            seen.append([m["key"] for m in running.active(tmp_path)])
        return await original(room_id, event_type, content, txn)

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


def _private(door, room, owner_membership="join", **extra):
    door.registry.update_chat(room, door="mx", members={OWNER: owner_membership}, members_known=True, **extra)


def test_finished_jobs_are_reported_by_a_turn(tmp_path, monkeypatch):
    runner = FakeRunner("The backup finished.")
    door, api = make_door(tmp_path, runner)
    door.registry.upsert(KEY, title="Server", state="open", last_turn_at=100.0)
    door.registry.upsert(KEY2, title="Recent", state="open", last_turn_at=200.0)
    door.registry.upsert(KEY3, title="Gone", state="deleted")
    _private(door, ROOM2)
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
        assert reported == ["j3"]                       # nowhere to report: marked; the others only once delivered
        await settle(door)
        assert await door.report_jobs() == 0

    go(scenario())
    assert sorted(reported) == ["j1", "j2", "j3"]
    by_room = {c["env"]["ARCHI_CONVERSATION"]: c["prompt"] for c in runner.calls}
    assert by_room[KEY].endswith("[job j1 ok]") and by_room[KEY2].endswith("[job j2 failed]")   # private room
    assert len(runner.calls) == 2


def test_job_fallback_is_private_or_a_new_room(tmp_path):
    door, api = make_door(tmp_path)
    for key, room, title, members in ((KEY, ROOM, "Shared", {OWNER: "join", STRANGER: "join"}),
                                      (KEY2, ROOM2, "Owner left", {STRANGER: "join"})):
        door.registry.upsert(key, title=title, state="open", last_turn_at=time.time())
        door.registry.update_chat(room, door="mx", members=members, members_known=True)
    door.registry.upsert(KEY3, title="Gone", state="deleted")
    gone = door.registry.get(KEY3)
    made = go(door.job_fallback(gone))
    assert made == "mx:!made1:example.org" and api.created == [("!made1:example.org", "Archi: background jobs", [OWNER])]
    conv = door.registry.get(made)
    assert conv.state == "open" and door.registry.chat("!made1:example.org")["created_by_agent"] is True
    assert go(door.job_fallback(gone)) == made and len(api.created) == 1     # reused while the owner has not joined
    _private(door, "!r9:example.org")
    door.registry.upsert("mx:!r9:example.org", title="Mine", state="open", last_turn_at=1.0)
    assert go(door.job_fallback(gone)) == "mx:!r9:example.org"               # a joined private room comes first


def test_nothing_is_posted_where_the_owner_is_not(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    api.members_of[ROOM] = {BOT: "join", STRANGER: "join"}
    go(door.handle_sync(joined(ROOM, state=[name_ev("Not mine")]), initial=True))   # found at the first start
    assert api.left == [ROOM] and door.registry.get(KEY) is None
    door.registry.upsert(KEY2, title="Theirs", state="open")
    door.registry.update_chat(ROOM2, door="mx", members={STRANGER: "join"}, members_known=True)
    assert go(door.send_text(KEY2, "hello")) is False and api.sent == []
    running.start("matrix", KEY2, tmp_path, pid=DEAD_PID)
    go(door._report_interrupted())
    assert api.sent == []


def test_job_report_is_marked_only_after_delivery_and_offered_again_after_a_stop(tmp_path, monkeypatch):
    runner = FakeRunner(wait_cancel=True)
    door, api = make_door(tmp_path, runner)
    door.registry.upsert(KEY, title="Server", state="open")
    queue = [{"id": "j1", "conversation": KEY, "state": "ok", "label": "backup"}]
    reported = []
    monkeypatch.setattr(jobs, "finished_unreported", lambda d, home=None: [j for j in queue if j["id"] not in reported])
    monkeypatch.setattr(jobs, "mark_reported", lambda job_id, home=None: reported.append(job_id))
    monkeypatch.setattr(jobs, "report_prompt", lambda job: f"[job {job['id']}]")

    async def scenario():
        assert await door.report_jobs() == 1
        await asyncio.wait_for(runner.started.wait(), 5)
        assert await door.report_jobs() == 0             # its report turn is running: not offered twice
        assert reported == []
        door.stopping = True                             # the door stops before the report was delivered
        for t in list(door.running.values()):
            t.cancel.set()
        await settle(door)

    go(scenario())
    assert reported == [] and door._reporting == set()
    door2, api2 = make_door(tmp_path, FakeRunner("Backup done."))
    go(door2.report_jobs())
    go(settle(door2))
    assert reported == ["j1"] and any("Backup done." in t for t in api2.texts(ROOM))


def test_attachment_parsing():
    att = media.attachment({"msgtype": "m.image", "body": "caption here", "filename": "x.heic",
                            "url": "mxc://e/1", "info": {"mimetype": "image/heic", "size": 9}})
    assert att.kind == "file" and att.caption == "caption here" and att.name == "x.heic"
    assert media.attachment({"msgtype": "m.text", "body": "x"}) is None
    assert media.attachment({"msgtype": "m.file", "body": "y", "url": "https://evil"}) is None
    assert media.attachment({"msgtype": "m.audio", "body": "song.mp3", "url": "mxc://e/2"}).kind == "audio"
    assert media.image_size(PNG) == (640, 480) and media.image_size(b"nope") is None


# ------------------------------------------------------------------ fix round: delivery never loses an answer
def net_down(*_a):
    return MxError("ARCHI_NETWORK", "send: ClientConnectorError: connection refused", 0)


def test_transient_send_failure_is_retried_with_one_transaction(tmp_path):
    door, api = make_door(tmp_path, FakeRunner("Done."))
    tries = []
    original = api.send

    async def send(room_id, event_type, content, txn=None):
        if content.get("body") == "Done.":
            tries.append(txn)
            if len(tries) < 3:
                raise net_down()
        return await original(room_id, event_type, content, txn)

    api.send = send
    run_sync(door, joined(ROOM, msg("hello", eid="$h")))
    assert len(tries) == 3 and tries[0] and len(set(tries)) == 1      # one message, retried under one txn
    assert [c["body"] for c in api.messages(ROOM, "m.text")].count("Done.") == 1
    assert api.reactions()[-1] == ("$h", "✅") and not (tmp_path / "state" / "outbox").exists()


def test_undelivered_answer_is_kept_and_delivered_when_the_server_is_back(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(afterturn, "turn_finished", lambda door, key, **kw: calls.append(kw["answer"]) or None)
    door, api = make_door(tmp_path, FakeRunner("The report is ready."))
    order = []
    original = api.send

    async def send(room_id, event_type, content, txn=None):
        order.append((event_type, content.get("body") or (content.get("m.relates_to") or {}).get("key")))
        return await original(room_id, event_type, content, txn)

    api.send = send
    api.fail["send"] = lambda r, t, c: net_down() if t == "m.room.message" and c.get("body") == "The report is ready." else None
    run_sync(door, joined(ROOM, msg("make the report", eid="$m")))
    kept = list((tmp_path / "state" / "outbox").glob("matrix--*.json"))
    assert len(kept) == 1 and (kept[0].stat().st_mode & 0o777) == 0o600
    assert (kept[0].parent.stat().st_mode & 0o777) == 0o700
    record = json.loads(kept[0].read_text())
    assert record["key"] == KEY and record["items"][-1] == ["text", "The report is ready."]
    assert record["mark"]["src"] == "$m" and record["mark"]["reaction"]
    assert api.reactions()[-1] == ("$m", "⚠️") and calls == []          # not delivered: no ✅, no learning hook
    assert running.active(tmp_path) == []

    api.fail.pop("send")                                                # the homeserver answers again
    assert go(door.flush_outbox()) == 1
    texts = api.texts(ROOM)
    assert texts[-2].startswith("_(Delivered late: this answer was ready at") and texts[-1] == "The report is ready."
    assert api.reactions()[-1] == ("$m", "✅") and record["mark"]["reaction"] in [r for _room, r in api.redactions]
    assert not list((tmp_path / "state" / "outbox").glob("*.json")) and calls == ["The report is ready."]


def test_kept_answer_is_delivered_at_the_next_start(tmp_path):
    door, api = make_door(tmp_path, FakeRunner("Answer one."))
    api.fail["send"] = lambda r, t, c: net_down() if c.get("body") == "Answer one." else None
    run_sync(door, joined(ROOM, msg("q1")))
    assert len(list((tmp_path / "state" / "outbox").glob("*.json"))) == 1
    door2, api2 = make_door(tmp_path)
    go(door2.startup())
    assert api2.texts(ROOM)[-1] == "Answer one." and not list((tmp_path / "state" / "outbox").glob("*.json"))


def test_a_broken_send_path_keeps_the_answer_and_logs(tmp_path):
    logs = []
    door, api = make_door(tmp_path, FakeRunner("x"))
    door.log = lambda *a: logs.append(" ".join(map(str, a)))

    async def boom(key, md, **kw):
        raise RuntimeError("renderer exploded")

    door.send_text = boom
    run_sync(door, joined(ROOM, msg("hello")))
    assert len(list((tmp_path / "state" / "outbox").glob("*.json"))) == 1
    assert any("renderer exploded" in line for line in logs) and running.active(tmp_path) == []


def test_background_task_errors_are_logged(tmp_path):
    logs = []
    door, api = make_door(tmp_path)
    door.log = lambda *a: logs.append(" ".join(map(str, a)))

    async def bad():
        raise ValueError("lost in the background")

    async def scenario():
        door.spawn(bad())
        await asyncio.sleep(0)
        await settle(door)

    go(scenario())
    assert any("lost in the background" in line for line in logs)


def test_image_download_failure_replies_and_leaves_no_temp_files(tmp_path, monkeypatch):
    made = []
    real = media.temp_file
    monkeypatch.setattr(media, "temp_file", lambda data, suffix: made.append(real(data, suffix)) or made[-1])
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    api.media["mxc://example.org/ok"] = PNG

    async def download(mxc):
        if mxc == "mxc://example.org/ok":
            return PNG
        raise MxError("ARCHI_NETWORK", "download: ServerDisconnectedError", 0)

    api.download = download

    async def failing_turn(*a, **kw):
        raise RuntimeError("the turn crashed")

    door.turn = failing_turn
    run_sync(door, joined(ROOM, media_msg("m.image", body="a.png", url="mxc://example.org/ok",
                                          info={"mimetype": "image/png", "size": 70}),
                          media_msg("m.image", body="b.png", url="mxc://example.org/broken",
                                    info={"mimetype": "image/png", "size": 70})))
    assert len(made) == 1 and not made[0].exists()          # removed even though the turn crashed


def test_encrypted_room_left_even_when_the_network_hiccups(tmp_path):
    door, api = make_door(tmp_path)
    leaves = []

    async def leave(room_id):
        leaves.append(room_id)
        if len(leaves) == 1:
            raise MxError("ARCHI_NETWORK", "leave: connection reset", 0)
        api.left.append(room_id)

    api.leave = leave
    api.fail["send"] = lambda r, t, c: net_down()

    async def scenario():
        await door.handle_sync(invited(ROOM, OWNER, name="Secret", encrypted=True))
        await asyncio.wait(list(door.background), timeout=5)

    go(scenario())
    assert leaves == [ROOM, ROOM] and api.left == [ROOM]


def test_second_invite_into_the_same_encrypted_room_is_explained_again(tmp_path):
    door, api = make_door(tmp_path)
    run_sync(door, invited(ROOM, OWNER, name="Secret", encrypted=True))
    run_sync(door, invited(ROOM, OWNER, name="Secret", encrypted=True))
    assert api.joined == [ROOM, ROOM] and api.left == [ROOM, ROOM]
    assert api.texts(ROOM) == [ENCRYPTED_NOTICE, ENCRYPTED_NOTICE]


def test_only_a_state_event_makes_a_room_encrypted(tmp_path):
    runner = FakeRunner()
    door, api = make_door(tmp_path, runner)
    fake = ev("m.room.encryption", {"algorithm": "m.megolm.v1.aes-sha2"})          # no state_key: not state
    run_sync(door, joined(ROOM, fake, msg("still readable"), state=[name_ev("Plain")]))
    assert api.left == [] and door.registry.get(KEY).state == "open" and len(runner.calls) == 1
    assert ENCRYPTED_NOTICE not in api.texts(ROOM)


def test_corrupt_encrypted_attachment_gets_a_reply(tmp_path):
    from nio.crypto.attachments import encrypt_attachment
    door, api = make_door(tmp_path)
    cipher, keys = encrypt_attachment(b"secret notes")
    api.media["mxc://example.org/enc"] = cipher[:-1] + bytes([cipher[-1] ^ 1])          # hash no longer matches
    run_sync(door, joined(ROOM, media_msg("m.file", body="notes.txt", file={
        "url": "mxc://example.org/enc", "key": keys["key"], "iv": keys["iv"], "hashes": keys["hashes"], "v": "v2"})))
    assert any("could not download the file" in t and "decrypted" in t for t in api.texts(ROOM))


def test_first_notice_in_flight_still_becomes_the_footer():
    async def scenario():
        api = FakeMx()
        original = api.send

        async def slow_send(room_id, event_type, content, txn=None):
            if content.get("msgtype") == "m.notice" and "m.relates_to" not in content:
                await asyncio.sleep(0.3)                 # the first notice is still on its way when the turn ends
            return await original(room_id, event_type, content, txn)

        api.send = slow_send
        st = TurnStatus()
        live = LiveStatus(api, ROOM, "$src", st, log=lambda *a: None, delay=0.05, edit_min=0.1, tick=30,
                          typing_every=30)
        await live.start()
        await asyncio.sleep(0.15)
        st.finish(TurnResult("x", "s", True))
        await live.finish()
        await live.done(True, "delivered")
        return api, live

    api, live = go(scenario())
    assert live.notice_id is not None
    footer = api.edits()[-1]
    assert footer["m.relates_to"]["event_id"] == live.notice_id and footer["m.new_content"]["body"].startswith("✅ Done")


def test_rejected_token_replaced_in_the_config_is_adopted(tmp_path):
    door, api = make_door(tmp_path)
    calls = []

    async def sync(since, timeout_ms, sync_filter=None):
        calls.append(api.token)
        if api.token == "tok":
            door.cfg.set("MATRIX_ACCESS_TOKEN", "tok-new")          # `archi setup matrix` ran meanwhile
            raise MxError("M_UNKNOWN_TOKEN", "Unknown access token", 401)
        raise asyncio.CancelledError

    api.sync = sync
    with pytest.raises(asyncio.CancelledError):
        go(door.sync_loop())
    assert calls == ["tok", "tok-new"]


def test_send_refused_for_an_old_token_retries_with_the_new_one(tmp_path):
    door, api = make_door(tmp_path, FakeRunner("Answer."))
    tokens = []
    original = api.send

    async def send(room_id, event_type, content, txn=None):
        if content.get("body") == "Answer.":
            tokens.append(api.token)
            if api.token == "tok":
                door.cfg.set("MATRIX_ACCESS_TOKEN", "tok-rotated")
                raise MxError("M_UNKNOWN_TOKEN", "Unknown access token", 401)
        return await original(room_id, event_type, content, txn)

    api.send = send
    run_sync(door, joined(ROOM, msg("hi")))
    assert tokens == ["tok", "tok-rotated"] and "Answer." in api.texts(ROOM)



def test_room_the_agent_just_made_is_kept_until_the_owner_invite_arrives(tmp_path):
    """The bot's sync can see a room while createRoom is still running: its join is there, the owner's invite
    not yet. That is no reason to leave; posting waits until the owner's membership shows up."""
    door, api = make_door(tmp_path)
    door.registry.upsert(KEY, title="Made", state="open")
    door.registry.update_chat(ROOM, door="mx", membership="join", created_by_agent=True)
    api.members_of[ROOM] = {BOT: "join"}
    run_sync(door, joined(ROOM, member(BOT, "join"), state=[name_ev("Made")]))
    assert api.left == [] and door.registry.get(KEY).state == "open"
    assert go(door.send_text(KEY, "too early")) is False and api.sent == []
    run_sync(door, joined(ROOM, member(OWNER, "invite", sender=BOT)))
    assert go(door.send_text(KEY, "now")) is True and api.texts(ROOM) == ["now"]


def test_a_kept_answer_delivered_meanwhile_is_not_brought_back(tmp_path):
    """The outbox can deliver (and remove) a kept answer while the turn is still setting its final mark: the
    mark must not write the record back, or the answer would go out a second time."""
    door, api = make_door(tmp_path, FakeRunner("Kept answer."))
    api.fail["send"] = lambda r, t, c: net_down() if c.get("body") == "Kept answer." else None
    original_done = LiveStatus.done

    async def done_then_flush(self, ok, outcome="delivered"):
        await original_done(self, ok, outcome)
        if outcome == "queued":
            api.fail.pop("send", None)
            await door.flush_outbox()                   # delivered while the turn has not finished yet

    LiveStatus.done = done_then_flush
    try:
        run_sync(door, joined(ROOM, msg("hello", eid="$k")))
    finally:
        LiveStatus.done = original_done
    assert [t for t in api.texts(ROOM) if t == "Kept answer."] == ["Kept answer."]
    assert not list((tmp_path / "state" / "outbox").glob("*.json"))
    go(door.flush_outbox())
    assert [t for t in api.texts(ROOM) if t == "Kept answer."] == ["Kept answer."]
