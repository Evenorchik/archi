"""The ops CLI pieces that work without systemd: markers, restart planning, update, identity, validation,
setup and pairing. Git operations run against real throwaway repositories."""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core import running
from core.config import Config
from doors.telegram.api import TgError
from ops import identity, service, setup, update, validate

ROOT = Path(__file__).resolve().parent.parent
DEAD_PID = 2 ** 22 + 4321


# ------------------------------------------------------------------ systemd unit
def test_unit_text():
    text = service.unit_text("telegram", "/opt/archi", python="/opt/archi/.venv/bin/python", user_home="/root")
    for line in ("WorkingDirectory=/opt/archi", "Environment=ARCHI_HOME=/opt/archi", "Restart=always",
                 "KillSignal=SIGTERM", "ExecStart=/opt/archi/.venv/bin/python -m doors.telegram",
                 "WantedBy=multi-user.target", "Environment=HOME=/root"):
        assert line in text
    assert "/root/.local/bin" in text            # where the official CLI installers put claude / codex


# ------------------------------------------------------------------ run markers
def test_markers_active_and_leftovers(tmp_path):
    live = running.start("telegram", "tg:1:main", tmp_path)
    running.start("telegram", "tg:1:7", tmp_path, pid=DEAD_PID)
    running.start("telegram", "tg:1:8", tmp_path, now=time.time() - 10 * 3600, hard_s=3600)   # past deadline
    running.start("matrix", "mx:room", tmp_path, pid=DEAD_PID)
    assert [m["key"] for m in running.active(tmp_path)] == ["tg:1:main"]
    gone = {m["key"] for m in running.leftovers("telegram", tmp_path)}
    assert gone == {"tg:1:7"}                    # our own live marker and the other door's are kept
    running.finish(live)
    assert running.active(tmp_path) == []


def test_wait_idle_returns_when_the_last_turn_ends(tmp_path):
    marker = running.start("telegram", "tg:1:main", tmp_path)
    threading.Timer(0.3, running.finish, args=[marker]).start()
    notes = []
    assert service.wait_idle(tmp_path, poll=0.05, out=notes.append) is True
    assert notes and "tg:1:main" in notes[0]
    running.start("telegram", "tg:1:main", tmp_path)
    assert service.wait_idle(tmp_path, max_wait=0.2, poll=0.05, out=lambda s: None) is False


def test_restart_from_inside_a_turn_is_deferred(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "have_systemd", lambda: True)
    monkeypatch.setattr(service, "installed_doors", lambda: ["telegram"])
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service, "restart_now", lambda doors: calls.append(("now", doors)) or 0)

    def fake_run(cmd, **kw):
        calls.append(("run", cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    monkeypatch.setenv("ARCHI_CONVERSATION", "tg:1:main")
    assert service.restart(when_idle=False, home=tmp_path) == 0
    kind, cmd = calls[0]
    assert kind == "run" and cmd[0] == "systemd-run" and "--when-idle" in cmd
    assert cmd[cmd.index("--delay") + 1] == str(service.IN_TURN_MIN_DELAY_S)
    assert f"ARCHI_HOME={tmp_path}" in cmd and ("now", ["telegram"]) not in calls

    monkeypatch.delenv("ARCHI_CONVERSATION")
    calls.clear()
    assert service.restart(when_idle=True, home=tmp_path) == 0           # idle: restarts right away
    assert calls == [("now", ["telegram"])]


# ------------------------------------------------------------------ update
def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="you@example.com", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="you@example.com")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """upstream (bare) <- dev (pushes new commits) ; home = the installed checkout."""
    up, dev, home = tmp_path / "up.git", tmp_path / "dev", tmp_path / "home"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(up)], check=True)
    subprocess.run(["git", "clone", "-q", str(up), str(dev)], check=True, capture_output=True)
    git(dev, "checkout", "-q", "-b", "main")
    (dev / "CLAUDE.md").write_text("You are **{{AGENT_NAME}}**, the owner's personal agent.\nrules\n")
    (dev / "app.py").write_text("A = 1\n")
    (dev / ".gitignore").write_text("/config/\n")
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", "init")
    git(dev, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(up), str(home)], check=True, capture_output=True)
    return up, dev, home


def push(dev, name, text, msg):
    (dev / name).write_text(text)
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", msg)
    git(dev, "push", "-q", "origin", "main")


def run_update(home, **kw):
    out = []
    rc = update.update(home, restart=False, validate=False, reinstall=False, out=out.append, **kw)
    return rc, "\n".join(out)


def test_update_fast_forward_keeps_local_changes(repos):
    _up, dev, home = repos
    (home / "CLAUDE.md").write_text("You are **Max**, the owner's personal agent.\nrules\nmine\n")
    (home / "notes.txt").write_text("untracked but not ignored\n")
    (home / "config").mkdir()
    (home / "config" / "archi.env").write_text("SECRET=1\n")
    push(dev, "app.py", "A = 2\n", "upstream change")
    rc, out = run_update(home)
    assert rc == 0, out
    assert (home / "app.py").read_text() == "A = 2\n"
    assert "mine" in (home / "CLAUDE.md").read_text() and (home / "notes.txt").exists()
    assert (home / "config" / "archi.env").read_text() == "SECRET=1\n"
    assert git(home, "stash", "list") == ""
    rc, out = run_update(home)
    assert rc == 0 and "Already up to date" in out


