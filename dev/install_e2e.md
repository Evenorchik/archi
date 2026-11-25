# End-to-end test of the installer in a clean container (stage B)

Run on 2026-11-25 in a systemd-enabled Ubuntu 24.04 container (`dev/test/Dockerfile`), against a private test
bot. The token was passed with `docker exec -e` and never written to the repository.

## Repeat it
```bash
docker build -t archi-test dev/test
docker run -d --name archi-e2e --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v "$PWD":/src:ro archi-test
docker exec -e ARCHI_NONINTERACTIVE=1 -e ARCHI_SOURCE=/src -e ARCHI_BOT_TOKEN=<test bot token> \
  -e ARCHI_PROVIDER=claude -e ARCHI_SKIP_LOGIN=1 -e ARCHI_SKIP_PAIRING_WAIT=1 archi-e2e bash /src/install.sh
```
Sign Claude Code in inside the container (`docker exec -it archi-e2e archi setup provider claude` runs the real
`claude setup-token` flow), then pair by sending the printed code to the bot.

## What was checked
- The installer from a bare image: packages, copy of the checkout with its `.git`, venv, the official Claude
  Code installer (2.1.332 into `~/.local/bin`), config written 0600 with a 6-digit pairing code, the
  `archi-telegram` service enabled, started and stable; exit code 0.
- The door: connected, Bot API settings applied, polling, no tracebacks in `archi logs`.
- `archi version`, `archi status` (no secrets), `archi validate` (syntax, 26 imports, dry start, config),
  `archi logs`.
- A real first-run turn through the engine and the Claude CLI in `/opt/archi`: hostname, public IP,
  provider and model, the two questions.
- `archi restart --when-idle` while idle: restarted at once, new door process.
- The same from inside a turn (`ARCHI_CONVERSATION` set, a run marker held by a live process): returned at
  once, a transient `archi-restart-*` unit waited, the door kept running while the turn did and was
  restarted about a second after the turn ended.
- `archi identity set-name`: config, CLAUDE.md and the bot's Telegram name changed (and were restored).
- `archi update` with the container's `origin` pointed at the source checkout: fast-forward to a new commit,
  requirements, validation with the new code, restart when idle.

## Not covered here (by hand with the owner)
Pairing from a real Telegram account and the first-run message in Telegram, the interactive prompts
(`claude setup-token` capture, Codex device and browser sign-in, the voice key prompt), a real conversation.
