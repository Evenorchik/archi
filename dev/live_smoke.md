# Live smoke test of the Telegram door (stage A)

Run on 2026-11-25 against Bot API 10.5 with a private test bot, its owner's private chat and a test
supergroup with topics where the bot is an administrator with `can_manage_topics`. Token and ids were
kept outside the repository; the test used a temporary `ARCHI_HOME` (deleted afterwards), so nothing of
it lands in the working tree.

## Setup
1. Temporary home with `config/archi.env` (mode 0600): `TELEGRAM_BOT_TOKEN`, `OWNER_ID`,
   `PROVIDER=claude`, `CLAUDE_MODEL=claude-haiku-4-5` (the cheapest model the CLI accepted),
   `TG_PROBE_INTERVAL=60`.
2. `getWebhookInfo`: no webhook and no other poller. The 7 pending updates were stale updates from manual
   API exploration and were dropped (`deleteWebhook(drop_pending_updates=true)`), so no old owner message was
   answered and no topic was renamed by accident.
3. `core.engine.probe_model("claude", "claude-haiku-4-5")`: the CLI answered.
4. Door started with `ARCHI_HOME=<tmp> timeout 900 .venv/bin/python -m doors.telegram`.

## What was checked (21/21 passed)
- Startup: `setMySettings` applied (`getMe` reports `has_topics_enabled`, `allows_users_to_create_topics`,
  `can_read_all_group_messages`); `getMyCommands` returns new, status, model, provider, topics, stop, help.
- `archi tg topic create "[Archi test] dm"` in the private chat: topic created and registered by the CLI;
  the polling door then received the `forum_topic_created` service message for the bot's own topic and
  confirmed it. (In the group the bot does not get a service message for a topic it created itself, which
  is why the CLI registers topics directly.)
- `archi tg send` with Markdown into the topic, `archi tg topics` lists it.
- A real turn through `Door.turn()` (real Bot API, real `claude -p` with haiku) in the private-chat topic:
  three `sendMessageDraft` calls (placeholder, progress, answer preview), the final answer as
  `sendMessage` in the topic, the session id stored in `state/conversations.json`.
- The same in a group topic: no drafts; typing action, a status message after 3 s, deleted when the
  turn succeeded, then the answer.
- Probe of the live group topic: `editForumTopic(name=<stored name>)` answered 400 `TOPIC_NOT_MODIFIED`
  and was classified as alive.
- Both topics deleted with `deleteForumTopic`. Observed: `sendMessage` to the deleted thread answers
  `Bad Request: message thread not found`, the probe edit answers `Bad Request: TOPIC_ID_INVALID`.
  The polling door's periodic probe detected both deletions within about a minute, marked them
  `deleted`, appended them to `state/archive.jsonl` and posted the one-line notice to `main` of each chat.
- No traceback in the door log; SIGTERM stopped polling cleanly.

## Not covered live (unit tests only)
Incoming owner messages (a bot cannot send messages as its owner): pairing, commands, voice notes and the
key flow, the stop button (`stopped_message_generation`), photos and documents, the restart notice. These
are covered by `tests/test_telegram_door.py` with update shapes taken from the live API, and should be
tried by hand once with the owner.

## Repeat it
Use any test bot you own: put its token and your id into a temporary `ARCHI_HOME/config/archi.env`, start
the door with that `ARCHI_HOME`, then drive it with `ARCHI_HOME=<tmp> ARCHI_CHAT_ID=<your id> ./archi tg …`
and talk to the bot. Delete the temporary home afterwards: it holds the token.
