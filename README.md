# Archi

**A self-hosted personal agent you talk to from Telegram.** Archi runs on your own Linux server, drives a
coding-agent CLI (Claude Code or OpenAI Codex) with your subscription, and answers you in Telegram: in a private
chat with your bot or in a group, with one independent conversation per topic. It has full access to its server,
so it can install things, run services, write and fix code, and maintain itself.

> Status: v0.1 in development. This README describes the target of the first release.

## What you get
- **Telegram as the main door.** Private chat with topics, or a group with topics: every topic is its own
  conversation with its own memory, and topics run in parallel. Archi notices new, renamed, closed and deleted
  topics and can create topics for you. Element/Matrix is available as a second door.
- **Your subscription, no API bills.** Claude Code (Claude subscription) or Codex (ChatGPT subscription); switch
  per chat with `/provider`.
- **Live progress.** While a task runs you see its steps; long tasks keep going after the reply.
- **Life support.** systemd services, safe restarts that wait for running tasks, validation before restarting
  its own code, `archi update`.
- **Memory and self-improvement.** Notes, session logs and searchable memory; a self-review loop that writes
  lessons and improves skills; tools to create new skills.
- **Skills included:** brainstorming, adversarial review of its own diffs, repository indexing, answers as HTML
  pages, web pages to Markdown, a writing-quality gate (`clean-copy`), and an optional design suite.

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
  `archi setup telegram` (new bot token)
- `archi pair --reset`: pair a different Telegram account
- `archi identity set-name "Name"`: rename the agent (it does this itself when you name it)

### Unattended install
Every question has an environment variable, see the header of `install.sh`. For example:
```bash
curl -fsSL https://raw.githubusercontent.com/Evenorchik/archi/main/install.sh | \
  ARCHI_NONINTERACTIVE=1 ARCHI_BOT_TOKEN=<bot token> ARCHI_PROVIDER=claude \
  ARCHI_CLAUDE_OAUTH_TOKEN=<token from claude setup-token> bash
```

## Security
Archi runs as root by default and executes whatever its agent decides to run. Use a dedicated server, keep the
bot private (only the paired owner is answered), and keep backups.

## License
MIT, see [LICENSE](LICENSE).
