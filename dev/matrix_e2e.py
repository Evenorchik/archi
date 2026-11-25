#!/usr/bin/env python3
"""Live end-to-end test of the Matrix door against a throwaway homeserver (see dev/matrix_e2e.md).

    python3 dev/matrix_e2e.py [--port 18008] [--keep]

Starts a continuwuity container (federation off, registration only with a generated token, published on
127.0.0.1 only), registers an owner, a bot and a stranger, runs `archi setup matrix` non-interactively with a
unit directory systemd does not read, runs the door from this checkout with a temporary ARCHI_HOME and the FAKE
agent CLI (tests/fake_cli.py; never the real claude or codex), and drives it as the owner and the stranger
through the client API. The container, its volume and the work directory (it holds the bot's token) are
removed at the end unless --keep. Needs docker and the checkout's .venv. Stdlib only.
"""
import argparse
import json
import os
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse as up
import urllib.request
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python")
IMAGE = "forgejo.ellis.link/continuwuation/continuwuity:v26.7.2"
CONTAINER, VOLUME = "archi-test-hs", "archi-test-hs-db"
SERVER = "archi-test.localhost"
OWNER, BOT, STRANGER = f"@owner:{SERVER}", f"@archibot:{SERVER}", f"@stranger:{SERVER}"
KEY_REQUEST_EN = "Send me an OpenAI API key and I will add it to the config"
HS = "http://127.0.0.1:18008"
WORK = HOME = UNITS = OUT = Path()
results = []
DOOR_PROCESSES = []


# ---------------------------------------------------------------------- client API
def call(method, path, token=None, body=None, query=None, raw=None, ctype="application/json"):
    """-> (status, JSON body or raw bytes)."""
    url = HS + path + (("?" + up.urlencode(query)) if query else "")
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", ctype)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            b = r.read()
            try:
                return r.status, json.loads(b.decode() or "{}")
            except ValueError:
                return r.status, b
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b.decode() or "{}")
        except ValueError:
            return e.code, b


def register(user, password, regtoken):
    st, res = call("POST", "/_matrix/client/v3/register", body={"username": user, "password": password,
                                                                 "inhibit_login": False})
    if st != 401:
        return st, res
    session = res.get("session")
    st, res = call("POST", "/_matrix/client/v3/register", body={
        "username": user, "password": password, "inhibit_login": False,
        "auth": {"type": "m.login.registration_token", "token": regtoken, "session": session}})
    if st == 401 and any("m.login.dummy" in f.get("stages", []) for f in res.get("flows", [])):
        st, res = call("POST", "/_matrix/client/v3/register", body={
            "username": user, "password": password, "auth": {"type": "m.login.dummy", "session": session}})
    return st, res


def login(user, password):
    return call("POST", "/_matrix/client/v3/login", body={"type": "m.login.password",
                                                          "identifier": {"type": "m.id.user", "user": user},
                                                          "password": password})


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    return ok


def wait(pred, timeout=30.0, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


def sh(*cmd, timeout=120, **kw):
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, **kw)


