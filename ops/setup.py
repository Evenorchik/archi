"""`archi setup`: the onboarding the installer runs, and each of its steps on its own.

  archi setup                    everything: Telegram bot, provider CLI + login, voice key, config
  archi setup telegram           the bot token (checked with getMe)
  archi setup provider [NAME]    install the Claude Code or Codex CLI, sign in, check it with a real request
  archi setup voice              add, replace or remove the OpenAI key for voice notes
  archi pair [--reset] [--wait]  the one-time code that makes a Telegram account the owner

Interactive by default: answers come from the terminal and secrets are never echoed. With
ARCHI_NONINTERACTIVE=1 (or --noninteractive) nothing is asked and the answers come from the environment:
  ARCHI_BOT_TOKEN, ARCHI_PROVIDER (claude|codex), ARCHI_OPENAI_KEY, ARCHI_CLAUDE_OAUTH_TOKEN,
  ARCHI_SKIP_CLI_INSTALL=1, ARCHI_SKIP_LOGIN=1, ARCHI_RECONFIGURE=1 (replace an existing config).

Provider CLIs are installed with their vendors' official standalone installers and signed in headlessly:
  * Claude Code: `claude setup-token` (browser link + pasted code, prints a one-year token that Archi keeps
    as CLAUDE_CODE_OAUTH_TOKEN); if that token fails a test request, the regular `claude auth login`.
  * Codex: `codex login --device-auth` (link + one-time code); if that is unavailable, the browser flow,
    with the final localhost:1455 address pasted back and delivered to the waiting login server here.
"""
from __future__ import annotations

import os
import re
import secrets
import select
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from core import engine
from core.config import PROVIDERS, Config, atomic_write
from core.status import clock

from . import term
from .term import Aborted, ask_choice, ask_secret, ask_yes, say

CLI = {
    "claude": {"title": "Claude Code", "url": "https://claude.ai/install.sh", "shell": "bash",
               "env": {"CLAUDE_INSTALL_ALLOW_SUDO": "1"}},     # Archi runs as root on purpose
    "codex": {"title": "Codex", "url": "https://chatgpt.com/codex/install.sh", "shell": "sh",
              "env": {"CODEX_NON_INTERACTIVE": "1"}},
}
PROVIDER_CHOICES = [("claude", "Claude: Claude Code with a Claude Pro or Max subscription"),
                    ("codex", "ChatGPT: Codex with a ChatGPT Plus or Pro subscription")]
BOT_TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,}$")
OAUTH_TOKEN_RE = re.compile(r"sk-ant-oat\d{2}-[A-Za-z0-9_-]{20,}")
TOKEN_CHARS_RE = re.compile(r"^[A-Za-z0-9_-]+$")
BORDER = " \t│┃|"
TEST_PROMPT = "Reply with exactly: OK"
CONFIG_HEADER = (
    "# Archi configuration: KEY=VALUE, one per line. This file holds secrets: keep it mode 0600.\n"
    "# Change values with `archi config set KEY VALUE`; the door reads them on the next message.\n"
    "# Empty CLAUDE_MODEL / CODEX_MODEL = the CLI's own default model.\n"
)


class SetupError(Exception):
    pass


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def root_home() -> str:
    return os.path.expanduser("~root") or "/root"


def cli_env(drop_token: bool = False, **extra: str) -> dict:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env["HOME"] = env.get("HOME") or root_home()
    local_bin = os.path.join(env["HOME"], ".local", "bin")
    if local_bin not in env.get("PATH", "").split(":"):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
    if drop_token:
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env.update(extra)
    return env


# =========================================================================================== checks

def check_bot_token(token: str) -> tuple[bool | None, dict | str]:
    """-> (True, getMe result) | (False, Telegram's reason) | (None, network problem)."""
    from doors.telegram.api import SyncApi, TgError
    try:
        return True, SyncApi(token, timeout=20).call("getMe")
    except TgError as e:
        return (None if e.code == 0 else False), e.description


