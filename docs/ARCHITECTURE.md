# Archi architecture (v0.1 target)

Archi is a self-hosted personal agent: a long-running service on a Linux server that lets you talk to a coding
agent CLI (Claude Code or Codex) from Telegram or Matrix/Element, keeps one conversation per topic/room, runs
the agent with full access to its own server, and maintains itself (restarts, updates, memory, skills).

## Layout of an installed agent (`/opt/archi` = the agent's home and working directory)
```
/opt/archi
├── archi                    CLI: status | restart [--when-idle] | update | logs | doctor | setup <telegram|matrix|voice>
├── install.sh               one-command installer (also used by `archi setup`)
├── CLAUDE.md                the agent's rulebook; AGENTS.md -> CLAUDE.md for Codex
├── core/                    shared engine, no door-specific code
│   ├── engine.py            run_turn(provider, prompt, session, on_event) for Claude Code and Codex
│   ├── conversations.py     conversation registry: key -> session id, title, state, lock
│   ├── config.py            config/archi.env + config/identity.json
│   ├── voice.py             speech-to-text through the OpenAI API (optional key)
│   └── status.py            live task status model (steps, elapsed, final state)
├── doors/telegram/          bot in private chat and in groups, forum topics = conversations
├── doors/matrix/            Element/Matrix rooms = conversations (optional)
├── ops/                     validate.sh, restart guard, systemd unit templates, self-modification guard
├── tools/                   new_skill.py, memory_search.py, durable jobs, self-learning loop
├── skills/                  skills shipped with the base; .claude/skills -> ../skills
├── config/  state/  logs/   runtime data (git-ignored)
└── memory/  knowledge/  sessions/   the agent's own memory (git-ignored)
```

## Engine
One function for both providers. Claude Code runs as `claude -p --output-format stream-json` with
`--resume <session>`; Codex runs as `codex exec --json` / `codex exec resume`. Both streams are parsed into the
same events: `tool` (a step the user can see), `text` (final answer parts), `done` (answer, new session id, ok).
Deadlines: silence limit and hard limit; the whole process group is killed on timeout. One lock per
conversation; different conversations run in parallel.

## Conversations
Key = door + chat + thread: `tg:<chat_id>:<thread_id|general>`, `mx:<room_id>`. The registry stores the
provider session id, title, state (`open`, `closed`, `deleted`), timestamps. Every key has an optional persona
file (`state/personas/<key>.md`) the agent can write when the user gives a topic its own rules.

## Telegram door (Bot API 10.5)
- Private chat with topics: the installer enables topic mode, user-created topics and the topic sidebar through
  `setMySettings` (`has_topics_enabled`, `allows_users_to_create_topics`, `has_topic_sidebar`). The agent can
  create topics itself (`createForumTopic` in the private chat).
- Groups with topics: the bot is added as an administrator with `can_manage_topics`; privacy mode is turned off
  through `setMySettings(can_read_all_group_messages=True)`. Only the owner's messages start turns.
- Topic lifecycle: `forum_topic_created` registers a topic immediately, `forum_topic_edited` renames it,
  `forum_topic_closed` / `forum_topic_reopened` change its state. Telegram sends no event for a deleted topic
  and has no method to list topics, so deletions are detected from send errors and by a periodic existence
  probe; topics created before the bot joined are learned from their first message
  (`reply_to_message.forum_topic_created`).
- Live status: in private chats the answer is streamed with `sendMessageDraft` (with a stop button,
  `stopped_message_generation` cancels the turn); in groups an edited status message shows the steps.
- Owner pairing: the installer prints a one-time code; the first account that sends it to the bot becomes the
  owner. Everyone else is ignored.
- Voice notes: transcribed when an OpenAI key is configured; otherwise the agent asks for the key and writes it
  into the config itself.

## Life support
systemd units with `Restart=always` for each door; `archi restart --when-idle` waits until no turn is running
(a restart kills running turns); self-modification is validated (`ops/validate.sh`) before any restart; a
restart requested from inside a turn is delayed so the reply is delivered first. `archi update` pulls the
repository and keeps local changes (stash + reapply, conflicts reported to the agent).
