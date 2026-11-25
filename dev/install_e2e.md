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

- `archi update` three times against new commits of the source: fast-forward with a local untracked file
  kept, requirements, validation, restart when idle; then "Already up to date".
- The interactive Claude sign-in, driven on a pty (`archi setup provider claude`) without completing OAuth:
  the "sign in again?" question, `claude setup-token` drawn through the capture pty (screen text rendered
  with its spaces), three Ctrl+C leaving it (it ignores a single Ctrl+C, Ctrl+D and Esc), the paste-the-token
  fallback, then `claude auth login` showing its link and paste prompt. The credentials stayed untouched.
- The Codex browser fallback in a throwaway container: the sign-in link captured from `codex login`, the
  pasted callback delivered to its server on 127.0.0.1:1455 (a made-up code, so it did not finish).

## Not covered here (by hand with the owner)
Pairing from a real Telegram account and the first-run message in Telegram, completing a real
`claude setup-token` (the token capture itself) and a Codex device or browser sign-in, the voice key
prompt, a real conversation.

## Fresh installs on three systems (before publishing, 2026-11-25)
Each in a new systemd container with only curl and ca-certificates, the installer fed through a pipe
(`cat install.sh | bash`, the `curl | bash` case), `ARCHI_SOURCE` pointing at the checkout, a private test bot.
- **Ubuntu 24.04 (Python 3.12), interactive.** A pty driver answered every question the way a person types
  (token hidden, provider 1, keep the existing Claude sign-in, no voice key): packages, code, venv, the official
  Claude Code installer, config 0600 in a 0700 directory, `archi-telegram` enabled and polling,
  `archi-learn-curator.timer` enabled, the pairing code and link, a 20 s pairing wait, the summary; exit 0.
  `archi status` and `archi validate` clean; the checkout clean (`git status` empty).
  **Re-run** on the same machine: packages and code kept, "Keep it?" for the existing config, service and
  timer reinstalled, the same pairing code; exit 0.
- **Ubuntu 22.04 (Python 3.10), non-interactive.** The first run failed at the Python requirements:
  aiogram 3.32+ needs Python 3.11. requirements.txt now lets pip pick aiogram 3.31 there; the door does not
  need newer (an `Update` with `stopped_message_generation` and topic fields survives aiogram 3.31's parsing).
  Then exit 0, the door polling, 228 tests passing on Python 3.10.
- **Debian 12 (Python 3.11.2), non-interactive.** The door did not start: aiogram 3.33+ cannot be imported on
  Python 3.11.2 (`TypeError: unhashable type: 'FailFast'`, a typing bug with pydantic's FailFast in a Union);
  3.32 imports. requirements.txt keeps Python 3.11 and older below aiogram 3.33. Then exit 0, the door
  polling, 228 tests passing on Python 3.11.2.
The Claude sign-in was the CLI's own login copied in (the "already signed in" path); `claude setup-token`
itself is covered above, up to the OAuth page.

The same images: `dev/test/Dockerfile` (Ubuntu 24.04), `dev/test/Dockerfile.ubuntu2204`, `dev/test/Dockerfile.debian12`.
After publishing, the README command itself (`curl … | bash` from GitHub) was run on a fresh Ubuntu 22.04 with
`ARCHI_NONINTERACTIVE=true`: cloned from GitHub, exit 0, the door polling, `archi update` "Already up to date".
