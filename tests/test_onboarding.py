"""Installer-path fixes: resuming an unfinished sign-in, pairing-code revocation, the Codex browser callback,
webhooks, missing CLIs and multi-line pastes."""
import http.server
import os
import pty
import sys
import threading
import time

import pytest

from core.config import Config
from ops import setup
from test_telegram_door import OWNER, dm, go, make_door


def _keep_config(tmp_path, monkeypatch, ready):
    cfg = Config(tmp_path)
    cfg.update({"TELEGRAM_BOT_TOKEN": "123456:" + "b" * 35, "TELEGRAM_BOT_USERNAME": "test_bot"})
    calls = []
    monkeypatch.setattr(setup, "provider_ready", lambda cfg, ni: (ready, "Codex is not installed"))
    monkeypatch.setattr(setup, "step_provider", lambda cfg, ni, provider=None: calls.append(ni) or "codex")
    return cfg, calls


def test_keeping_a_config_resumes_an_unfinished_sign_in(tmp_path, monkeypatch):
    cfg, calls = _keep_config(tmp_path, monkeypatch, ready=False)
    assert setup.onboard(cfg, ni=True) == 0
    assert calls == [True]                      # step 2 ran again instead of "keeping" a dead agent


def test_keeping_a_ready_config_asks_nothing_more(tmp_path, monkeypatch):
    cfg, calls = _keep_config(tmp_path, monkeypatch, ready=True)
    assert setup.onboard(cfg, ni=True) == 0
    assert calls == []


