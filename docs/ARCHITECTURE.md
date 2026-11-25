# Archi architecture (v0.1 target)

Archi is a self-hosted personal agent: a long-running service on a Linux server that lets you talk to a coding
agent CLI (Claude Code or Codex) from Telegram or Matrix/Element, keeps one conversation per topic/room, runs
the agent with full access to its own server, and maintains itself (restarts, updates, memory, skills).

## Layout of an installed agent (`/opt/archi` = the agent's home and working directory)
```
/opt/archi
├── archi                    CLI: status | restart | logs | validate | update | identity | setup | pair |
│                            service | version | tg … | config …  (stdlib only, /usr/local/bin/archi)
├── install.sh               one-command installer: packages, code, venv, then `archi setup` + service
├── CLAUDE.md                the agent's rulebook; AGENTS.md -> CLAUDE.md for Codex
├── core/                    shared engine, no door-specific code
│   ├── engine.py            run_turn(provider, prompt, session, on_event) for Claude Code and Codex
│   ├── conversations.py     conversation registry: key -> session id, title, state, lock
│   ├── config.py            config/archi.env + config/identity.json
│   ├── running.py           run markers: state/running/<door>--<key>.json while a turn runs
│   ├── voice.py             speech-to-text through the OpenAI API (optional key)
│   └── status.py            live task status model (steps, elapsed, final state)
├── doors/telegram/          bot in private chat and in groups, forum topics = conversations
├── doors/matrix/            Element/Matrix rooms = conversations (optional, not shipped yet)
├── ops/                     the ops half of `archi`: setup.py (onboarding, CLI install and sign-in),
│                            service.py (systemd unit, restarts, logs), validate.py, update.py,
│                            identity.py, term.py (prompts, pty capture)
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
Key = door + chat + thread: `tg:<chat_id>:<thread_id|main>` ("main" = outside any topic, General in groups), `mx:<room_id>`. The registry stores the
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
- Owner pairing: the installer prints a one-time 6-digit code (also as a `t.me/<bot>?start=<code>` link); the
  first account that sends it to the bot becomes the owner. Everyone else is ignored, silently; 20 wrong
  codes revoke the code (`archi pair` makes a new one). Right after pairing the door starts a turn of its own
  (no owner message): the agent introduces itself per "First run" in CLAUDE.md and asks for its name.
- Voice notes: transcribed when an OpenAI key is configured; otherwise the agent asks for the key and writes it
  into the config itself.

## Install and onboarding
`install.sh` (bash, `curl … | bash`, as root) checks root / apt / systemd (exit codes 2-4), installs the
packages, clones the repository into `/opt/archi` (or copies `ARCHI_SOURCE` with its `.git` for tests),
builds `.venv`, links `/usr/local/bin/archi`, then runs `archi setup </dev/tty` (stdin is the script itself
under `curl | bash`), `archi service install` and `archi pair --wait 900`. A re-run keeps the code and asks
before replacing the config; `ARCHI_NONINTERACTIVE=1` takes every answer from `ARCHI_*` variables.

`archi setup` (ops/setup.py) writes `config/archi.env` (0600):
- the bot token, checked with `getMe` (the username is kept as `TELEGRAM_BOT_USERNAME`);
- `PROVIDER` and the provider CLI, installed with the vendor's official standalone installer
  (`https://claude.ai/install.sh`, `https://chatgpt.com/codex/install.sh`, both into `~/.local/bin`) and
  checked with `--version`;
- the sign-in, headless: Claude runs `claude setup-token` on a pseudo-terminal the user drives while its output
  is captured; the one-year token it prints is stored as `CLAUDE_CODE_OAUTH_TOKEN` after a real `claude -p`
  answered with it (fallback: `claude auth login`, then the CLI's own credentials are used and the token is
  left empty). Codex runs `codex login --device-auth`; without it, `codex login`, whose browser redirect to
  `127.0.0.1:1455` is pasted back and delivered to the waiting login server with curl. A real `codex exec`
  confirms it;
- the optional `OPENAI_API_KEY` (checked against `/v1/models`), a random `PAIRING_CODE`, empty `OWNER_ID`
  and `AGENT_NAME`.

The engine reads `CLAUDE_CODE_OAUTH_TOKEN` from the config for every Claude turn: secrets never live in the
service environment, and a new token works on the next turn.

## Life support
- `archi-telegram.service`: `Restart=always`, `WorkingDirectory` and `ARCHI_HOME` = the install directory,
  `KillSignal=SIGTERM` to the door only (`KillMode=mixed`): on SIGTERM the door stops its turns, which kills
  their CLI process trees, and exits; leftovers get SIGKILL. The door applies its Bot API settings itself at
  every start.
- Run markers (`core/running.py`): one file per running turn in `state/running/`, written when the turn starts
  and removed only after its answer was delivered. They are kept when the door is stopping, so the next
  start finds them (their pid is dead) and tells those conversations that the task was interrupted. A
  marker of a dead process or past its deadline (start + hard turn limit + 10 min) never counts as running.
- `archi restart --when-idle` waits until no marker counts as running, then restarts and checks that the
  unit stays up. A restart requested from inside a turn (`ARCHI_CONVERSATION` is set) is never done in place:
  it is handed to a transient `systemd-run` unit outside the door's cgroup (a detached process without
  systemd-run) that waits until the turn has delivered its answer and the door is idle.
- `archi validate`: syntax of every module, imports in the venv, a dry start of each door
  (`python -m doors.telegram --check`: config, token format, construction, no network), config sanity.
  Run before any restart after a code change.
- `archi update`: fetch the branch, stash uncommitted changes (tracked and untracked), fast-forward or rebase
  local commits, reapply the stash, reinstall requirements, validate with the new code, restart when idle.
  A conflict or a failed validation rolls the checkout back exactly (old commit, local changes in place)
  and lists the conflicting files; nothing is forced.
- `archi status` never prints secrets; `archi logs` is journalctl over `archi-*` units, never following.
