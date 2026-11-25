import json
import os
import stat

from core.config import Config, mask
from core.conversations import Registry, make_key, parse_key
from core.engine import Event, TurnResult
from core.markers import split_answer
from core.status import TurnStatus
from core import voice


# ------------------------------------------------------------------ config
def test_config_roundtrip_keeps_comments_and_mode(tmp_path):
    cfg = Config(tmp_path)
    cfg.path.parent.mkdir(parents=True)
    cfg.path.write_text("# my comment\nPROVIDER=codex\nAGENT_NAME=\"Max Power\"\n")
    assert cfg.get("PROVIDER") == "codex" and cfg.get("AGENT_NAME") == "Max Power"
    cfg.set("OWNER_ID", "1001")
    cfg.set("PAIRING_CODE", None)
    cfg.set("AGENT_NAME", "A # tricky \"name\"")
    text = cfg.path.read_text()
    assert text.startswith("# my comment\nPROVIDER=codex\n")
    assert Config(tmp_path).get("AGENT_NAME") == "A # tricky \"name\""
    assert Config(tmp_path).owner_id() == 1001
    assert stat.S_IMODE(os.stat(cfg.path).st_mode) == 0o600


def test_config_sees_changes_from_another_writer(tmp_path):
    a, b = Config(tmp_path), Config(tmp_path)
    a.set("CLAUDE_MODEL", "m1")
    assert b.get("CLAUDE_MODEL") == "m1"
    b.set("CLAUDE_MODEL", "m2")
    assert a.get("CLAUDE_MODEL") == "m2"
    assert a.get("PROVIDER") == "claude"      # default
    assert mask("OPENAI_API_KEY", "sk-abcdefghijklmnop") .startswith("<set")


# ------------------------------------------------------------------ registry
def test_keys():
    assert make_key("tg", -100500, 7) == "tg:-100500:7"
    assert make_key("tg", 1001, None) == "tg:1001:main"
    assert parse_key("tg:-100500:7") == ("tg", "-100500", 7)
    assert parse_key("tg:1001:main") == ("tg", "1001", None)
    assert make_key("mx", "!room:example.org:8448") == "mx:!room:example.org:8448"
    assert parse_key("mx:!room:example.org:8448") == ("mx", "!room:example.org:8448", None)
    assert parse_key("mx:!v12room") == ("mx", "!v12room", None)


def test_registry_mixes_telegram_and_matrix(tmp_path):
    reg = Registry(tmp_path)
    reg.upsert("tg:1001:5", title="Topic")
    reg.upsert("mx:!b:example.org", title="")
    reg.upsert("mx:!a:example.org", title="Room A")
    reg.upsert("tg:-100:main")
    assert [c.key for c in reg.all()] == ["mx:!a:example.org", "mx:!b:example.org", "tg:-100:main", "tg:1001:5"]
    room = reg.get("mx:!b:example.org")
    assert room.chat_id == "!b:example.org" and room.thread_id is None and room.display_title() == "unnamed room"
    assert reg.get("tg:1001:5").chat_id == 1001
    reg.update_chat("!a:example.org", door="mx", encrypted=False)
    reg.update_chat(1001, owner_seen=True)
    assert list(reg.chats(door="mx")) == ["!a:example.org"] and len(reg.chats()) == 2


def test_inbox_names_never_collide(tmp_path):
    from core.inbox import file_prompt, inbox_path
    first = inbox_path(tmp_path, "../x y.txt", now=1_800_000_000)
    first.write_text("1")
    second = inbox_path(tmp_path, "../x y.txt", now=1_800_000_000)
    assert first.name.endswith("_x_y.txt") and second != first and second.parent == tmp_path
    assert "Do NOT execute it" in file_prompt("file", second, "x y.txt")


def test_registry_persists_and_archives(tmp_path):
    reg = Registry(tmp_path)
    key = make_key("tg", 1001, 5)
    conv = reg.upsert(key, title="Plans", title_confirmed=True)
    assert conv.created_at > 0 and conv.thread_id == 5 and not conv.is_main
    assert reg.set_session(key, "claude", "s1")
    gen = reg.generation(key)
    assert reg.clear_session(key) is True
    assert reg.set_session(key, "claude", "s-late", gen=gen) is False   # /new happened during the turn
    assert Registry(tmp_path).get(key).sessions == {}
    gone = reg.mark_deleted(key, reason="probe")
    assert gone and gone.state == "deleted"
    assert reg.mark_deleted(key) is None                                  # announced once
    assert reg.all() == [] and len(reg.all(include_deleted=True)) == 1
    archived = [json.loads(x) for x in (tmp_path / "archive.jsonl").read_text().splitlines()]
    assert archived[0]["key"] == key and archived[0]["reason"] == "probe"
    data = json.loads((tmp_path / "conversations.json").read_text())
    assert data["conversations"][key]["state"] == "deleted"


def test_registry_survives_corrupt_file(tmp_path):
    (tmp_path / "conversations.json").write_text("{broken")
    reg = Registry(tmp_path)
    assert reg.all() == []
    reg.upsert("tg:1:main", title="main")
    assert reg.get("tg:1:main").title == "main"


# ------------------------------------------------------------------ markers / status / voice
def test_markers():
    text = "Here.\n[FILE /tmp/a.txt]\nSee [FILE /tmp/inline] inline\n[VOICE lang=\"en\" speed=\"1.5\"]Hi there[/VOICE]"
    clean, files, voices = split_answer(text)
    assert files == ["/tmp/a.txt"] and voices == [("en", "Hi there", 1.5)]
    assert "[VOICE" not in clean and "inline" in clean


def test_status_model():
    t = [100.0]
    st = TurnStatus(now=lambda: t[0])
    st.feed(Event("step", "Reading a.py"))
    st.feed(Event("step", "Editing a.py"))
    st.feed(Event("bg", "Long build", data={"id": "b1", "state": "running"}))
    st.feed(Event("text", "partial"))
    t[0] = 165.0
    assert st.headline() == "Working · 1:05 · 2 steps"
    assert st.step_lines() == ["Editing a.py", "Reading a.py"]
    assert "in background: Long build" in st.render_text()
    st.feed(Event("done", result=TurnResult("x", "s", True)))
    assert st.state == "done" and st.footer() == "Done in 1:05 · 2 steps"


def test_voice_helpers(tmp_path):
    assert voice.looks_like_openai_key("sk-proj-" + "a" * 40)
    assert not voice.looks_like_openai_key("sk-ant-" + "a" * 40)
    assert not voice.looks_like_openai_key("hello sk-" + "a" * 40)
    assert voice.available(Config(tmp_path)) is False
    cuts = voice.cut_points(600, [170.0, 185.0, 400.0])
    assert cuts[0] == 185.0 and all(b - a > 100 for a, b in zip([0, *cuts], cuts))