def test_provider_ready_wants_a_new_token_tested(tmp_path, monkeypatch):
    cfg = Config(tmp_path)
    cfg.update({"PROVIDER": "claude", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-old"})
    monkeypatch.setattr(setup, "cli_path", lambda p: "/usr/bin/true")
    monkeypatch.setattr(setup, "test_claude", lambda token: (True, ""))
    assert setup.provider_ready(cfg, ni=True) == (True, "")
    monkeypatch.setenv("ARCHI_CLAUDE_OAUTH_TOKEN", "sk-ant-oat01-new")
    assert setup.provider_ready(cfg, ni=True)[0] is False
    monkeypatch.setattr(setup, "cli_path", lambda p: None)
    assert setup.provider_ready(cfg, ni=False) == (False, "Claude Code is not installed")


def test_a_new_pairing_code_gets_a_fresh_failure_count(tmp_path):
    door, _api = make_door(tmp_path, owner=None, PAIRING_CODE="111111")

    async def scenario():
        for i in range(setup_max()):
            await door.handle_update(dm(str(200000 + i), uid=OWNER, mid=100 + i))
        assert door.cfg.get("PAIRING_CODE") == ""          # revoked by the brute-force guard
        door.cfg.set("PAIRING_CODE", "222222")              # `archi pair`
        await door.handle_update(dm("999999", uid=OWNER, mid=300))   # one typo
        assert door.cfg.get("PAIRING_CODE") == "222222"     # the new code survives it
        await door.handle_update(dm("222222", uid=OWNER, mid=301))

    go(scenario())
    assert door.cfg.owner_id() == OWNER


def setup_max():
    from doors.telegram.bot import PAIR_MAX_FAILURES
    return PAIR_MAX_FAILURES


def test_wait_for_owner_notices_a_revoked_code(tmp_path):
    cfg = Config(tmp_path)
    cfg.update({"PAIRING_CODE": ""})
    out = []
    assert setup.wait_for_owner(cfg, 30, out=out.append, poll=0.01) == 1
    assert "revoked" in out[-1] and "archi pair" in out[-1]


def test_codex_callback_port_comes_from_the_sign_in_link():
    url = ("https://auth.openai.com/oauth/authorize?response_type=code&client_id=x"
           "&redirect_uri=http%3A%2F%2Flocalhost%3A1457%2Fauth%2Fcallback&state=s")
    assert setup.callback_port(url) == 1457
    assert setup.callback_port("https://auth.openai.com/oauth/authorize?state=s") == 1455


def test_codex_callback_is_delivered_and_its_redirect_followed():
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path.startswith("/auth/callback"):
                self.send_response(302)
                self.send_header("Location", "/success")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"signed in")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status = setup.deliver_callback(f"http://127.0.0.1:{srv.server_port}/auth/callback?code=c&state=s")
    finally:
        srv.shutdown()
    assert status == 200 and hits == ["/auth/callback?code=c&state=s", "/success"]
    assert setup.deliver_callback("http://127.0.0.1:9/auth/callback?code=c") is None   # nothing listens


def test_webhook_is_removed_only_when_allowed(tmp_path, monkeypatch):
    calls = []

    def fake_call(self, method, **params):
        calls.append(method)
        return {"url": "https://example.com/hook"} if method == "getWebhookInfo" else True

    monkeypatch.setattr("doors.telegram.api.SyncApi.call", fake_call)
    monkeypatch.setattr(setup, "check_bot_token", lambda t: (True, {"username": "test_bot", "first_name": "T"}))
    monkeypatch.setenv("ARCHI_BOT_TOKEN", "123456:" + "b" * 35)
    with pytest.raises(setup.SetupError):
        setup.step_telegram(Config(tmp_path), ni=True)
    assert "deleteWebhook" not in calls
    monkeypatch.setenv("ARCHI_DELETE_WEBHOOK", "1")
    setup.step_telegram(Config(tmp_path), ni=True)
    assert calls.count("deleteWebhook") == 1


@pytest.mark.parametrize("login", ["login_claude", "login_codex"])
def test_a_missing_cli_is_a_clear_error(tmp_path, monkeypatch, login):
    monkeypatch.setattr(setup, "cli_path", lambda p: None)
    monkeypatch.setattr(setup, "test_claude", lambda token: (False, "no"))
    monkeypatch.setattr(setup, "test_codex", lambda: (False, "no"))
    with pytest.raises(setup.SetupError, match="not installed"):
        getattr(setup, login)(Config(tmp_path), ni=False)


def test_a_wrapped_paste_without_final_newline_is_read_whole():
    pid, fd = pty.fork()
    if pid == 0:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from ops import term
        got = term.ask_secret('token: ', join_paste=True)
        os.write(1, f"RESULT={got}|\n".encode())     # fd 1 is the pty; sys.stdout is pytest's capture
        os._exit(0)
    time.sleep(0.5)
    os.write(fd, b"test-token-AAAAAAAAAAAAAAAAAAAA\nBBBBBBBBBBBB")   # a wrapped token copied from a terminal
    time.sleep(0.8)
    os.write(fd, b"\r")                                              # the user presses Enter
    out, end = b"", time.time() + 6
    while time.time() < end:
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        out += data
        if b"|" in out:
            break
    os.waitpid(pid, 0)
    assert "RESULT=test-token-AAAAAAAAAAAAAAAAAAAABBBBBBBBBBBB|" in out.decode(errors="replace")


def test_the_rest_of_a_triple_ctrl_c_is_swallowed_when_the_screen_ends_on_the_first():
    pid, fd = pty.fork()
    if pid == 0:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from ops import term
        try:
            # a "screen" that ends at the first Ctrl+C, like `claude auth login`
            rc, _ = term.run_captured([sys.executable, "-c",
                                       "import sys,tty; tty.setraw(0); sys.stdin.buffer.read(1)"])
            time.sleep(2.0)            # the setup goes on (a test request) while the user is still pressing
            os.write(1, f"AFTER rc={rc}|\n".encode())
        except KeyboardInterrupt:
            os.write(1, b"CANCELLED|\n")
        os._exit(0)
    time.sleep(1.0)
    for _ in range(3):
        os.write(fd, b"\x03")
        time.sleep(0.6)
    out, end = b"", time.time() + 8
    while time.time() < end:
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        out += data
        if b"|" in out:
            break
    os.waitpid(pid, 0)
    text = out.decode(errors="replace")
    assert "AFTER rc=0|" in text and "CANCELLED" not in text
