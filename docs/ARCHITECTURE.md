# Archi architecture (v0.1 target)

Archi is a self-hosted personal agent: a long-running service on a Linux server that lets you talk to a coding
agent CLI (Claude Code or Codex) from Telegram or Matrix/Element, keeps one conversation per topic/room, runs
the agent with full access to its own server, and maintains itself (restarts, updates, memory, skills).

## Layout of an installed agent (`/opt/archi` = the agent's home and working directory)
```
/opt/archi
├── archi                    CLI: status | restart | logs | validate | update | identity | setup | pair |
│                            service | version | tg … | mx … | config …  (stdlib only, /usr/local/bin/archi)
├── install.sh               one-command installer: packages, code, venv, then `archi setup` + service
├── CLAUDE.md                the agent's rulebook; AGENTS.md -> CLAUDE.md for Codex
├── core/                    shared engine, no messenger-specific code
│   ├── engine.py            run_turn(provider, prompt, session, on_event) for Claude Code and Codex
│   ├── door.py              BaseDoor: turn -> deliver, the shared chat commands, contracts, run markers
│   ├── conversations.py     conversation registry: key -> session id, title, state, lock
│   ├── config.py            config/archi.env + config/identity.json
│   ├── running.py           run markers: state/running/<door>--<key>.json while a turn runs
│   ├── markdown.py          the door-independent half of rendering Markdown (inline HTML, splitting)
│   ├── inbox.py             received files: .inbox/<timestamp>_<name>, the file-turn prompt
│   ├── voice.py             speech-to-text through the OpenAI API (optional key)
│   ├── status.py            live task status model (steps, elapsed, final state)
│   ├── afterturn.py         door hook of the learning loop: turn log, thresholds, /learn
│   ├── learnlog.py          state of the learning loop (state/learn/: config, turn logs, counters, runs)
│   ├── jobs.py              durable jobs: launch, runner, reaper, cancel, the door's report contract
│   ├── detach.py            start work outside the turn and the door's cgroup (systemd-run or setsid)
│   └── notify.py            post a notice into a conversation from outside its door (tg:, mx:)
├── doors/telegram/          bot in private chat and in groups, forum topics = conversations
├── doors/matrix/            Element/Matrix rooms = conversations (optional second door, no e2e encryption)
├── ops/                     the ops half of `archi`: setup.py (onboarding, CLI install and sign-in),
│                            service.py (systemd unit, restarts, logs), validate.py, update.py,
│                            identity.py, term.py (prompts, pty capture)
├── tools/                   new_skill.py (skill scaffold/validate/snapshot), memory_search.py (FTS5 search),
│   │                        workflow_job.py (a Claude workflow as a durable job)
│   └── learn/               self-learning loop: review.py, curator.py, digest.py, prompts.py, stores.py,
│                            reviewer.py
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

## Doors share one core
A door only speaks its messenger: it receives messages, maps them to a conversation and hands them to
`BaseDoor.turn()` (core/door.py). The rest is the same for every door: one turn at a time per conversation,
the conversation's provider, model and session, the run marker that covers the turn including delivery, the
answer with its `[VOICE]` and `[FILE]` markers, the chat commands (/new /status /model /provider /stop /learn
/jobs /help, answered at once even while a turn runs), the afterturn and jobs contracts, the notice about turns
a restart interrupted, the voice-key flow. A door implements the hooks: send_text / send_voice / send_file,
is_transient, make_live (its live status display), system_prompt (its block after the shared base prompt),
turn_env, can_post, job_fallback (where a finished job reports when its conversation is gone) and
delivered_late.

An answer never vanishes. A send that fails for a passing reason (no network, a server error, a rate limit) is
retried with a bounded backoff (1, 2, 4, 8, 15 s; Matrix retries one message under one transaction id, so the
server stores it once). What still could not be sent is kept in `state/outbox/` (0600) and delivered, marked as
late, when the messenger answers again (a check every 30 s; Matrix also right after its sync recovers) or at
the next start; a door stopped in the middle of a delivery keeps the rest there too. The live status shows the
outcome only after the delivery: ✅ (Matrix) means the owner has the answer, a kept answer shows ⚠️ and a
footer saying it follows, and its late delivery turns the ⚠️ into ✅. The afterturn hook runs only for an
answer that reached the owner (for a kept one, when it is delivered). A finished job counts as reported only
after its report was delivered or kept: while its report turn waits or runs it is not offered twice, and a
stop before that offers it again after the restart (at least once).

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

## Matrix door (optional, Element or any Matrix client)
- matrix-nio without its e2e extra (no libolm, no vodozemac): the door cannot read encrypted rooms. The bot
  needs its own account on any homeserver; `archi setup matrix` resolves the homeserver through
  `/.well-known/matrix/client`, signs the bot in with its password once (only the access token is kept, as
  `MATRIX_ACCESS_TOKEN`, a secret), checks it with whoami, stores `MATRIX_OWNER_ID`, installs
  `archi-matrix.service`. Telegram stays the primary door; the installer does not ask about Matrix.
- Every room the bot is in is a conversation `mx:<room_id>` (room-version-12 ids without a server part
  included) with its own session; rooms run in parallel. The room name is the title (`m.room.name` updates it).
  The bot being kicked or leaving, or the owner leaving (the bot then leaves too), archives the conversation
  like a deleted Telegram topic.
- Owner only: only `MATRIX_OWNER_ID`'s messages start turns and only the owner's invites are accepted; other
  invites are rejected, everybody else is ignored, silently. The door keeps every room's members (from the
  sync; one `/members` call for a room it has not seen whole) and never posts into a room the owner is not in
  (`archi mx send` and `notify.post` refuse too); the bot leaves such a room. A finished job whose room is gone
  reports only in a private room of the owner and the bot (the owner joined), else in a room the door made for
  that, else in a new unencrypted room with the owner invited.
- Encrypted rooms: invited into one (its invite state carries `m.room.encryption`), or when encryption is
  switched on in a room the bot is in, the bot posts one plain-text notice that it cannot read encrypted rooms
  and how to create one without encryption, then leaves; the room is recorded for `archi status`. Staying would
  pretend to work: the owner's messages there are unreadable.
- Sync: the door long-polls `/sync` itself and handles the event JSON as the spec documents it (only
  `doors/matrix/api.py` touches nio). The token is kept in `state/matrix.json`: a restart replays nothing and
  misses nothing; the first start ever skips the rooms' history; owner messages older than 24 h (the door was
  down) are not answered. Joins and leaves send a JSON body (strict servers refuse nio's empty one), and the
  access token always travels in the `Authorization` header, never in a URL. Every transport failure comes out
  of the client as an `MxError` with status 0, so it is retried like a server error. `archi setup matrix`
  signs the stored device in again instead of adding one and logs the replaced token out; a running door picks
  the new token up from the config when the old one is refused.
- Live status: ⏳ on the owner's message, an `m.notice` after 4 s edited in place (`m.replace`, at most every
  3 s, a clock tick every 30 s), typing kept up, ✅ or ⚠️ at the end; quick answers get no notice. Notices do
  not notify under Matrix's default push rules; the answer is an `m.text` (Markdown in `body`,
  `org.matrix.custom.html` in `formatted_body`, split by UTF-8 size under the 64 KiB event limit).
- Commands start with `!` (Element keeps `/` for its own commands and asks before sending unknown ones); `/`
  works too.
- Media: images (several in a row are one turn) as image input, files to `.inbox/`, voice messages (MSC3245)
  transcribed or the key asked for, a pasted OpenAI key stored and its message redacted when the bot may,
  attachments forwarded out of encrypted rooms decrypted with the key they carry. `[FILE]` goes out as
  `m.image` (shown inline, with its size) or `m.file` within the server's upload limit, `[VOICE]` as an MSC3245
  voice message.
- `archi mx room create|rename`, `archi mx rooms`, `archi mx send` (stdlib) are the agent's tools; the room of
  the turn is `ARCHI_ROOM_ID`. A room the agent creates is unencrypted and the owner is invited with the
  creator's power level (`trusted_private_chat`). `doors/matrix/notify.post(cfg, key, text, silent=True)` posts
  a notice from background work (the learning loop).

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
- `archi-telegram.service` (and `archi-matrix.service` once the Matrix door is set up, same template):
  `Restart=always`, `WorkingDirectory` and `ARCHI_HOME` = the install directory,
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
- `archi validate`: syntax of every module, imports in the venv, a dry start of each configured door
  (`python -m doors.telegram --check`, `python -m doors.matrix --check`: config, token format, construction, no
  network), config sanity. Run before any restart after a code change.
- `ARCHI_UNIT_DIR` points the unit files elsewhere (tests, staging copies); systemctl is then never called,
  because it would act on the host's units of the same name.
- `archi update`: fetch the branch, stash uncommitted changes (tracked and untracked), fast-forward or rebase
  local commits, reapply the stash, reinstall requirements, validate with the new code, restart when idle.
  A conflict or a failed validation rolls the checkout back exactly (old commit, local changes in place)
  and lists the conflicting files; nothing is forced.
- `archi status` never prints secrets; `archi logs [--door telegram|matrix]` is journalctl over `archi-*`
  units, never following.

## Durable jobs
Work that must outlive a turn: `archi job run -- <command>`, `archi job agent "<task>"` (a nested
`engine.run_turn` in a fresh session), `tools/workflow_job.py launch --script x.js` (a nested `claude -p` that
only runs that workflow). State in `state/jobs/<id>/` (spec, status, output, result). The runner
(`archi job _run <id>`) is a transient systemd unit `archi-job-<id>` outside the door's cgroup (setsid double
fork without systemd), at most `JOBS_MAX_ACTIVE` (3) at once. A reaper marks jobs whose runner died (pid +
start time + boot id) as failed; cancel kills the whole tree. Every job belongs to a conversation: the door
polls `jobs.finished_unreported()` every 5 s and starts a report turn there with `jobs.report_prompt()`.

## Self-learning loop
`afterturn.turn_finished()` appends every delivered turn to `state/learn/turns/<conversation>.jsonl` (prompt,
answer, step labels: provider-independent) and counts it; every 10 owner turns (memory) or 60 tool calls
(skills) it starts `tools/learn/review.py` detached (transient unit, nice 10, one at a time). The review digests
the turns since the conversation's watermark, backs up and stages the memory store and `skills/`, runs the
reviewer CLI on the copies (claude: acceptEdits, only file tools, writes confined to the staged dirs, no
settings file of the host loaded; codex: `-s workspace-write` in the stage, no extra writable roots, no
network; the digest is framed as untrusted data), validates (memory frontmatter, index, secrets; each skill against its own
problems before the run, rolled back otherwise; only Markdown skill changes are applied, other files wait in
`state/learn/proposals/`), applies three-way (live changes win; a memory file is removed only through the
archive flow), advances the watermark and posts a silent, redacted notice through `core/notify.py`. The weekly curator (`archi-learn-curator.timer`, installed
by `archi learn enable`) consolidates memory the same way; its first run is a dry run. It reports only counts
and file names, and only into a conversation nobody but the owner reads (a Telegram private chat, a Matrix
room of two).