def check_openai_key(key: str) -> bool | None:
    """True = the key works, False = rejected, None = could not check (network or other errors)."""
    req = urllib.request.Request("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code in (401, 403) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def new_pairing_code() -> str:
    return f"{secrets.randbelow(10 ** 6):06d}"


def cli_path(provider: str) -> str | None:
    binary = engine.resolve_binary(provider)
    return binary if os.path.isabs(binary) and os.path.exists(binary) else None


def cli_version(provider: str) -> str:
    binary = cli_path(provider)
    if not binary:
        return ""
    try:
        res = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30,
                             env=cli_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (res.stdout.strip().splitlines() or [""])[0] if res.returncode == 0 else ""


def find_oauth_token(text: str) -> str | None:
    """The token `claude setup-token` printed. Its renderer wraps a long token onto the next line(s) of a
    narrow terminal, so a match that runs to the end of its line takes the following token-only lines."""
    lines = term.strip_ansi(text).split("\n")
    found = None
    for i, raw in enumerate(lines):
        line = raw.strip(BORDER)
        for m in OAUTH_TOKEN_RE.finditer(line):
            token = m.group(0)
            if m.end() == len(line):
                for nxt in lines[i + 1:]:
                    piece = nxt.strip(BORDER)
                    if not piece or not TOKEN_CHARS_RE.match(piece) or len(token) + len(piece) > 300:
                        break
                    token += piece
            found = token          # the last one wins: it is from the final screen
    return found


def find_login_url(text: str) -> str | None:
    """The OAuth link `codex login` prints (it waits for the redirect on 127.0.0.1:1455)."""
    for m in re.finditer(r"https://\S+", text):
        if "authorize" in m.group(0) or "oauth" in m.group(0):
            return m.group(0)
    return None


def test_claude(token: str | None) -> tuple[bool, str]:
    """One real request, the way a turn makes it: with the token when given, else with the CLI's own
    login. Run in an empty directory so no CLAUDE.md steers the answer."""
    binary = cli_path("claude")
    if not binary:
        return False, "the claude CLI is not installed"
    env = cli_env(drop_token=True, **({"CLAUDE_CODE_OAUTH_TOKEN": token} if token else {}))
    with tempfile.TemporaryDirectory(prefix="archi-check-") as d:
        try:
            res = subprocess.run([binary, "-p", TEST_PROMPT], cwd=d, env=env, capture_output=True, text=True,
                                 timeout=180, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, "no answer within 3 minutes"
    answer = res.stdout.strip()
    if res.returncode == 0 and answer:
        return True, answer[:200]
    return False, ((res.stderr or res.stdout).strip() or f"exit code {res.returncode}")[-400:]


def test_codex() -> tuple[bool, str]:
    binary = cli_path("codex")
    if not binary:
        return False, "the codex CLI is not installed"
    env = cli_env()
    try:
        st = subprocess.run([binary, "login", "status"], capture_output=True, text=True, timeout=30, env=env,
                            stdin=subprocess.DEVNULL)
        if st.returncode != 0:
            return False, (st.stdout + st.stderr).strip() or "not logged in"
        with tempfile.TemporaryDirectory(prefix="archi-check-") as d:
            res = subprocess.run([binary, "exec", "--skip-git-repo-check", TEST_PROMPT], cwd=d, env=env,
                                 capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "no answer within 4 minutes"
    answer = res.stdout.strip()
    if res.returncode == 0 and answer:
        return True, answer.splitlines()[-1][:200]
    return False, ((res.stderr or res.stdout).strip() or f"exit code {res.returncode}")[-400:]


# =========================================================================================== steps

def step_telegram(cfg: Config, ni: bool) -> None:
    if ni:
        token = os.environ.get("ARCHI_BOT_TOKEN", "").strip() or cfg.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise SetupError("ARCHI_BOT_TOKEN is not set")
        ok, info = check_bot_token(token)
        if not ok:
            raise SetupError(f"Telegram did not accept the bot token: {info}")
    else:
        say("Create a bot with @BotFather in Telegram (send it /newbot), then paste the token it gives you.")
        for _ in range(5):
            token = ask_secret("Bot token (input hidden): ")
            if not BOT_TOKEN_RE.match(token):
                say("  That does not look like a bot token (digits, a colon, then letters). Try again.")
                continue
            ok, info = check_bot_token(token)
            if ok:
                break
            say(f"  Telegram did not accept it: {info}" if ok is False else f"  Could not reach Telegram: {info}")
        else:
            raise SetupError("no working bot token")
    me = info if isinstance(info, dict) else {}
    cfg.update({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_BOT_USERNAME": str(me.get("username") or "")})
    say(f"  ✓ Bot @{me.get('username')} ({me.get('first_name', '')})")
    from doors.telegram.api import SyncApi, TgError
    try:
        hook = SyncApi(token, timeout=20).call("getWebhookInfo") or {}
        if hook.get("url"):
            say("  ! This bot has a webhook set (another service uses it?). Archi polls for updates and cannot "
                "work while a webhook is set: remove it with deleteWebhook, or use a new bot.")
    except TgError:
        pass


def choose_provider(cfg: Config, ni: bool, provider: str | None = None) -> str:
    current = cfg.get("PROVIDER") if cfg.get("PROVIDER") in PROVIDERS else "claude"
    if provider is None:
        if ni:
            provider = (os.environ.get("ARCHI_PROVIDER") or current).strip().lower()
        else:
            say("Which subscription will the agent run on?")
            provider = ask_choice("Choose 1 or 2:", PROVIDER_CHOICES, current)
    if provider not in PROVIDERS:
        raise SetupError(f"unknown provider {provider!r} (claude or codex)")
    cfg.set("PROVIDER", provider)
    return provider


def install_cli(provider: str, *, force: bool = False) -> None:
    info = CLI[provider]
    if cli_path(provider) and not force:
        say(f"  ✓ {info['title']} is installed: {cli_version(provider) or cli_path(provider)}")
        return
    say(f"  Installing {info['title']} with its official installer ({info['url']}); this takes a minute …")
    env = cli_env(**info["env"])
    cmd = ["bash", "-c", f"set -o pipefail; curl -fsSL {info['url']} | {info['shell']}"]
    try:
        res = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, timeout=900)
    except subprocess.TimeoutExpired:
        raise SetupError(f"the {info['title']} installer did not finish in 15 minutes") from None
    version = cli_version(provider)
    if res.returncode != 0 or not version:
        raise SetupError(f"the {info['title']} installer failed (exit code {res.returncode}); see its output above")
    say(f"  ✓ {info['title']} installed: {version} ({cli_path(provider)})")


def login_claude(cfg: Config, ni: bool) -> None:
    configured = cfg.get("CLAUDE_CODE_OAUTH_TOKEN") or None
    if ni:
        token = os.environ.get("ARCHI_CLAUDE_OAUTH_TOKEN", "").strip() or configured
        ok, why = test_claude(token)
        if not ok:
            raise SetupError("Claude Code is not signed in or the token does not work "
                             f"({why}); set ARCHI_CLAUDE_OAUTH_TOKEN to a `claude setup-token` token")
        if token:
            cfg.set("CLAUDE_CODE_OAUTH_TOKEN", token)
        say("  ✓ Claude answered a test request")
        return
    ok, _why = test_claude(configured)
    if ok and not ask_yes("  Claude Code is already signed in (a test request works). Sign in again?", False):
        return
    binary = cli_path("claude")
    say("Sign in with your Claude subscription. `claude setup-token` shows a link: open it in a browser on any\n"
        "device, approve, and paste the code you get back here. It then prints a token valid for one year,\n"
        "which Archi keeps in config/archi.env.")
    token = None
    try:
        _rc, shown = term.run_captured([binary, "setup-token"], env=cli_env(drop_token=True))
        token = find_oauth_token(shown)
    except OSError as e:
        say(f"  could not run claude setup-token: {e}")
    if not token:
        say("  I could not read the token from the screen.")
        token = ask_secret("  Paste the token it printed (starts with sk-ant-oat; hidden; Enter to skip): ",
                           join_paste=True) or None
    if token:
        say("  Checking the token with a test request …")
        ok, why = test_claude(token)
        if ok:
            cfg.set("CLAUDE_CODE_OAUTH_TOKEN", token)
            say("  ✓ Claude answered; the token is saved")
            return
        say(f"  ✗ The token did not work: {why}")
    say("Trying the regular sign-in instead (claude auth login): it also shows a link; open it, sign in and\n"
        "paste the code back here.")
    subprocess.run([binary, "auth", "login", "--claudeai"], env=cli_env(drop_token=True))
    ok, why = test_claude(None)
    if not ok:
        raise SetupError(f"Claude Code still does not answer: {why}")
    cfg.set("CLAUDE_CODE_OAUTH_TOKEN", "")      # the CLI's own login is used; a token would override it
    say("  ✓ Claude answered a test request")


def codex_browser_login(binary: str) -> bool:
    """`codex login` waits for its browser redirect on 127.0.0.1:1455 of THIS machine. On a server the
    user's browser cannot reach it, so the final address is pasted here and delivered with curl."""
    proc = subprocess.Popen([binary, "login"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=cli_env())
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    seen, url, end = b"", None, time.time() + 30
    while url is None and time.time() < end:
        if select.select([fd], [], [], 0.5)[0]:
            chunk = os.read(fd, 65536)      # raw reads: a buffered readline would hide lines from select()
            if not chunk:
                break
            seen += chunk
            url = find_login_url(term.strip_ansi(seen.decode("utf-8", "replace")))
    if not url:
        proc.kill()
        say("  codex login did not print a sign-in link:\n" + seen.decode("utf-8", "replace")[-500:])
        return False
    say("Open this link in a browser on any device and sign in with your ChatGPT account:\n")
    say(url + "\n")
    say("At the end the browser opens a page on 127.0.0.1:1455 (or localhost:1455) that fails to load. That is\n"
        "expected: copy the full address from the address bar and paste it here.")
    try:
        for _ in range(3):
            pasted = ask_secret("Address of that page (hidden): ", join_paste=True)
            parts = urlsplit(pasted)
            if parts.port == 1455 and parts.path.startswith("/auth/callback") and "code=" in parts.query:
                break
            say("  That is not it: the address starts with http://127.0.0.1:1455/auth/callback?code=")
        else:
            proc.kill()
            return False
        local = urlunsplit(("http", "127.0.0.1:1455", parts.path, parts.query, ""))
        subprocess.run(["curl", "-sS", "--max-time", "30", "-o", "/dev/null", local], timeout=40)
        rest, _ = proc.communicate(timeout=90)
        out = (seen + (rest or b"")).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        say("  codex login did not finish after the address was delivered")
        return False
    finally:
        if proc.poll() is None:
            proc.kill()
    tail = term.strip_ansi(out).strip().splitlines()[-2:]
    if tail:
        say("  " + "\n  ".join(tail))
    return proc.returncode == 0


def login_codex(cfg: Config, ni: bool) -> None:
    ok, why = test_codex()
    if ni:
        if not ok:
            raise SetupError(f"Codex is not signed in ({why}); sign in with `codex login --device-auth`, "
                             "then run `archi setup provider codex`")
        say("  ✓ Codex answered a test request")
        return
    if ok and not ask_yes("  Codex is already signed in (a test request works). Sign in again?", False):
        return
    binary = cli_path("codex")
    helptext = subprocess.run([binary, "login", "--help"], capture_output=True, text=True, timeout=30,
                              env=cli_env()).stdout
    done = False
    if "--device-auth" in helptext:
        say("Sign in with your ChatGPT account using a one-time code: Codex shows a link and a code; open the\n"
            "link on any device and enter the code. Device-code sign-in must be enabled once in ChatGPT\n"
            "(Settings > Security, or by your workspace admin). If the page refuses the code, press Ctrl+C\n"
            "here to sign in with a browser link instead.")
        try:
            done = subprocess.run([binary, "login", "--device-auth"], env=cli_env()).returncode == 0
        except KeyboardInterrupt:
            say("\n  Device-code sign-in cancelled.")
    if not done:
        say("Signing in with a browser link instead.")
        done = codex_browser_login(binary)
    ok, why = test_codex()
    if not ok:
        raise SetupError(f"Codex does not answer: {why}")
    say("  ✓ Codex answered a test request")


def step_provider(cfg: Config, ni: bool, provider: str | None = None) -> str:
    provider = choose_provider(cfg, ni, provider)
    if env_flag("ARCHI_SKIP_CLI_INSTALL"):
        say(f"  Skipping the {CLI[provider]['title']} install (ARCHI_SKIP_CLI_INSTALL)")
    else:
        install_cli(provider)
    if env_flag("ARCHI_SKIP_LOGIN"):
        say("  Skipping the sign-in (ARCHI_SKIP_LOGIN)")
    elif provider == "claude":
        login_claude(cfg, ni)
    else:
        login_codex(cfg, ni)
    return provider


def step_voice(cfg: Config, ni: bool, *, remove: bool = False, key_from_stdin: bool = False) -> None:
    from core.voice import looks_like_openai_key
    if remove:
        cfg.set("OPENAI_API_KEY", "")
        say("Voice key removed: voice notes are off.")
        return
    if ni or key_from_stdin:
        key = (sys.stdin.readline() if key_from_stdin else os.environ.get("ARCHI_OPENAI_KEY", "")).strip()
        if not key:
            say("  No OpenAI key given (ARCHI_OPENAI_KEY): voice notes stay off; add one later with "
                "`archi setup voice`.")
            return
        verdict = check_openai_key(key)
        if verdict is False:
            say("  ! OpenAI rejected the key; it was not saved. Voice notes stay off.")
            return
        cfg.set("OPENAI_API_KEY", key)
        say("  ✓ OpenAI key saved" + ("" if verdict else " (not verified: OpenAI could not be reached)"))
        return
    current = cfg.get("OPENAI_API_KEY")
    say("With an OpenAI API key, voice messages are transcribed (and the agent can answer with voice).\n"
        "Press Enter to skip; you can add it later with `archi setup voice` or by sending the key to the bot.")
    for _ in range(3):
        key = ask_secret("OpenAI API key (hidden; Enter to " + ("keep the current one" if current else "skip") + "): ")
        if not key:
            return
        if not looks_like_openai_key(key):
            say("  That does not look like an OpenAI API key (it starts with sk-).")
            continue
        verdict = check_openai_key(key)
        if verdict:
            cfg.set("OPENAI_API_KEY", key)
            say("  ✓ The key works: voice notes are on")
            return
        if verdict is False:
            say("  ✗ OpenAI rejected this key.")
            continue
        if ask_yes("  OpenAI could not be reached to check it. Save it anyway?", False):
            cfg.set("OPENAI_API_KEY", key)
            return


def ensure_defaults(cfg: Config) -> None:
    """Every key the door reads is present (empty = default); a pairing code exists until there is an owner."""
    if not cfg.path.exists():
        cfg.dir.mkdir(parents=True, exist_ok=True)
        atomic_write(cfg.path, CONFIG_HEADER, mode=0o600)
    os.chmod(cfg.dir, 0o700)
    have = cfg.all()
    wanted = [("PROVIDER", "claude"), ("CLAUDE_MODEL", ""), ("CLAUDE_EFFORT", ""), ("CODEX_MODEL", ""),
              ("CLAUDE_CODE_OAUTH_TOKEN", ""), ("OPENAI_API_KEY", ""), ("OWNER_ID", ""), ("AGENT_NAME", "")]
    changes: dict[str, str | None] = {k: v for k, v in wanted if k not in have}
    if not have.get("OWNER_ID") and not have.get("PAIRING_CODE"):
        changes["PAIRING_CODE"] = new_pairing_code()
    if changes:
        cfg.update(changes)


def describe(cfg: Config) -> str:
    owner = cfg.owner_id()
    return (f"bot @{cfg.get('TELEGRAM_BOT_USERNAME') or '?'}, provider {cfg.provider()}, "
            + (f"paired with Telegram user {owner}" if owner else "not paired yet"))


def onboard(cfg: Config, ni: bool) -> int:
    say("\n== Archi setup ==")
    if cfg.path.exists() and cfg.get("TELEGRAM_BOT_TOKEN"):
        say(f"An existing configuration was found: {describe(cfg)}.")
        keep = (not env_flag("ARCHI_RECONFIGURE")) if ni else ask_yes("Keep it?", True)
        if keep:
            say("Keeping the existing configuration" + (" (ARCHI_RECONFIGURE=1 replaces it)." if ni else "."))
            ensure_defaults(cfg)
            return 0
    ensure_defaults(cfg)
    say("\n[1/3] Telegram bot")
    step_telegram(cfg, ni)
    say("\n[2/3] AI subscription")
    step_provider(cfg, ni)
    say("\n[3/3] Voice notes (optional)")
    step_voice(cfg, ni)
    ensure_defaults(cfg)
    say(f"\n✓ Configuration saved: {cfg.path} (mode 600)")
    return 0


# =========================================================================================== pairing

def bot_username(cfg: Config) -> str:
    name = cfg.get("TELEGRAM_BOT_USERNAME")
    if name or not cfg.get("TELEGRAM_BOT_TOKEN"):
        return name
    ok, info = check_bot_token(cfg.get("TELEGRAM_BOT_TOKEN"))
    if ok and isinstance(info, dict) and info.get("username"):
        cfg.set("TELEGRAM_BOT_USERNAME", info["username"])
        return str(info["username"])
    return ""


def pair(cfg: Config, *, reset: bool = False, wait: float | None = None, out=say) -> int:
    if reset:
        cfg.update({"OWNER_ID": "", "PAIRING_CODE": new_pairing_code()})
        out("The owner is unpaired; a new pairing code was made.")
    owner = cfg.owner_id()
    if owner is not None:
        out(f"Paired: the owner is Telegram user {owner}. To pair another account: archi pair --reset")
        return 0
    code = cfg.get("PAIRING_CODE")
    if not code:
        code = new_pairing_code()
        cfg.set("PAIRING_CODE", code)
    bot = bot_username(cfg)
    link = f"https://t.me/{bot}" if bot else "your bot in Telegram"
    out(f"\nOpen {link}, press Start and send this code: {code}")
    if bot:
        out(f"(or open https://t.me/{bot}?start={code} and press Start)")
    if not wait:
        return 0
    return wait_for_owner(cfg, wait, out=out)


def wait_for_owner(cfg: Config, timeout: float, *, out=say, poll: float = 2.0) -> int:
    t0 = time.monotonic()
    live = sys.stdout.isatty()
    last = 0.0
    while True:
        elapsed = time.monotonic() - t0
        owner = cfg.owner_id()
        if owner is not None:
            if live:
                sys.stdout.write("\r" + " " * 60 + "\r")
            out(f"✓ Paired with Telegram user {owner}. Your agent is writing its first message to you there now.")
            return 0
        if elapsed >= timeout:
            break
        if live:
            sys.stdout.write(f"\r  waiting for the code … {clock(elapsed)} (up to {clock(timeout)})")
            sys.stdout.flush()
        elif elapsed - last >= 60:
            out(f"  waiting for the code … {clock(elapsed)}")
            last = elapsed
        time.sleep(poll)
    if live:
        sys.stdout.write("\n")
    out("Not paired yet. The code stays valid: send it to the bot any time; `archi pair` shows it again.")
    return 1


# =========================================================================================== entry point

def run(args) -> int:
    cfg = Config()
    ni = bool(getattr(args, "noninteractive", False)) or env_flag("ARCHI_NONINTERACTIVE")
    what = getattr(args, "what", None) or "all"
    if not ni and not term.is_tty() and not (what == "voice" and getattr(args, "key_stdin", False)):
        print("archi setup needs a terminal (or ARCHI_NONINTERACTIVE=1 with the ARCHI_* answers)", file=sys.stderr)
        return 2
    try:
        if what == "all":
            return onboard(cfg, ni)
        ensure_defaults(cfg)
        if what == "telegram":
            step_telegram(cfg, ni)
            say("The door reads the new token on its next start: archi restart --when-idle")
        elif what == "provider":
            step_provider(cfg, ni, getattr(args, "provider", None))
        elif what == "voice":
            step_voice(cfg, ni, remove=getattr(args, "remove", False), key_from_stdin=getattr(args, "key_stdin", False))
        return 0
    except Aborted:
        say("\nSetup cancelled. Run `archi setup` again to finish it.")
        return 130
    except KeyboardInterrupt:
        say("\nSetup cancelled. Run `archi setup` again to finish it.")
        return 130
    except SetupError as e:
        say(f"\n✗ Setup failed: {e}")
        return 1
