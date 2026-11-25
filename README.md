# Archi

**English** · [Русский](README.ru.md)

**A self-hosted personal agent you talk to from Telegram.** Archi runs on your own Linux server, drives a
coding-agent CLI (Claude Code or OpenAI Codex) with your subscription, and answers you in Telegram: in a private
chat with your bot or in a group, with one independent conversation per topic. It has full access to its server,
so it can install things, run services, write and fix code, and maintain itself.

> Status: v0.1, the first public version. The installer is tested on fresh Ubuntu 22.04, Ubuntu 24.04 and
> Debian 12; Claude Code is tested end to end, Codex up to its sign-in. Issues and fixes are welcome.

## What you get
- **Telegram as the main door.** Private chat with topics, or a group with topics: every topic is its own
  conversation with its own memory, and topics run in parallel. Archi notices new, renamed, closed and deleted
  topics and can create topics for you. Element/Matrix is available as a second door.
- **Your subscription, no API bills.** Claude Code (Claude subscription) or Codex (ChatGPT subscription); switch
  per chat with `/provider`.
- **Live progress.** While a task runs you see its steps. Long work runs as a durable job that outlives
  the reply, and when it ends the agent reports back in the same conversation.
- **Life support.** systemd services, safe restarts that wait for running tasks, validation before restarting
  its own code, `archi update`.
- **Memory and self-improvement.** Notes, session logs and searchable memory. Every few turns a background
  review reads the conversation, saves facts and lessons, improves skills and tells you briefly what changed.
- **Skills included:** brainstorming, adversarial review of its own diffs, repository indexing, answers as HTML
  pages, web pages to Markdown, a writing-quality gate (`clean-copy`), a design suite, and optional skills
  that need extra tools: image sets (Gemini API key), Blender modelling (Blender MCP), video editing (ffmpeg).

## Quick start
On a fresh Ubuntu 22.04+ / Debian 12+ server, as root:
```bash
curl -fsSL https://raw.githubusercontent.com/Evenorchik/archi/main/install.sh | bash
```
The installer:
1. installs git, Python and ffmpeg and puts Archi into `/opt/archi` (a git checkout);
2. asks for a Telegram bot token (create a bot with @BotFather) and checks it;
3. asks which subscription you use, Claude (Claude Code) or ChatGPT (Codex), installs that CLI with its
   official installer and signs you in without a browser on the server: you open a link on your phone or
   laptop and paste a code back;
4. optionally takes an OpenAI API key for voice notes;
5. starts the `archi-telegram` service and shows a one-time code: open your bot, press Start, send the code.

Your agent then writes first: it introduces itself and asks for its name and how you want to work with it.
Running the installer again is safe: it keeps the installed code and your configuration unless you choose to
reconfigure.

### Everyday commands
- `archi status`: services, provider, model, pairing, running tasks (never prints secrets)
- `archi logs -n 100`: recent log lines
- `archi restart --when-idle`: restart once the running tasks are done (the agent restarts itself this way)
- `archi update`: update from GitHub and keep your local changes; a conflict is reported and rolled back,
  never forced
- `archi validate`: check code and config before a restart
- `archi setup voice` (OpenAI key), `archi setup provider codex` (install and sign in the other CLI),
  `archi setup telegram` (new bot token), `archi setup matrix` (the Element / Matrix door)
- `archi pair --reset`: pair a different Telegram account
- `archi identity set-name "Name"`: rename the agent (it does this itself when you name it)
- `archi job list`: durable jobs; `archi learn status`: how the learning loop is set up

### Unattended install
Every question has an environment variable (the list is in the header of `install.sh`). For example:
```bash
curl -fsSL https://raw.githubusercontent.com/Evenorchik/archi/main/install.sh | \
  ARCHI_NONINTERACTIVE=1 ARCHI_BOT_TOKEN=<bot token> ARCHI_PROVIDER=claude \
  ARCHI_CLAUDE_OAUTH_TOKEN=<token from claude setup-token> bash
```
`claude setup-token` prints a one-year token on any machine where you can sign in. Codex has no such token:
sign in on the server once with `codex login --device-auth` (or copy `~/.codex/auth.json` there), then install
with `ARCHI_PROVIDER=codex`. A bot that has a webhook cannot be polled: `ARCHI_DELETE_WEBHOOK=1` removes it.

## Element / Matrix door
Archi can also answer you in Element or any other Matrix client, next to Telegram. Give the bot its own Matrix
account (on your homeserver or on matrix.org), then run on the server:
```bash
archi setup matrix
```
It finds your homeserver (also behind a `.well-known` delegation), signs the bot account in with its password
(only the access token is kept), asks for your own Matrix id (the only account it answers) and starts the
`archi-matrix` service. Then, in Element, create a room **without end-to-end encryption** (New room, turn off
"Enable end-to-end encryption") and invite the bot. Every room is its own conversation, rooms run in parallel,
and the agent can create rooms for you. The bot cannot read encrypted rooms: invited into one, it says so and
leaves. Commands start with `!` in Element (`!new`, `!status`, `!help`), since Element keeps `/` for its own.
Unattended: `ARCHI_NONINTERACTIVE=1 ARCHI_MATRIX_HOMESERVER=… ARCHI_MATRIX_USER=… ARCHI_MATRIX_PASSWORD=…
ARCHI_MATRIX_OWNER=@you:example.org archi setup matrix` (or `ARCHI_MATRIX_TOKEN` instead of user and password).

## Development
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```
How the parts fit together: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Live test procedures and their results: `dev/`.

## Security
Archi runs as root by default and executes whatever its agent decides to run. Use a dedicated server, keep the
bot private (only the paired owner is answered), and keep backups.

## License
MIT, see [LICENSE](LICENSE). The design skills come from the impeccable project under Apache-2.0, see
`skills/NOTICE-impeccable.md`.
