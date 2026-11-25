"""The notifier: Telegram through a local fake Bot API server, Matrix through the optional door module."""
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import doors.telegram.api as tg_api
from core import notify
from core.config import Config

TOKEN = "123456:" + "a" * 35


@pytest.fixture
def bot_api(monkeypatch):
    seen = SimpleNamespace(requests=[], html_fails=False, status=200)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.requests.append((self.path, body))
            if seen.status != 200:
                code, reply = seen.status, {"ok": False, "error_code": seen.status, "description": "Internal Server Error"}
            elif seen.html_fails and body.get("parse_mode") == "HTML":
                code, reply = 400, {"ok": False, "error_code": 400,
                                    "description": "Bad Request: can't parse entities: unexpected end tag"}
            else:
                code, reply = 200, {"ok": True, "result": {"message_id": len(seen.requests)}}
            data = json.dumps(reply).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(tg_api, "API_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    yield seen
    srv.shutdown()
    srv.server_close()


def test_telegram_notice_is_silent_and_goes_to_the_topic(tmp_path, bot_api):
    Config(tmp_path).set("TELEGRAM_BOT_TOKEN", TOKEN)
    assert notify.post("tg:1001:77", "🧠 **Session review**\n· saved: a fact", home=tmp_path) is True
    path, body = bot_api.requests[0]
    assert path == f"/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == 1001 and body["message_thread_id"] == 77 and body["disable_notification"] is True
    assert body["parse_mode"] == "HTML" and "<b>Session review</b>" in body["text"]
    assert notify.post("tg:1001:main", "loud", silent=False, home=tmp_path) is True
    _, body = bot_api.requests[1]
    assert "message_thread_id" not in body and body["disable_notification"] is False


def test_rejected_markup_is_sent_as_plain_text(tmp_path, bot_api):
    Config(tmp_path).set("TELEGRAM_BOT_TOKEN", TOKEN)
    bot_api.html_fails = True
    assert notify.post("tg:-100500:3", "**bold** text", home=tmp_path) is True
    (_, first), (_, second) = bot_api.requests
    assert first["parse_mode"] == "HTML" and "parse_mode" not in second and second["text"] == "bold text"
    assert second["disable_notification"] is True


def test_failures_are_false_never_exceptions(tmp_path, bot_api):
    assert notify.post("tg:1001:77", "no token yet", home=tmp_path) is False
    Config(tmp_path).set("TELEGRAM_BOT_TOKEN", TOKEN)
    bot_api.status = 500
    assert notify.post("tg:1001:77", "server down", home=tmp_path) is False
    assert notify.post("tg:not-a-number:77", "bad key", home=tmp_path) is False
    assert notify.post("xx:1", "unknown door", home=tmp_path) is False
    assert notify.post("tg:1001:77", "   ", home=tmp_path) is False


def test_matrix_goes_through_the_optional_door(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "doors.matrix", raising=False)
    monkeypatch.delitem(sys.modules, "doors.matrix.notify", raising=False)
    assert notify.post("mx:!room:example.org", "hello", home=tmp_path) is False      # door not installed
    calls = []
    pkg = types.ModuleType("doors.matrix")
    mod = types.ModuleType("doors.matrix.notify")
    mod.post = lambda cfg, key, text, silent=True: calls.append((cfg.home, key, text, silent)) or True
    pkg.notify = mod
    monkeypatch.setitem(sys.modules, "doors.matrix", pkg)
    monkeypatch.setitem(sys.modules, "doors.matrix.notify", mod)
    assert notify.post("mx:!room:example.org", "hello", silent=True, home=tmp_path) is True
    assert calls == [(tmp_path.resolve(), "mx:!room:example.org", "hello", True)]

    def broken(cfg, key, text, silent=True):
        raise RuntimeError("homeserver unreachable")

    mod.post = broken
    assert notify.post("mx:!room:example.org", "hello", home=tmp_path) is False



def test_a_notice_never_carries_a_secret(tmp_path, bot_api):
    Config(tmp_path).set("TELEGRAM_BOT_TOKEN", TOKEN)
    leaked = "sk-" + "f" * 30
    assert notify.post("tg:1001:77", f"saved: the key {leaked}", home=tmp_path) is True
    body = bot_api.requests[-1][1]
    assert leaked not in body["text"] and "[secret removed]" in body["text"]