def test_update_rebases_local_commits(repos):
    _up, dev, home = repos
    (home / "skill.md").write_text("my skill\n")
    git(home, "add", "skill.md")
    git(home, "commit", "-q", "-m", "local skill")
    push(dev, "app.py", "A = 3\n", "upstream change")
    rc, out = run_update(home)
    assert rc == 0, out
    assert (home / "app.py").read_text() == "A = 3\n" and (home / "skill.md").exists()
    assert git(home, "log", "-1", "--format=%s") == "local skill"


def test_update_conflict_rolls_back_everything(repos):
    _up, dev, home = repos
    before = git(home, "rev-parse", "HEAD")
    (home / "app.py").write_text("A = 'mine'\n")
    (home / "extra.txt").write_text("keep me\n")
    push(dev, "app.py", "A = 'theirs'\n", "conflicting change")
    rc, out = run_update(home)
    assert rc == 2 and "app.py" in out and "Nothing changed" in out
    assert git(home, "rev-parse", "HEAD") == before
    assert (home / "app.py").read_text() == "A = 'mine'\n" and (home / "extra.txt").read_text() == "keep me\n"
    assert git(home, "stash", "list") == ""


def test_update_rolls_back_when_validation_fails(repos):
    _up, dev, home = repos
    before = git(home, "rev-parse", "HEAD")
    (home / "local.txt").write_text("local\n")
    push(dev, "app.py", "A = 4\n", "breaks validation")

    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1 if "validate" in cmd else 0, "FAIL  imports: boom", "")

    out = []
    rc = update.update(home, restart=False, validate=True, reinstall=False, out=out.append, runner=runner)
    assert rc == 3
    assert git(home, "rev-parse", "HEAD") == before and (home / "app.py").read_text() == "A = 1\n"
    assert (home / "local.txt").read_text() == "local\n" and git(home, "stash", "list") == ""


# ------------------------------------------------------------------ identity
class FakeSyncApi:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def call(self, method, **params):
        self.calls.append((method, params))
        if self.fail:
            raise self.fail
        return True