def png(w, h):
    raw = b"".join(b"\x00" + b"\x20\x80\xe0" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Client:
    """One person's view of the homeserver: their own incremental sync and actions."""

    def __init__(self, token, user_id):
        self.token, self.user_id = token, user_id
        self.since = None
        self.events, self.invites, self.typing = {}, {}, {}
        self.n = 0

    def sync(self, timeout=0):
        q = {"timeout": str(timeout)}
        if self.since:
            q["since"] = self.since
        st, s = call("GET", "/_matrix/client/v3/sync", self.token, query=q)
        assert st == 200, (st, s)
        self.since = s["next_batch"]
        rooms = s.get("rooms") or {}
        for section in ("join", "leave"):
            for rid, data in (rooms.get(section) or {}).items():
                self.events.setdefault(rid, []).extend((data.get("timeline") or {}).get("events") or [])
                for e in (data.get("ephemeral") or {}).get("events") or []:
                    if e.get("type") == "m.typing":
                        self.typing.setdefault(rid, []).append(list(e["content"].get("user_ids") or []))
        for rid, data in (rooms.get("invite") or {}).items():
            self.invites[rid] = data
        return s

    def ev(self, rid, sender=None, etype=None, pred=None):
        self.sync()
        out = []
        for e in self.events.get(rid, []):
            if sender and e.get("sender") != sender:
                continue
            if etype and e.get("type") != etype:
                continue
            if pred and not pred(e):
                continue
            out.append(e)
        return out

    def put(self, path, body):
        self.n += 1
        return call("PUT", path, self.token, body)

    def send(self, rid, content, etype="m.room.message"):
        st, r = self.put(f"/_matrix/client/v3/rooms/{up.quote(rid)}/send/{etype}/e2e{time.time_ns()}", content)
        assert st == 200, (st, r)
        return r["event_id"]

    def text(self, rid, body):
        return self.send(rid, {"msgtype": "m.text", "body": body})

    def create_room(self, name, invite=(), encrypted=False):
        body = {"name": name, "preset": "private_chat", "invite": list(invite)}
        if encrypted:
            body["initial_state"] = [{"type": "m.room.encryption", "state_key": "",
                                      "content": {"algorithm": "m.megolm.v1.aes-sha2"}}]
        st, r = call("POST", "/_matrix/client/v3/createRoom", self.token, body)
        assert st == 200, (st, r)
        return r["room_id"]

    def join(self, rid):
        st, r = call("POST", f"/_matrix/client/v3/join/{up.quote(rid)}", self.token, {})
        assert st == 200, (st, r)

    def leave(self, rid):
        st, r = call("POST", f"/_matrix/client/v3/rooms/{up.quote(rid)}/leave", self.token, {})
        assert st == 200, (st, r)

    def invite(self, rid, user):
        st, r = call("POST", f"/_matrix/client/v3/rooms/{up.quote(rid)}/invite", self.token, {"user_id": user})
        assert st == 200, (st, r)

    def set_name(self, rid, name):
        st, r = self.put(f"/_matrix/client/v3/rooms/{up.quote(rid)}/state/m.room.name/", {"name": name})
        assert st == 200, (st, r)

    def upload(self, data, ctype, filename):
        st, r = call("POST", "/_matrix/media/v3/upload", self.token, raw=data, ctype=ctype,
                        query={"filename": filename})
        assert st == 200, (st, r)
        return r["content_uri"]

    def download(self, mxc):
        server, media_id = mxc[len("mxc://"):].split("/", 1)
        st, body = call("GET", f"/_matrix/client/v1/media/download/{server}/{media_id}", self.token)
        return body if st == 200 else None


# ---------------------------------------------------------------------- bot-side selectors
def bot_msgs(c, rid, msgtype=None, plain=True):
    def ok(e):
        content = e.get("content") or {}
        if plain and "m.relates_to" in content:
            return False
        return msgtype is None or content.get("msgtype") == msgtype
    return c.ev(rid, sender=BOT, etype="m.room.message", pred=ok)


def bot_edits(c, rid):
    return c.ev(rid, sender=BOT, etype="m.room.message",
                pred=lambda e: (e["content"].get("m.relates_to") or {}).get("rel_type") == "m.replace")


def bot_reactions(c, rid, target):
    reacts = c.ev(rid, sender=BOT, etype="m.reaction",
                  pred=lambda e: (e["content"].get("m.relates_to") or {}).get("event_id") == target)
    redacted = {e.get("redacts") or (e.get("content") or {}).get("redacts")
                for e in c.ev(rid, sender=BOT, etype="m.room.redaction")}
    return [(e["content"]["m.relates_to"]["key"], e["event_id"] in redacted) for e in reacts]


def bot_membership(c, rid):
    m = c.ev(rid, etype="m.room.member", pred=lambda e: e.get("state_key") == BOT)
    return m[-1]["content"].get("membership") if m else None


def registry():
    try:
        return json.loads((HOME / "state" / "conversations.json").read_text())
    except (OSError, ValueError):
        return {"conversations": {}, "chats": {}}


def conv(rid):
    return registry()["conversations"].get(f"mx:{rid}")


def fake_log():
    try:
        return [json.loads(x) for x in (WORK / "fake.log").read_text().splitlines()]
    except OSError:
        return []


# ---------------------------------------------------------------------- environment
def fresh_server(port):
    sh("docker", "rm", "-f", CONTAINER)
    sh("docker", "volume", "rm", VOLUME)
    sh("docker", "volume", "create", VOLUME)
    regtoken = secrets.token_urlsafe(24)
    res = sh("docker", "run", "-d", "--name", CONTAINER, "-p", f"127.0.0.1:{port}:8008",
             "-v", f"{VOLUME}:/var/lib/continuwuity",
             "-e", f"CONTINUWUITY_SERVER_NAME={SERVER}", "-e", "CONTINUWUITY_DATABASE_PATH=/var/lib/continuwuity",
             "-e", "CONTINUWUITY_ADDRESS=0.0.0.0", "-e", "CONTINUWUITY_PORT=8008",
             "-e", "CONTINUWUITY_ALLOW_REGISTRATION=true", "-e", f"CONTINUWUITY_REGISTRATION_TOKEN={regtoken}",
             "-e", "CONTINUWUITY_ALLOW_FEDERATION=false", IMAGE)
    assert res.returncode == 0, res.stderr
    assert wait(_up, 60), "homeserver did not come up"
    def banner():
        r = sh("docker", "logs", CONTAINER)
        return re.search(r"using the registration token\s+(?:\x1b\[[0-9;]*m)?([A-Za-z0-9_-]+)", r.stdout + r.stderr)
    boot = wait(banner, 30).group(1)
    pw = {u: secrets.token_urlsafe(18) for u in ("owner", "archibot", "stranger")}
    assert register("owner", pw["owner"], boot)[0] == 200          # the first account uses the bootstrap token
    assert register("archibot", pw["archibot"], regtoken)[0] == 200
    assert register("stranger", pw["stranger"], regtoken)[0] == 200
    return pw


def _up():
    try:
        return call("GET", "/_matrix/client/versions")[0] == 200
    except Exception:
        return False


def fake_wrappers():
    out = {}
    for flavor in ("claude", "codex"):
        w = WORK / f"fake-{flavor}"
        w.write_text(f"#!/bin/sh\nFAKE_FLAVOR={flavor} exec {sys.executable} {REPO}/tests/fake_cli.py \"$@\"\n")
        w.chmod(w.stat().st_mode | stat.S_IEXEC)
        out[flavor] = str(w)
    return out


def door_env(fakes):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ARCHI_", "CLAUDE", "ANTHROPIC", "OPENAI"))}
    env.update(ARCHI_HOME=str(HOME), ARCHI_UNIT_DIR=str(UNITS), CLAUDE_BIN=fakes["claude"], CODEX_BIN=fakes["codex"],
               FAKE_LOG=str(WORK / "fake.log"), FAKE_PIDFILE=str(WORK / "fake.pids"), FAKE_MODE="ok",
               FAKE_STEP_S="1.5", FAKE_SLOW_STEPS="6", PYTHONUNBUFFERED="1")
    return env


def start_door(env, tag):
    log = open(WORK / f"door-{tag}.log", "w")
    p = subprocess.Popen([PY, "-m", "doors.matrix"], cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
    DOOR_PROCESSES.append(p)
    return p


def stop_door(p, timeout=30):
    p.send_signal(signal.SIGTERM)
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        return None


def archi(env, *args, stdin=None):
    return subprocess.run([sys.executable, str(REPO / "archi"), *args], capture_output=True, text=True, env=env,
                          timeout=180, input=stdin)


# ---------------------------------------------------------------------- the run
def run(port):
    for d in (HOME, UNITS, OUT):
        d.mkdir(parents=True, exist_ok=True)
    pw = fresh_server(port)
    owner = Client(login("owner", pw["owner"])[1]["access_token"], OWNER)
    stranger = Client(login("stranger", pw["stranger"])[1]["access_token"], STRANGER)
    owner.sync()
    fakes = fake_wrappers()
    env = door_env(fakes)

    # rooms that exist before the door ever starts
    room_a = owner.create_room("E2E main")                         # the bot joins it before the first start
    room_b = owner.create_room("E2E side", invite=[BOT])           # a pending invite at the first start

    # ---- archi setup matrix (non-interactive, unit dir systemd does not read)
    senv = dict(env, ARCHI_NONINTERACTIVE="1", ARCHI_MATRIX_HOMESERVER=HS, ARCHI_MATRIX_USER="archibot",
                ARCHI_MATRIX_PASSWORD=pw["archibot"], ARCHI_MATRIX_OWNER=OWNER)
    res = archi(senv, "setup", "matrix")
    cfg_text = (HOME / "config" / "archi.env").read_text()
    check("setup: exit 0", res.returncode == 0, res.stdout[-500:] + res.stderr[-500:])
    check("setup: config has homeserver, bot, owner, token, device",
          all(k in cfg_text for k in ("MATRIX_HOMESERVER=http://127.0.0.1:18008", f"MATRIX_USER_ID={BOT}",
                                      f"MATRIX_OWNER_ID={OWNER}", "MATRIX_ACCESS_TOKEN=", "MATRIX_DEVICE_ID=")))
    check("setup: the password is not stored, config mode 600",
          pw["archibot"] not in cfg_text and (os.stat(HOME / "config" / "archi.env").st_mode & 0o777) == 0o600)
    token = re.search(r"^MATRIX_ACCESS_TOKEN=(\S+)$", cfg_text, re.M).group(1)
    check("setup: no token in its output", token not in res.stdout + res.stderr)
    unit = UNITS / "archi-matrix.service"
    check("setup: unit written to the temp unit dir, not started",
          unit.exists() and "-m doors.matrix" in unit.read_text() and "not started" in res.stdout)
    check("setup: lists the pending invite", "«E2E side» · invited" in res.stdout, res.stdout[-800:])
    archi(env, "config", "set", "TELEGRAM_BOT_TOKEN", "123456:" + "T" * 35)   # format-valid dummy for validate

    # the bot is already in room A with an old owner message: history, never answered
    bot = Client(token, BOT)
    owner.invite(room_a, BOT)
    bot.join(room_a)
    owner.text(room_a, "old message before the first start")

    # ---- first door start
    door = start_door(env, "1")
    check("door: B (pending owner invite) joined + greeting notice",
          wait(lambda: bot_msgs(owner, room_b, "m.notice"), 30)
          and bot_membership(owner, room_b) == "join")
    time.sleep(3)
    check("door: history in A is not answered", not bot_msgs(owner, room_a, "m.text"))
    check("registry: A and B registered with titles",
          (conv(room_a) or {}).get("title") == "E2E main" and (conv(room_b) or {}).get("title") == "E2E side")

    # ---- a plain turn
    m1 = owner.text(room_a, "hello e2e")
    answer = wait(lambda: [e for e in bot_msgs(owner, room_a, "m.text") if "hello e2e" in e["content"]["body"]], 30)
    check("turn: answer is an m.text with HTML", answer and answer[0]["content"].get("format") ==
          "org.matrix.custom.html" and answer[0]["content"].get("formatted_body"))
    reacts = wait(lambda: [r for r in bot_reactions(owner, room_a, m1) if r[0] == "✅"] and bot_reactions(owner, room_a, m1), 10)
    redactions = owner.ev(room_a, sender=BOT, etype="m.room.redaction")
    check("turn: reactions ⏳ (taken back) then ✅", reacts and ("✅", False) in reacts and ("⏳", False) not in reacts
          and redactions, str(reacts))
    check("turn: quick answer got no status notice", not bot_msgs(owner, room_a, "m.notice"))
    check("registry: session stored", (conv(room_a) or {}).get("sessions", {}).get("claude") == "sess-new")
    first = [x for x in fake_log() if "hello e2e" in x["stdin"]]
    check("turn: env of the agent", first and first[0]["env"].get("ARCHI_DOOR") == "matrix"
          and first[0]["env"].get("ARCHI_ROOM_ID") == room_a and first[0]["env"].get("ARCHI_CONVERSATION") == f"mx:{room_a}"
          and first[0]["cwd"] == str(HOME), str(first[0]["env"] if first else None))

    # ---- live status on a slow turn
    m2 = owner.text(room_a, "fake:slow please")
    done = wait(lambda: [e for e in bot_msgs(owner, room_a, "m.text") if "fake:slow" in e["content"]["body"]], 45)
    notices = bot_msgs(owner, room_a, "m.notice")
    edits = bot_edits(owner, room_a)
    check("status: one m.notice after a few seconds", done and len(notices) == 1
          and notices[0]["content"]["body"].startswith("⏳ Working"), str([n["content"]["body"] for n in notices]))
    stamps = [e["origin_server_ts"] for e in edits]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    check("status: edited in place, throttled (>= 3 s apart while running)",
          len(edits) >= 2 and all(g >= 2900 for g in gaps[:-1]), f"{len(edits)} edits, gaps {gaps}")
    last = edits[-1]["content"]["m.new_content"]["body"] if edits else ""
    check("status: ends as a footer", last.startswith("✅ Done in") and "steps" in last, last)
    check("status: steps from tool events shown",
          any("Slow step" in e["content"]["m.new_content"]["body"] for e in edits))
    check("status: typing shown during the turn", any(BOT in ids for ids in owner.typing.get(room_a, [])))
    check("status: ✅ on the slow turn", ("✅", False) in bot_reactions(owner, room_a, m2))

    # ---- commands
    owner.text(room_a, "!status")
    st_ans = wait(lambda: [e for e in bot_msgs(owner, room_a, "m.text") if "agent: `claude`" in e["content"]["body"]], 15)
    check("command: !status", st_ans)
    owner.text(room_a, "/help")
    check("command: /help lists !jobs", wait(lambda: [e for e in bot_msgs(owner, room_a, "m.text")
                                                      if "!jobs" in e["content"]["body"]], 15))
    owner.text(room_a, "!jobs")
    check("command: !jobs", wait(lambda: [e for e in bot_msgs(owner, room_a, "m.text")
                                          if e["content"]["body"].startswith("No jobs")], 15))
    m3 = owner.text(room_a, "fake:hang forever")
    wait(lambda: bot_reactions(owner, room_a, m3), 15)
    time.sleep(1)
    owner.text(room_a, "!stop")
    check("command: !stop ends the running turn", wait(lambda: [e for e in bot_msgs(owner, room_a, "m.text")
                                                                if e["content"]["body"].startswith("⏹ Stopped after")], 20))
    check("command: ⚠️ on the stopped turn", wait(lambda: ("⚠️", False) in bot_reactions(owner, room_a, m3), 10))

    # ---- rename
    owner.set_name(room_a, "E2E main renamed")
    check("lifecycle: rename updates the title", wait(lambda: (conv(room_a) or {}).get("title") == "E2E main renamed", 20))

    # ---- media in
    img = png(64, 48)
    mxc = owner.upload(img, "image/png", "dot.png")
    owner.send(room_a, {"msgtype": "m.image", "body": "describe this", "filename": "dot.png", "url": mxc,
                        "info": {"mimetype": "image/png", "size": len(img), "w": 64, "h": 48}})
    check("media in: image reaches the agent as image input",
          wait(lambda: [x for x in fake_log() if '"type": "image"' in x["stdin"] and "describe this" in x["stdin"]], 30))
    data = b"line one\nline two\n"
    mxc = owner.upload(data, "text/plain", "notes.txt")
    owner.send(room_a, {"msgtype": "m.file", "body": "notes.txt", "url": mxc,
                        "info": {"mimetype": "text/plain", "size": len(data)}})
    saved = wait(lambda: list((HOME / ".inbox").glob("*_notes.txt")), 30)
    check("media in: file saved to .inbox with the same bytes", saved and saved[0].read_bytes() == data)
    check("media in: the file prompt names the path, never runs it",
          wait(lambda: [x for x in fake_log() if saved and str(saved[0]) in x["stdin"] and "Do NOT execute" in x["stdin"]], 20))
    mxc = owner.upload(b"OggS" + b"\x00" * 60, "audio/ogg", "voice.ogg")
    owner.send(room_a, {"msgtype": "m.audio", "body": "Voice message", "url": mxc,
                        "info": {"mimetype": "audio/ogg", "size": 64, "duration": 1000},
                        "org.matrix.msc1767.audio": {"duration": 1000}, "org.matrix.msc3245.voice": {}})
    check("media in: voice without a key asks for it", wait(lambda: [e for e in bot_msgs(owner, room_a, "m.text")
                                                                     if e["content"]["body"] == KEY_REQUEST_EN], 20))

    # ---- media out
    chart, report = OUT / "chart.png", OUT / "report.txt"
    chart.write_bytes(png(120, 80))
    report.write_text("quarterly numbers\n")
    owner.text(room_a, f"send it fake:file={chart}")
    im = wait(lambda: bot_msgs(owner, room_a, "m.image"), 30)
    check("media out: [FILE png] as m.image with size", im and im[0]["content"]["info"].get("w") == 120
          and im[0]["content"]["info"].get("h") == 80 and owner.download(im[0]["content"]["url"]) == chart.read_bytes())
    owner.text(room_a, f"and fake:file={report}")
    fl = wait(lambda: bot_msgs(owner, room_a, "m.file"), 30)
    check("media out: [FILE txt] as m.file", fl and fl[0]["content"]["body"] == "report.txt"
          and owner.download(fl[0]["content"]["url"]) == report.read_bytes())
    owner.text(room_a, "talk fake:voice")
    check("media out: [VOICE] without a key falls back to text",
          wait(lambda: [e for e in bot_msgs(owner, room_a, "m.text") if e["content"]["body"] == "Hello from the fake agent"], 30))

    # ---- encrypted room, strangers
    room_e = owner.create_room("E2E encrypted", invite=[BOT], encrypted=True)
    check("encrypted: the bot explains in plain text and leaves",
          wait(lambda: bot_membership(owner, room_e) == "leave", 30)
          and any("end-to-end encrypted" in e["content"]["body"] for e in bot_msgs(owner, room_e, "m.text")))
    check("encrypted: recorded, no conversation kept",
          registry()["chats"].get(room_e, {}).get("encrypted") is True
          and (conv(room_e) is None or conv(room_e)["state"] == "deleted"))
    stranger.sync()
    room_s = stranger.create_room("Stranger room", invite=[BOT])
    stranger.text(room_s, "hello bot")
    check("strangers: their invite is rejected silently",
          wait(lambda: bot_membership(stranger, room_s) == "leave", 30) and not bot_msgs(stranger, room_s, plain=False))
    owner.invite(room_b, STRANGER)
    stranger.join(room_b)
    stranger.text(room_b, "!status")
    stranger.text(room_b, "please answer me")
    time.sleep(6)
    check("strangers: ignored in the owner's room", len(bot_msgs(owner, room_b)) == 1)    # only the greeting

    # ---- archi mx (inside-a-turn CLI)
    res = archi(env, "mx", "room", "create", "E2E created")
    rid = re.search(r"created room «E2E created»: (\S+),", res.stdout)
    check("archi mx room create", res.returncode == 0 and rid, res.stdout + res.stderr)
    room_c = rid.group(1) if rid else ""
    check("archi mx: the owner is invited", wait(lambda: owner.sync() and room_c in owner.invites, 20))
    owner.join(room_c)
    owner.text(room_c, "hi created room")
    check("archi mx: the created room is a working conversation",
          wait(lambda: [e for e in bot_msgs(owner, room_c, "m.text") if "hi created room" in e["content"]["body"]], 30))
    res = archi(dict(env, ARCHI_ROOM_ID=room_c), "mx", "room", "rename", "E2E created renamed")
    check("archi mx room rename (current room)", res.returncode == 0 and wait(
        lambda: (conv(room_c) or {}).get("title") == "E2E created renamed", 20), res.stdout + res.stderr)
    res = archi(env, "mx", "send", "**cli** says hi", "--room", room_c, "--notice")
    check("archi mx send --notice", res.returncode == 0 and wait(lambda: [
        e for e in bot_msgs(owner, room_c, "m.notice") if e["content"].get("formatted_body") == "<p><b>cli</b> says hi</p>"], 15))
    res = archi(dict(env, ARCHI_ROOM_ID=room_a), "mx", "room", "rename", "Not allowed")
    check("archi mx room rename refuses without power, clearly", res.returncode == 1 and "may not rename" in res.stderr)
    res = archi(env, "mx", "rooms")
    check("archi mx rooms", res.returncode == 0 and f"mx:{room_c}" in res.stdout and f"mx:{room_a}" in res.stdout
          and "encrypted room(s) were left" in res.stdout, res.stdout)
    res = subprocess.run([PY, "-c", "import sys; from core.config import Config; from doors.matrix import notify; "
                          f"print(notify.post(Config(), 'mx:{room_b}', '🧠 review: **1** lesson'))"],
                         cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    check("notify.post sends a quiet notice", res.stdout.strip() == "True" and wait(lambda: [
        e for e in bot_msgs(owner, room_b, "m.notice") if "review" in e["content"]["body"]], 15), res.stdout + res.stderr)

    # ---- owner leaves a room
    owner.leave(room_b)
    check("lifecycle: owner leaves, the bot follows, archived",
          wait(lambda: (conv(room_b) or {}).get("state") == "deleted", 20)
          and wait(lambda: bot_membership(stranger, room_b) == "leave", 20)
          and '"reason": "owner left"' in (HOME / "state" / "archive.jsonl").read_text())

    # ---- more lifecycle: kicked, encryption switched on later
    room_k = owner.create_room("E2E kick", invite=[BOT])
    wait(lambda: bot_membership(owner, room_k) == "join", 30)
    st, r = call("POST", f"/_matrix/client/v3/rooms/{up.quote(room_k)}/kick", owner.token,
                    {"user_id": BOT, "reason": "e2e"})
    check("lifecycle: the bot kicked, archived as kicked", st == 200 and wait(
        lambda: (conv(room_k) or {}).get("state") == "deleted", 20)
          and '"reason": "kicked"' in (HOME / "state" / "archive.jsonl").read_text())
    room_l = owner.create_room("E2E later encrypted", invite=[BOT])
    wait(lambda: bot_membership(owner, room_l) == "join", 30)
    owner.put(f"/_matrix/client/v3/rooms/{up.quote(room_l)}/state/m.room.encryption/",
              {"algorithm": "m.megolm.v1.aes-sha2"})
    check("encrypted later: explained, left, archived", wait(lambda: bot_membership(owner, room_l) == "leave", 30)
          and any("end-to-end encrypted" in e["content"]["body"] for e in bot_msgs(owner, room_l, "m.text"))
          and (conv(room_l) or {}).get("state") == "deleted")

    # ---- model, provider and session commands through the door
    owner.text(room_c, "!model sonnet")
    check("command: !model probes and switches", wait(lambda: [e for e in bot_msgs(owner, room_c, "m.text")
                                                               if "now uses `sonnet`" in e["content"]["body"]], 30)
          and (conv(room_c) or {}).get("model") == "sonnet")
    owner.text(room_c, "!provider codex")
    wait(lambda: (conv(room_c) or {}).get("provider") == "codex", 20)
    owner.text(room_c, "which agent are you")
    check("command: !provider codex, the next turn runs codex",
          wait(lambda: [e for e in bot_msgs(owner, room_c, "m.text") if e["content"]["body"].startswith("CODEX stdin=")], 30)
          and (conv(room_c) or {}).get("model") == "" and (conv(room_c) or {}).get("sessions", {}).get("codex") == "thread-new")
    owner.text(room_c, "!new")
    check("command: !new drops the sessions", wait(lambda: [e for e in bot_msgs(owner, room_c, "m.text")
                                                            if "New session for this room" in e["content"]["body"]], 20)
          and (conv(room_c) or {}).get("sessions") == {})

    # ---- status and validate
    res = archi(env, "status")
    check("archi status shows the Matrix door without secrets",
          res.returncode == 0 and f"Matrix:   {BOT}" in res.stdout and "encrypted room(s) ignored" in res.stdout
          and token not in res.stdout and "archi-matrix.service" in res.stdout, res.stdout)
    res = archi(env, "validate")
    check("archi validate dry-starts the Matrix door", res.returncode == 0 and "dry start: matrix (door ok" in res.stdout,
          res.stdout[-1500:])

    # ---- the homeserver goes away while an answer is being delivered
    m5 = owner.text(room_a, "fake:slow during an outage")
    wait(lambda: bot_reactions(owner, room_a, m5), 15)
    sh("docker", "stop", "-t", "2", CONTAINER, timeout=60)
    outbox = HOME / "state" / "outbox"
    kept = wait(lambda: list(outbox.glob("matrix--*.json")), 120, 1.0)
    check("outage: the undelivered answer is kept on disk (0600)",
          kept and (kept[0].stat().st_mode & 0o777) == 0o600 and "fake:slow during an outage" in kept[0].read_text())
    sh("docker", "start", CONTAINER, timeout=60)
    assert wait(_up, 60), "homeserver did not come back"
    late = wait(lambda: [e for e in bot_msgs(owner, room_a, "m.text")
                         if e["content"]["body"].startswith("_(Delivered late:")], 120, 2.0)
    answers = [e for e in bot_msgs(owner, room_a, "m.text") if "fake:slow during an outage" in e["content"]["body"]]
    check("outage: delivered late once the server is back, marked as late", late and len(answers) == 1)
    check("outage: the outbox is empty again", wait(lambda: not list(outbox.glob("*.json")), 30))
    check("outage: the owner's message ends with ✅", wait(lambda: ("✅", False) in bot_reactions(owner, room_a, m5), 30))

    # ---- restart in the middle of a turn
    m4 = owner.text(room_a, "fake:hang during restart")
    wait(lambda: bot_reactions(owner, room_a, m4), 15)
    time.sleep(1)
    answers_before = len(bot_msgs(owner, room_a, "m.text"))
    rc = stop_door(door)
    markers = list((HOME / "state" / "running").glob("matrix--*.json"))
    pids = (WORK / "fake.pids").read_text().split() if (WORK / "fake.pids").exists() else []
    alive = [p for p in pids if Path(f"/proc/{p}").exists() and "State:\tZ" not in Path(f"/proc/{p}/status").read_text()]
    check("restart: SIGTERM stops the door cleanly", rc == 0, f"rc={rc}")
    check("restart: the run marker is kept", len(markers) == 1)
    check("restart: the agent's process tree was killed", pids and not alive, f"{pids} alive={alive}")
    door = start_door(env, "2")
    check("restart: the interrupted room is told", wait(lambda: [e for e in bot_msgs(owner, room_a, "m.text")
                                                                 if "interrupted" in e["content"]["body"]], 30))
    time.sleep(5)
    check("restart: nothing is replayed", len(bot_msgs(owner, room_a, "m.text")) == answers_before + 1)
    owner.text(room_a, "after restart")
    check("restart: new messages are answered", wait(lambda: [e for e in bot_msgs(owner, room_a, "m.text")
                                                              if "after restart" in e["content"]["body"]], 30))
    saved = json.loads((HOME / "state" / "matrix.json").read_text())
    check("restart: sync token persisted", saved.get("user_id") == BOT and saved.get("since"))
    rc = stop_door(door)
    check("door: no tracebacks in the logs", rc == 0 and "Traceback" not in "".join(
        (WORK / f"door-{t}.log").read_text() for t in ("1", "2")))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


def main() -> int:
    global HS, WORK, HOME, UNITS, OUT
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=18008, help="local port of the test homeserver")
    ap.add_argument("--keep", action="store_true", help="keep the container, volume and work directory")
    a = ap.parse_args()
    if not Path(PY).exists():
        print(f"no venv at {PY}: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt")
        return 2
    HS = f"http://127.0.0.1:{a.port}"
    WORK = Path(tempfile.mkdtemp(prefix="archi-mx-e2e-"))
    HOME, UNITS, OUT = WORK / "home", WORK / "units", WORK / "out"
    try:
        return run(a.port)
    finally:
        for p in DOOR_PROCESSES:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
        if a.keep:
            print(f"kept: container {CONTAINER}, volume {VOLUME}, work directory {WORK}")
        else:
            sh("docker", "rm", "-f", CONTAINER)
            sh("docker", "volume", "rm", VOLUME)
            shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
