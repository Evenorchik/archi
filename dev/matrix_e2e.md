# Live end-to-end test of the Matrix door (stage C)

Run on 2026-11-25 against a throwaway continuwuity 26.7.2 homeserver in a container (image
`forgejo.ellis.link/continuwuation/continuwuity:v26.7.2`), with the door from this checkout and the FAKE agent CLI
(`tests/fake_cli.py` behind `CLAUDE_BIN` and `CODEX_BIN`, never the real claude or codex). Result: **61/61 checks
passed** on the last two runs, each from a freshly created server. Nothing of it lands in the working tree: passwords are generated
per run and kept in memory, the bot's token lives only in the temporary `ARCHI_HOME`, which is deleted with the
container and its volume at the end.

## Repeat it
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
python3 dev/matrix_e2e.py            # --port 18008 (default), --keep to inspect afterwards
```
`dev/matrix_e2e.py` (stdlib only) does the whole run:
1. Starts the container `archi-test-hs` with volume `archi-test-hs-db`, federation off, registration only with a
   token it generates, published on `127.0.0.1` only. Continuwuity accepts the first account only with the one-time
   token it prints at its first start; the script reads it from the container log and registers the owner with it,
   then the bot and a stranger with its own token.
2. Creates two rooms as the owner before the door ever runs: A, which the bot joins by hand and where the owner
   then writes an "old" message (history the door must never answer), and B with a pending invite to the bot.
3. Runs `archi setup matrix` non-interactively (`ARCHI_MATRIX_HOMESERVER`, `ARCHI_MATRIX_USER`,
   `ARCHI_MATRIX_PASSWORD`, `ARCHI_MATRIX_OWNER`) with `ARCHI_UNIT_DIR` set to a temporary directory: systemd does
   not read it, so the unit is written there and systemctl is never called (the host's systemd stays untouched).
4. Starts `python -m doors.matrix` with `ARCHI_HOME` in the temporary directory, drives it through the client API as
   the owner and as the stranger, watching the bot's events (messages, notices, edits, reactions, redactions,
   typing, membership) with the owner's own sync, and reads `state/` and the fake CLI's log for what the agent got.
5. Stops the door with SIGTERM during a turn, starts it again, checks the restart behaviour, stops it, removes the
   container, the volume and the temporary directory.

## What was checked (61/61 passed)
- Setup: exit 0; `config/archi.env` (mode 600) holds MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN,
  MATRIX_DEVICE_ID, MATRIX_OWNER_ID and not the password; the token is never printed; the unit
  `archi-matrix.service` (`-m doors.matrix`) is written to the temporary unit directory and reported as not
  started; the rooms of the bot account are listed with the pending invite.
- First start: the pending owner invite is accepted and greeted with a notice; the history in A is not answered;
  both rooms are registered under their names.
- A turn: the answer is an `m.text` with `org.matrix.custom.html`; ⏳ on the owner's message, taken back (redacted),
  then ✅; a quick answer gets no status notice; the session id is stored; the agent ran in `ARCHI_HOME` with
  `ARCHI_DOOR=matrix`, `ARCHI_ROOM_ID` and `ARCHI_CONVERSATION=mx:<room>`.
- Live status on a slow turn (six tool steps, 1.5 s apart): one `m.notice` after about 4 s, edited in place
  (`m.replace`) with the steps, edits at least 3 s apart while running, the last edit a footer
  ("✅ Done in m:ss · N steps"); typing shown for the bot; ✅ at the end.
- Commands: `!status`, `/help` (lists `!jobs`), `!jobs` ("No jobs …"), `!stop` on a hanging turn ("⏹ Stopped after
  …", ⚠️ on the owner's message), `!model sonnet` (probed with a real request to the fake CLI, then stored for the
  room), `!provider codex` (the next turn ran the codex flavour of the fake CLI, the model override was dropped, a
  codex session was stored), `!new` (both sessions dropped).
- Lifecycle: renaming the room in the client updates the conversation title; the owner leaving a room makes the bot
  leave too and archives the conversation ("owner left"); the bot kicked archives it ("kicked").
- Media in: an `m.image` reaches the agent as image input (stream-json image block); an `m.file` is saved under
  `.inbox/<timestamp>_notes.txt` with identical bytes and the prompt names the path and says not to execute it; a
  voice message (MSC3245) without an OpenAI key gets the key request.
- Media out: `[FILE chart.png]` arrives as `m.image` with width and height, `[FILE report.txt]` as `m.file`, both
  downloadable with identical bytes; `[VOICE]` without a key falls back to its text.
- Encrypted rooms: invited into one, the bot joins, explains in plain text that it cannot read encrypted rooms,
  leaves; the room is recorded and no conversation is kept. Encryption switched on in a room the bot is in: the
  same notice, the bot leaves, the conversation is archived ("encrypted"). `archi status` counts them.
- Strangers: their invite is rejected without a word; in the owner's room their messages (a command and a
  question) are ignored.
- `archi mx room create "E2E created"`: an unencrypted room, the owner invited; after the owner joins it is a
  working conversation. `archi mx room rename` (current room from `ARCHI_ROOM_ID`) works in a room the bot created
  and refuses clearly in the owner's room, where the bot has no power to rename. `archi mx send --notice` arrives
  as a notice with HTML. `archi mx rooms` lists the conversations and the encrypted rooms that were left.
  `doors.matrix.notify.post()` delivers a notice.
- `archi status` shows the bot, homeserver, owner, rooms, the encrypted rooms ignored and the service line, without
  the token. `archi validate` passes and dry-starts the Matrix door.
- Restart: SIGTERM during a hanging turn stops the door cleanly (exit 0), the run marker stays, the agent's process
  tree (the fake CLI and its child) is killed; the next start tells that room the task was interrupted, replays
  nothing (no answer to any earlier message), answers a new message, and the sync token is persisted in
  `state/matrix.json`. No traceback in either door log.

## Found and fixed by this test
matrix-nio's `join()` (and `room_leave()`) send no request body; continuwuity answers the join with
`M_BAD_JSON`, so the first run never entered a room the owner invited it to (stranger invites were still rejected,
and leaving happened to work). Joins and leaves now go through nio's HTTP session with `{}`, like /sync.

## Not covered live (unit tests only)
- A real voice note transcribed and a `[VOICE]` answer synthesized (both need an OpenAI key and send audio to
  OpenAI), and the pasted-key flow (it checks the key against OpenAI). While exploring the API on the same server,
  redacting an owner's message was refused for a bot without moderator power in an owner-made room
  (`M_FORBIDDEN`): that is the case the door's "please delete your message" text covers.
- Element itself: the test speaks the client-server API directly, the way Element does. Element's own behaviour,
  such as asking before sending an unknown `/command` (hence `!` for commands) and encrypting new DMs by default
  (hence "New room, encryption off"), was not exercised.
- Attachments forwarded out of encrypted rooms (decryption is unit-tested with nio's own encryption routine),
  several images in a row merged into one turn, reply fallbacks, the 24 h age limit for messages from a downtime,
  the upload-size limit, a rejected access token stopping the door, a job report routed to the most recently used
  room.
- The systemd unit actually running under systemd (the unit text is the Telegram door's template; this run used a
  unit directory systemd does not read, on purpose).
