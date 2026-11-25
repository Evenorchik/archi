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
The installer asks for a Telegram bot token (create one with @BotFather), lets you pick Claude Code or Codex
and walks you through the login, optionally takes an OpenAI key for voice messages, and gives you a one-time
code to send to your bot. From then on you talk to your agent in Telegram: give it a name and tell it how to
work with you.

## Security
Archi runs as root by default and executes whatever its agent decides to run. Use a dedicated server, keep the
bot private (only the paired owner is answered), and keep backups.

## License
MIT, see [LICENSE](LICENSE).
