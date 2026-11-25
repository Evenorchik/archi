"""The operations part of the `archi` command: status, restart, logs, validate, update, identity, setup,
pair, service, version. Stdlib only; secrets are never printed."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from core import __version__, engine, running
from core.config import CODE_DIR, Config

from . import service


def git_info(code_dir=CODE_DIR) -> str:
    try:
        res = subprocess.run(["git", "-C", str(code_dir), "log", "-1", "--format=%h %cs"], capture_output=True,
                             text=True, timeout=10)
        if res.returncode != 0 or not res.stdout.strip():
            return "not a git checkout"
        dirty = subprocess.run(["git", "-C", str(code_dir), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return res.stdout.strip() + (", local changes" if dirty else "")
    except (OSError, subprocess.TimeoutExpired):
        return "git unavailable"


def cmd_version(args) -> int:
    print(f"archi {__version__} ({git_info()})")
    return 0


def _ago(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else f"{s // 60} min {s % 60} s"


def cmd_status(args) -> int:
    from .setup import cli_path, cli_version
    cfg = Config()
    print(f"Archi {__version__} ({git_info()}) · home {cfg.home}")
    if service.have_systemd():
        for door in service.DOORS:
            st = service.unit_state(door)
            name = service.unit_name(door)
            if not st or st.get("LoadState") == "not-found":
                print(f"  {name}: not installed (archi service install)")
                continue
            line = f"  {name}: {st.get('ActiveState')} ({st.get('SubState')})"
            if st.get("ActiveState") == "active":
                line += f" since {st.get('ActiveEnterTimestamp') or '?'}, pid {st.get('MainPID')}"
            if st.get("NRestarts", "0") not in ("0", ""):
                line += f", restarted {st.get('NRestarts')}x by systemd"
            print(line + ("" if st.get("UnitFileState") == "enabled" else " · not enabled at boot"))
    else:
        print("  services: systemd is not running here")
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    bot = cfg.get("TELEGRAM_BOT_USERNAME")
    print(f"Telegram: {'@' + bot if bot else ('bot token set' if token else 'no bot token (archi setup telegram)')}")
    owner = cfg.owner_id()
    if owner is not None:
        print(f"Owner:    paired (Telegram user {owner})")
    elif cfg.get("PAIRING_CODE"):
        print("Owner:    not paired yet: `archi pair` shows the code to send to the bot")
    else:
        print("Owner:    not paired and no pairing code: run `archi pair`")
    print(f"Name:     {cfg.agent_name() if cfg.has_name() else 'not named yet (the owner names it on the first run)'}")
    provider = cfg.provider()
    if provider == "claude":
        model = cfg.get("CLAUDE_MODEL") or "CLI default"
        effort = cfg.get("CLAUDE_EFFORT") or "default"
        auth = "token from claude setup-token" if cfg.get("CLAUDE_CODE_OAUTH_TOKEN") else "the CLI's own login"
        print(f"Provider: claude · model {model} · effort {effort} · sign-in: {auth}")
    else:
        print(f"Provider: codex · model {cfg.get('CODEX_MODEL') or 'CLI default'}")
    path = cli_path(provider)
    print(f"CLI:      {cli_version(provider) + ' · ' + path if path else f'{provider} is NOT installed'}")
    print(f"Voice:    {'on (OpenAI key set)' if cfg.get('OPENAI_API_KEY') else 'off (archi setup voice)'}")
    busy = running.active(cfg.home)
    if busy:
        print(f"Turns:    {len(busy)} running: " + ", ".join(f"{m['key']} ({_ago(m['age'])})" for m in busy[:5]))
    else:
        print("Turns:    none running")
    return 0


def cmd_restart(args) -> int:
    return service.restart(when_idle=args.when_idle, delay=args.delay, max_wait=args.max_wait)


def cmd_logs(args) -> int:
    return service.logs(args.lines, args.door)


def cmd_validate(args) -> int:
    from . import validate
    return validate.run()


def cmd_update(args) -> int:
    from . import update
    return update.update(restart=not args.no_restart)


def cmd_identity(args) -> int:
    from . import identity
    if args.identity_cmd == "set-name":
        return identity.set_name(args.name)
    cfg = Config()
    print(cfg.agent_name() if cfg.has_name() else "not named yet")
    return 0


def cmd_setup(args) -> int:
    from . import setup
    return setup.run(args)


def cmd_pair(args) -> int:
    from . import setup
    cfg = Config()
    rc = setup.pair(cfg, reset=args.reset, wait=args.wait)
    if cfg.owner_id() is None and service.have_systemd() and service.installed_doors():
        if service.unit_state("telegram").get("ActiveState") != "active":
            print("Note: archi-telegram is not running, so the code cannot be received yet: archi service install")
    return rc


def cmd_service(args) -> int:
    return service.install_unit("telegram", start=not args.no_start)


def add_parsers(sub) -> None:
    p = sub.add_parser("status", help="services, provider, model, pairing, running turns")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("restart", help="restart the door service(s); safe from inside a turn")
    p.add_argument("--when-idle", action="store_true", help="wait until no turn is running")
    p.add_argument("--delay", type=float, default=0, metavar="N", help="wait N seconds first")
    p.add_argument("--max-wait", type=float, default=None, metavar="S", help="with --when-idle: give up after S s")
    p.set_defaults(func=cmd_restart)

    p = sub.add_parser("logs", help="recent log lines of the services (journalctl, never follows)")
    p.add_argument("-n", "--lines", type=int, default=100)
    p.add_argument("--door", choices=sorted(service.DOORS))
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("validate", help="syntax, imports, dry start, config sanity; non-zero on failure")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("update", help="pull upstream, keep local changes, validate, restart when idle")
    p.add_argument("--no-restart", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("identity", help="the agent's name")
    isub = p.add_subparsers(dest="identity_cmd")
    s = isub.add_parser("set-name", help="config, CLAUDE.md and the Telegram bot name")
    s.add_argument("name")
    p.set_defaults(func=cmd_identity)

    p = sub.add_parser("setup", help="onboarding: Telegram, provider login, voice key")
    p.add_argument("--noninteractive", action="store_true", help="take the answers from ARCHI_* variables")
    ssub = p.add_subparsers(dest="what")
    ssub.add_parser("telegram", help="replace the bot token")
    s = ssub.add_parser("provider", help="install and sign in claude or codex, make it the default")
    s.add_argument("provider", nargs="?", choices=list(engine.PROVIDERS))
    s = ssub.add_parser("voice", help="add, replace or remove the OpenAI key for voice notes")
    s.add_argument("--remove", action="store_true")
    s.add_argument("--key-stdin", action="store_true", help="read the key from stdin (scripts)")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("pair", help="show the pairing code; --reset to pair another account")
    p.add_argument("--reset", action="store_true", help="unpair the owner and make a new code")
    p.add_argument("--wait", type=float, default=None, metavar="S", help="wait up to S seconds for pairing")
    p.set_defaults(func=cmd_pair)

    p = sub.add_parser("service", help="systemd units")
    vsub = p.add_subparsers(dest="service_cmd", required=True)
    s = vsub.add_parser("install", help="write and enable archi-telegram.service, then (re)start it")
    s.add_argument("--no-start", action="store_true")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("version", help="Archi version and git commit")
    p.set_defaults(func=cmd_version)


def run(args) -> int:
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except subprocess.TimeoutExpired as e:
        print(f"timed out: {e}", file=sys.stderr)
        return 1


def main(argv=None) -> int:   # for tests
    ap = argparse.ArgumentParser(prog="archi")
    add_parsers(ap.add_subparsers(dest="cmd", required=True))
    args = ap.parse_args(argv)
    os.environ.setdefault("ARCHI_HOME", str(Config().home))
    return run(args)