def test_set_name_updates_config_rulebook_and_bot(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Rules\nYou are **{{AGENT_NAME}}**, the owner's personal agent.\n")
    cfg = Config(tmp_path)
    cfg.set("TELEGRAM_BOT_TOKEN", "123456:" + "a" * 35)
    api = FakeSyncApi()
    assert identity.set_name("  Max  Power ", cfg=cfg, code_dir=tmp_path, api=api, out=lambda s: None) == 0
    assert cfg.get("AGENT_NAME") == "Max Power" and cfg.has_name()
    assert "You are **Max Power**, the owner's" in (tmp_path / "CLAUDE.md").read_text()
    assert api.calls == [("setMyName", {"name": "Max Power"})]
    api = FakeSyncApi(fail=TgError("Too Many Requests: retry after 60", 429, "setMyName", retry_after=60))
    out = []
    assert identity.set_name("Rex", cfg=cfg, code_dir=tmp_path, api=api, out=out.append) == 1
    assert "You are **Rex**, the owner's" in (tmp_path / "CLAUDE.md").read_text() and "60 s" in out[-1]
    assert identity.set_name("", cfg=cfg, code_dir=tmp_path, api=api, out=lambda s: None) == 2


# ------------------------------------------------------------------ validate
def test_check_config(tmp_path, monkeypatch):
    cfg = Config(tmp_path)
    rep = validate.Report(out=lambda s: None)
    validate.check_config(rep, cfg)
    assert rep.failed == 1                                          # no config file
    cfg.update({"TELEGRAM_BOT_TOKEN": "nope", "PROVIDER": "gpt", "CLAUDE_EFFORT": "huge", "OWNER_ID": "x"})
    rep = validate.Report(out=lambda s: None)
    validate.check_config(rep, cfg)
    assert rep.failed == 4
    monkeypatch.setattr(validate.engine, "resolve_binary", lambda name: sys.executable)
    cfg.update({"TELEGRAM_BOT_TOKEN": "123456:" + "a" * 35, "PROVIDER": "claude", "CLAUDE_EFFORT": "",
                "OWNER_ID": "", "PAIRING_CODE": "123456"})
    os.chmod(cfg.path, 0o600)
    rep = validate.Report(out=lambda s: None)
    validate.check_config(rep, cfg)
    assert rep.failed == 0 and rep.warned == 0


def test_validate_modules_cover_the_code():
    mods = validate.modules(ROOT)
    assert {"core.engine", "doors.telegram.bot", "ops.setup", "ops.cli"} <= set(mods)
    assert not any(m.endswith("__main__") for m in mods)


# ------------------------------------------------------------------ setup
def test_find_oauth_token_unwrapped_wrapped_and_framed():
    tok = "sk-ant-oat01-" + "Ab3_" * 22 + "xyzAA"
    assert setup.find_oauth_token(f"\x1b[32m✓ Token created\x1b[0m\r\n\r\n  {tok}\r\n\r\nStore it safely") == tok
    wrapped = f"Your token:\n{tok[:60]}\n{tok[60:]}\n\nUse it as CLAUDE_CODE_OAUTH_TOKEN"
    assert setup.find_oauth_token(wrapped) == tok
    framed = f"│ {tok[:50]} │\n│ {tok[50:]} │\n│ Store this token securely │"
    assert setup.find_oauth_token(framed) == tok
    assert setup.find_oauth_token("Paste code here if prompted >") is None


def test_noninteractive_onboarding_writes_the_config(tmp_path, monkeypatch):
    cfg = Config(tmp_path)
    monkeypatch.setattr(setup, "check_bot_token", lambda t: (True, {"username": "test_bot", "first_name": "T"}))
    monkeypatch.setattr(setup, "check_openai_key", lambda k: True)
    for k, v in {"ARCHI_BOT_TOKEN": "123456:" + "b" * 35, "ARCHI_PROVIDER": "codex", "ARCHI_SKIP_CLI_INSTALL": "1",
                 "ARCHI_SKIP_LOGIN": "1", "ARCHI_OPENAI_KEY": "sk-proj-" + "c" * 40}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("doors.telegram.api.SyncApi.call", lambda self, m, **p: {})
    assert setup.onboard(cfg, ni=True) == 0
    vals = Config(tmp_path).all()
    assert vals["TELEGRAM_BOT_TOKEN"].endswith("b" * 35) and vals["TELEGRAM_BOT_USERNAME"] == "test_bot"
    assert vals["PROVIDER"] == "codex" and vals["OPENAI_API_KEY"].startswith("sk-proj-")
    assert vals["OWNER_ID"] == "" and vals["AGENT_NAME"] == "" and len(vals["PAIRING_CODE"]) == 6
    assert vals["PAIRING_CODE"].isdigit() and (os.stat(cfg.path).st_mode & 0o777) == 0o600
    code = vals["PAIRING_CODE"]
    monkeypatch.setenv("ARCHI_BOT_TOKEN", "999:" + "z" * 35)
    assert setup.onboard(cfg, ni=True) == 0                       # re-run: the config is kept
    assert Config(tmp_path).get("TELEGRAM_BOT_TOKEN").endswith("b" * 35)
    assert Config(tmp_path).get("PAIRING_CODE") == code


def test_voice_key_rejected_is_not_stored(tmp_path, monkeypatch):
    cfg = Config(tmp_path)
    monkeypatch.setattr(setup, "check_openai_key", lambda k: False)
    monkeypatch.setenv("ARCHI_OPENAI_KEY", "sk-proj-" + "d" * 40)
    setup.step_voice(cfg, ni=True)
    assert cfg.get("OPENAI_API_KEY") == ""
    setup.step_voice(cfg, ni=True, remove=True)
    assert cfg.get("OPENAI_API_KEY") == ""


def test_pair_reset_and_wait(tmp_path):
    cfg = Config(tmp_path)
    cfg.update({"OWNER_ID": "42", "TELEGRAM_BOT_USERNAME": "test_bot"})
    out = []
    assert setup.pair(cfg, out=out.append) == 0 and "user 42" in out[0]
    out.clear()
    assert setup.pair(cfg, reset=True, out=out.append) == 0
    code = cfg.get("PAIRING_CODE")
    assert cfg.owner_id() is None and code.isdigit() and len(code) == 6
    assert any(f"https://t.me/test_bot, press Start and send this code: {code}" in line for line in out)
    threading.Timer(0.3, lambda: Config(tmp_path).update({"OWNER_ID": "77", "PAIRING_CODE": ""})).start()
    assert setup.wait_for_owner(cfg, 10, out=out.append, poll=0.05) == 0 and "77" in out[-1]
    assert setup.wait_for_owner(Config(tmp_path / "x"), 0.1, out=lambda s: None, poll=0.05) == 1


# ------------------------------------------------------------------ the command itself
def test_archi_cli_version_and_status(tmp_path):
    env = dict(os.environ, ARCHI_HOME=str(tmp_path))
    res = subprocess.run([sys.executable, str(ROOT / "archi"), "version"], capture_output=True, text=True, env=env,
                         timeout=60)
    assert res.returncode == 0 and res.stdout.startswith("archi ")
    Config(tmp_path).update({"TELEGRAM_BOT_TOKEN": "123456:" + "s" * 35, "PAIRING_CODE": "246810"})
    res = subprocess.run([sys.executable, str(ROOT / "archi"), "status"], capture_output=True, text=True, env=env,
                         timeout=60)
    assert res.returncode == 0 and "not paired yet" in res.stdout
    assert "s" * 35 not in res.stdout and "246810" not in res.stdout            # never prints secrets
