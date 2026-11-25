"""The Matrix door's ops side against a fake homeserver over real HTTP: homeserver discovery, `archi setup
matrix`, the `archi mx` agent CLI, doors.matrix.notify, the unit, status and validation. All ids are made up."""
import argparse
import io
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.config import Config
from core.conversations import Registry
from doors.matrix import cli as mx_cli
from doors.matrix import notify
from doors.matrix.api import MxError, SyncApi, resolve_homeserver
from ops import cli as ops_cli
from ops import service, setup, validate

ROOT = Path(__file__).resolve().parent.parent
BOT, OWNER = "@archi:example.org", "@owner:example.org"
TOKEN, PASSWORD = "tok-for-tests", "pw-for-tests"


class FakeHomeserver:
    def __init__(self, *, homeserver=True, well_known=None):
        self.homeserver = homeserver
        self.well_known = well_known
        self.requests = []
        self.encrypted_new_rooms = False
        self.forbid_rename = False
        self.fail_send = False
        hs = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                url = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(url.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else None
                auth = self.headers.get("Authorization", "")
                hs.requests.append((method, path, body, auth, urllib.parse.parse_qs(url.query)))
                status, out = hs.route(method, path, body, auth)
                self._reply(status, out)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def route(self, method, path, body, auth):
        if path == "/.well-known/matrix/client":
            return (200, self.well_known) if self.well_known else (404, {"errcode": "M_NOT_FOUND"})
        if not self.homeserver:
            return 404, {"errcode": "M_UNRECOGNIZED"}
        if path == "/_matrix/client/versions":
            return 200, {"versions": ["v1.11", "v1.12"]}
        if path == "/_matrix/client/v3/login":
            if body.get("password") == PASSWORD and body["identifier"]["user"] in ("archi", BOT):
                return 200, {"user_id": BOT, "access_token": TOKEN, "device_id": "DEV9"}
            return 403, {"errcode": "M_FORBIDDEN", "error": "Invalid username or password"}
        if auth != f"Bearer {TOKEN}":
            return 401, {"errcode": "M_UNKNOWN_TOKEN", "error": "Unknown access token"}
        if path == "/_matrix/client/v3/account/whoami":
            return 200, {"user_id": BOT, "device_id": "DEV9"}
        if path == "/_matrix/client/v3/sync":
            return 200, {"next_batch": "s9", "rooms": {
                "join": {"!plain:example.org": {"state": {"events": [
                    {"type": "m.room.name", "state_key": "", "content": {"name": "Work"}}]}}},
                "invite": {"!enc:example.org": {"invite_state": {"events": [
                    {"type": "m.room.name", "state_key": "", "content": {"name": "Secret"}},
                    {"type": "m.room.encryption", "state_key": "", "content": {"algorithm": "m.megolm.v1.aes-sha2"}}]}}}}}
        if path == "/_matrix/client/v3/createRoom":
            return 200, {"room_id": "!made:example.org"}
        if path.endswith("/state/m.room.encryption/"):
            if self.encrypted_new_rooms:
                return 200, {"algorithm": "m.megolm.v1.aes-sha2"}
            return 404, {"errcode": "M_NOT_FOUND", "error": "Event not found."}
        if "/state/m.room.name/" in path:
            if self.forbid_rename:
                return 403, {"errcode": "M_FORBIDDEN", "error": "Event is not authorized."}
            return 200, {"event_id": "$name"}
        if "/send/m.room.message/" in path:
            if self.fail_send:
                return 500, {"errcode": "M_UNKNOWN", "error": "boom"}
            return 200, {"event_id": "$sent"}
        return 404, {"errcode": "M_UNRECOGNIZED", "error": path}

    def calls(self, needle):
        return [r for r in self.requests if needle in r[1]]


@pytest.fixture
def hs():
    server = FakeHomeserver()
    yield server
    server.close()


def configured(tmp_path, hs_url, **extra):
    cfg = Config(tmp_path)
    cfg.update({"MATRIX_HOMESERVER": hs_url, "MATRIX_USER_ID": BOT, "MATRIX_ACCESS_TOKEN": TOKEN,
                "MATRIX_OWNER_ID": OWNER, **extra})
    return cfg


# ------------------------------------------------------------------ discovery and login
def test_resolve_follows_well_known_delegation(hs):
    front = FakeHomeserver(homeserver=False, well_known={"m.homeserver": {"base_url": hs.url + "/"}})
    try:
        assert resolve_homeserver(front.url) == (hs.url, True)
        assert resolve_homeserver(hs.url) == (hs.url, False)              # no .well-known: the address itself
        dead = FakeHomeserver(homeserver=False, well_known={"m.homeserver": {"base_url": front.url}})
        try:
            with pytest.raises(MxError) as err:
                resolve_homeserver(dead.url)
            assert dead.url in err.value.error and front.url in err.value.error   # both candidates named
        finally:
            dead.close()
    finally:
        front.close()


def test_login_and_whoami(hs):
    api = SyncApi(hs.url)
    res = setup.matrix_login(api, "archi", PASSWORD)
    assert res == {"user_id": BOT, "access_token": TOKEN, "device_id": "DEV9"}
    assert hs.calls("/login")[0][2]["initial_device_display_name"] == "Archi"
    with pytest.raises(MxError) as err:
        api.login("archi", "wrong")
    assert err.value.errcode == "M_FORBIDDEN" and err.value.status == 403
    with pytest.raises(MxError) as err:
        SyncApi(hs.url, "bad-token").whoami()
    assert err.value.fatal
    rooms = {r["room_id"]: r for r in SyncApi(hs.url, TOKEN).rooms()}
    assert rooms["!plain:example.org"] == {"room_id": "!plain:example.org", "name": "Work", "membership": "join",
                                           "encrypted": False}
    assert rooms["!enc:example.org"]["encrypted"] is True and rooms["!enc:example.org"]["membership"] == "invite"


# ------------------------------------------------------------------ archi setup matrix
@pytest.fixture
def units(tmp_path, monkeypatch):
    """A unit directory systemd never reads; systemctl must not be called for it."""
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(service, "UNIT_DIR", unit_dir)
    monkeypatch.setattr(service, "have_systemd", lambda: True)

    def no_systemctl(*args, **kw):
        raise AssertionError(f"systemctl called: {args}")

    monkeypatch.setattr(service, "systemctl", no_systemctl)
    return unit_dir


def test_noninteractive_setup_signs_in_and_never_stores_the_password(tmp_path, hs, units, monkeypatch, capsys):
    front = FakeHomeserver(homeserver=False, well_known={"m.homeserver": {"base_url": hs.url}})
    try:
        for k, v in {"ARCHI_MATRIX_HOMESERVER": front.url, "ARCHI_MATRIX_USER": "archi",
                     "ARCHI_MATRIX_PASSWORD": PASSWORD, "ARCHI_MATRIX_OWNER": OWNER}.items():
            monkeypatch.setenv(k, v)
        home = tmp_path / "home"
        cfg = Config(home)
        setup.ensure_defaults(cfg)
        setup.step_matrix(cfg, ni=True)
    finally:
        front.close()
    vals = Config(home).all()
    assert vals["MATRIX_HOMESERVER"] == hs.url and vals["MATRIX_USER_ID"] == BOT
    assert vals["MATRIX_ACCESS_TOKEN"] == TOKEN and vals["MATRIX_DEVICE_ID"] == "DEV9" and vals["MATRIX_OWNER_ID"] == OWNER
    assert PASSWORD not in cfg.path.read_text() and (os.stat(cfg.path).st_mode & 0o777) == 0o600
    unit = (units / "archi-matrix.service").read_text()
    assert "-m doors.matrix" in unit and f"ARCHI_HOME={home}" in unit
    out = capsys.readouterr().out
    assert "ENCRYPTED" in out and "WITHOUT encryption" in out and BOT in out and "not started" in out
    assert TOKEN not in out


def test_setup_with_a_token_and_bad_answers(tmp_path, hs, units, monkeypatch):
    monkeypatch.setenv("ARCHI_MATRIX_HOMESERVER", hs.url)
    monkeypatch.setenv("ARCHI_MATRIX_TOKEN", TOKEN)
    monkeypatch.setenv("ARCHI_MATRIX_OWNER", "not-an-mxid")
    cfg = Config(tmp_path)
    with pytest.raises(setup.SetupError, match="not a Matrix user id"):
        setup.step_matrix(cfg, ni=True)
    monkeypatch.setenv("ARCHI_MATRIX_OWNER", BOT)
    with pytest.raises(setup.SetupError, match="not the bot's"):
        setup.step_matrix(cfg, ni=True)
    monkeypatch.setenv("ARCHI_MATRIX_OWNER", OWNER)
    setup.step_matrix(cfg, ni=True)
    assert cfg.get("MATRIX_ACCESS_TOKEN") == TOKEN and cfg.get("MATRIX_DEVICE_ID") == "DEV9"
    monkeypatch.delenv("ARCHI_MATRIX_TOKEN")
    monkeypatch.setenv("ARCHI_MATRIX_USER", "archi")
    monkeypatch.setenv("ARCHI_MATRIX_PASSWORD", "wrong")
    with pytest.raises(setup.SetupError, match="could not sign in"):
        setup.step_matrix(cfg, ni=True)
    monkeypatch.setenv("ARCHI_MATRIX_HOMESERVER", "http://127.0.0.1:9")
    with pytest.raises(setup.SetupError, match="no Matrix homeserver answered"):
        setup.step_matrix(cfg, ni=True)


# ------------------------------------------------------------------ archi mx
def mx(argv, env=None):
    ap = argparse.ArgumentParser()
    mx_cli.add_parser(ap.add_subparsers(dest="cmd"))
    args = ap.parse_args(["mx", *argv])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mx_cli.run(args)
    return rc, out.getvalue(), err.getvalue()


def test_archi_mx_room_create_rename_send_and_list(tmp_path, hs, monkeypatch):
    monkeypatch.setenv("ARCHI_HOME", str(tmp_path))
    configured(tmp_path, hs.url)
    rc, out, _ = mx(["room", "create", "Research"])
    assert rc == 0 and "!made:example.org" in out and "conversation mx:!made:example.org" in out
    body = hs.calls("/createRoom")[0][2]
    assert body["preset"] == "trusted_private_chat" and body["invite"] == [OWNER] and body["name"] == "Research"
    assert "initial_state" not in body                         # no encryption event, ever
    conv = Registry(tmp_path / "state").get("mx:!made:example.org")
    assert conv.title == "Research" and conv.title_confirmed and conv.state == "open"

    monkeypatch.setenv("ARCHI_ROOM_ID", "!made:example.org")
    rc, out, _ = mx(["room", "rename", "Deep research"])
    assert rc == 0 and hs.calls("/state/m.room.name/")[0][2] == {"name": "Deep research"}
    assert Registry(tmp_path / "state").get("mx:!made:example.org").title == "Deep research"
    hs.forbid_rename = True
    rc, _out, err = mx(["room", "rename", "Nope"])
    assert rc == 1 and "may not rename" in err

    rc, out, _ = mx(["send", "**hi** there", "--notice"])
    sent = hs.calls("/send/m.room.message/")[-1]
    assert rc == 0 and "!made:example.org" in sent[1] and sent[2]["msgtype"] == "m.notice"
    assert sent[2]["formatted_body"] == "<p><b>hi</b> there</p>" and sent[2]["body"] == "**hi** there"
    rc, out, _ = mx(["rooms"])
    assert rc == 0 and "mx:!made:example.org\topen" in out and "(this room)" in out

    hs.encrypted_new_rooms = True
    rc, _out, err = mx(["room", "create", "Forced"])
    assert rc == 1 and "encryption" in err


def test_archi_mx_needs_a_room_and_a_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHI_HOME", str(tmp_path))
    monkeypatch.delenv("ARCHI_ROOM_ID", raising=False)
    with pytest.raises(SystemExit, match="no room"):
        mx(["send", "x"])
    with pytest.raises(SystemExit, match="not set up"):
        mx(["send", "x", "--room", "!r:example.org"])


def test_archi_command_has_mx(tmp_path):
    env = dict(os.environ, ARCHI_HOME=str(tmp_path))
    res = subprocess.run([sys.executable, str(ROOT / "archi"), "mx", "rooms"], capture_output=True, text=True,
                         env=env, timeout=60)
    assert res.returncode == 0 and "no known rooms" in res.stdout


# ------------------------------------------------------------------ notify
def test_notify_post_sends_a_notice_and_never_raises(tmp_path, hs):
    cfg = configured(tmp_path, hs.url)
    assert notify.post(cfg, "mx:!room:example.org", "🧠 Session review: **2** lessons saved") is True
    method, path, body, auth, _q = hs.calls("/send/m.room.message/")[-1]
    assert method == "PUT" and "!room:example.org" in path and auth == f"Bearer {TOKEN}"
    assert body["msgtype"] == "m.notice" and "<b>2</b>" in body["formatted_body"]
    assert notify.post(cfg, "mx:!room:example.org", "loud", silent=False) is True
    assert hs.calls("/send/m.room.message/")[-1][2]["msgtype"] == "m.text"
    assert notify.post(cfg, "tg:1:main", "x") is False and notify.post(cfg, "", "x") is False
    assert notify.post(cfg, "mx:!room:example.org", "   ") is False
    hs.fail_send = True
    assert notify.post(cfg, "mx:!room:example.org", "x") is False
    assert notify.post(Config(tmp_path / "empty"), "mx:!room:example.org", "x") is False


# ------------------------------------------------------------------ service, status, validate
def test_matrix_unit_and_foreign_unit_dirs(tmp_path, units):
    assert "matrix" in service.DOORS
    text = service.unit_text("matrix", "/opt/archi", python="/opt/archi/.venv/bin/python", user_home="/root")
    assert "ExecStart=/opt/archi/.venv/bin/python -m doors.matrix" in text and "Archi agent: Matrix door" in text
    assert service.systemd_usable() is False
    assert service.install_unit("matrix", home=tmp_path) == 0 and (units / "archi-matrix.service").exists()
    assert service.restart(home=tmp_path) == 1                 # never restarts the host's units of that name
    cfg = Config(tmp_path)
    assert service.configured_doors(cfg) == ["telegram"]
    cfg.set("MATRIX_ACCESS_TOKEN", TOKEN)
    assert service.configured_doors(cfg) == ["telegram", "matrix"]


def test_status_shows_the_matrix_door_without_secrets(tmp_path, hs):
    cfg = configured(tmp_path, hs.url)
    assert ops_cli.matrix_status(Config(tmp_path / "none")) == ["Matrix:   not set up (optional: archi setup matrix)"]
    reg = Registry(tmp_path / "state")
    reg.upsert("mx:!a:example.org", title="A")
    reg.upsert("mx:!b:example.org", title="B")
    reg.upsert("tg:1:main", title="main")
    reg.update_chat("!c:example.org", door="mx", encrypted=True)
    lines = "\n".join(ops_cli.matrix_status(cfg))
    assert BOT in lines and OWNER in lines and hs.url in lines and "rooms: 2" in lines
    assert "1 encrypted room(s) ignored" in lines and TOKEN not in lines
    env = dict(os.environ, ARCHI_HOME=str(tmp_path))
    res = subprocess.run([sys.executable, str(ROOT / "archi"), "status"], capture_output=True, text=True, env=env,
                         timeout=60)
    assert res.returncode == 0 and "Matrix:   " + BOT in res.stdout and TOKEN not in res.stdout


def test_validate_matrix_config(tmp_path):
    cfg = Config(tmp_path)
    rep = validate.Report(out=lambda s: None)
    validate.check_matrix_config(rep, cfg)
    assert rep.failed == 0 and rep.warned == 0                 # not set up: nothing to check
    cfg.update({"MATRIX_HOMESERVER": "matrix.example.org", "MATRIX_USER_ID": "archi", "MATRIX_OWNER_ID": "me"})
    rep = validate.Report(out=lambda s: None)
    validate.check_matrix_config(rep, cfg)
    assert rep.failed == 4
    cfg.update({"MATRIX_HOMESERVER": "https://matrix.example.org", "MATRIX_USER_ID": BOT,
                "MATRIX_ACCESS_TOKEN": TOKEN, "MATRIX_OWNER_ID": ""})
    rep = validate.Report(out=lambda s: None)
    validate.check_matrix_config(rep, cfg)
    assert rep.failed == 0 and rep.warned == 1


def test_matrix_dry_start(tmp_path):
    env = dict(os.environ, ARCHI_HOME=str(tmp_path), PYTHONPATH=str(ROOT))
    py = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable
    res = subprocess.run([py, "-m", "doors.matrix", "--check"], capture_output=True, text=True, env=env, cwd=ROOT,
                         timeout=60)
    assert res.returncode == 1 and "MATRIX_HOMESERVER is not set" in res.stdout
    configured(tmp_path, "https://matrix.example.org")
    res = subprocess.run([py, "-m", "doors.matrix", "--check"], capture_output=True, text=True, env=env, cwd=ROOT,
                         timeout=60)
    assert res.returncode == 0 and "door ok" in res.stdout and TOKEN not in res.stdout
