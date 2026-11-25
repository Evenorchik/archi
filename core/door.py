"""The door-independent half of every door: from the owner's message to the delivered answer.

A door (doors/telegram, doors/matrix) receives messages from its messenger, works out which conversation they
belong to and hands them to `turn()`. Everything from there on is the same for every door and lives here:

* turns: one at a time per conversation (the registry's per-key lock), different conversations in parallel;
  the conversation's provider, model and session; the live status the door draws; the run marker that keeps
  `archi restart --when-idle` from killing a turn before its answer is delivered;
* delivery: the answer as text, `[VOICE]` blocks as voice notes, `[FILE]` markers as files;
* the chat commands /new /status /model /provider /stop /learn /jobs /help (answered at once, even while a
  turn runs);
* the contracts: core.afterturn after a delivered turn, core.jobs for finished durable jobs, and the notice
  about turns a restart interrupted;
* the voice-key flow and the turn prompts for voice notes and files.

A door subclass sets DOOR (its name for run markers, contracts and ARCHI_DOOR), PREFIX (the key prefix of its
conversations), PLACE / PLACES (what the owner calls a conversation there: "topic", "room"), COMMANDS, and
implements the messenger hooks: send_text, send_voice, send_file, make_live, system_prompt, turn_env and
job_fallback.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import afterturn, engine, jobs, running, voice
from .config import Config
from .conversations import Conversation, Registry
from .markers import split_answer
from .prompt import stamp
from .status import TurnStatus, clock

KEY_REQUEST = {
    "ru": "Дай мне ключ, я внесу его в конфиг",
    "uk": "Дай мне ключ, я внесу его в конфиг",
}
KEY_REQUEST_EN = "Send me an OpenAI API key and I will add it to the config"
JOBS_POLL_S = 5
INTERRUPTED_NOTICE = ("⚠️ I was restarted while a task was running here, so it was interrupted. Send the request "
                      "again if it is still needed.")
_COMMANDS = [
    ("new", "Start a new session in this {place}"),
    ("status", "Model, session and what is running"),
    ("model", "Show or change the model for this {place}"),
    ("provider", "Show or switch the agent: claude or codex"),
    ("stop", "Stop the task running in this {place}"),
    ("learn", "Review this conversation now and save the lessons"),
    ("jobs", "Background jobs started from this {place}"),
    ("help", "What I can do here"),
]


def command_list(place: str, extra: list[tuple[str, str]] | tuple = ()) -> list[tuple[str, str]]:
    """The commands every door has, worded for its conversations, with the door's own ones before /stop."""
    out = [(name, desc.format(place=place)) for name, desc in _COMMANDS]
    at = [name for name, _ in out].index("stop")
    return out[:at] + list(extra) + out[at:]


def parse_model_args(args: list[str], provider: str) -> tuple[str | None, str | None, str | None]:
    """`/model` arguments -> (model | None, effort | "" | None, error | None).
    model "default" = drop the override; effort None = not mentioned, "" = back to the default."""
    model = effort = None
    for raw in args:
        w = raw.strip()
        low = w.lower()
        if low in engine.EFFORTS:
            if provider != "claude":
                return None, None, "reasoning effort can only be set for claude"
            effort = low
        elif low in ("default", "reset"):
            if model is None and effort is None and len(args) == 1:
                return "default", None, None
            effort = ""
        elif model is None:
            if provider == "claude" and not low.startswith("claude-") and low not in ("opus", "sonnet", "haiku",
                                                                                         "fable"):
                low = "claude-" + low.replace(".", "-").replace("_", "-")
            model = low if provider == "claude" else w
        else:
            return None, None, f"I did not understand «{raw}»; efforts are {', '.join(engine.EFFORTS)}"
    return model, effort, None


@dataclass
class RunningTurn:
    key: str
    cancel: asyncio.Event
    status: TurnStatus
    live: object | None = None     # the door's live status display (start / poke / stop)
    started: float = field(default_factory=time.time)


class BaseDoor:
    DOOR = ""                      # "telegram" | "matrix"
    PREFIX = ""                    # conversation key prefix: "tg" | "mx"
    PLACE = "conversation"         # what a conversation is called to the owner: "topic", "room"
    PLACES = "conversations"
    COMMANDS: list[tuple[str, str]] = command_list("conversation")
    CMD_PREFIX = "/"               # how the owner types a command in this messenger
    HELP_TAIL = "Send text, photos, files or voice notes. Commands answer right away, even while a task runs."

    def __init__(self, cfg: Config | None = None, registry: Registry | None = None, *, runner=None, log=None,
                 debounce_s: float = 0.8):
        self.cfg = cfg or Config()
        self.home = self.cfg.home
        self.registry = registry or Registry(self.home / "state")
        self.runner = runner or engine.run_turn
        logger = logging.getLogger(f"archi.{self.DOOR or 'door'}")
        self.log = log or (lambda *a: logger.info(" ".join(str(x) for x in a)))
        self.debounce_s = debounce_s
        self.running: dict[str, RunningTurn] = {}
        self.tasks: set[asyncio.Task] = set()
        self.started = time.time()
        self._buffers: dict[str, dict] = {}
        self._owner_lang = ""
        self.stopping = False          # set when the process shuts down: running turns are not reported

    # ================================================================== messenger hooks (per door)
    async def send_text(self, key: str, md: str) -> bool:
        """Markdown -> messages in the conversation. False when the conversation is gone (stop sending)."""
        raise NotImplementedError

    async def send_voice(self, key: str, ogg: Path) -> bool:
        """Send a voice note. False when the conversation is gone."""
        raise NotImplementedError

    async def send_file(self, key: str, path: str) -> bool:
        """Send a `[FILE]` the agent asked for, or tell the owner why it could not be sent. False when the
        conversation is gone."""
        raise NotImplementedError

    def make_live(self, conv: Conversation, st: TurnStatus, src=None):
        """The live status display of one turn: an object with `start()`, `poke()` and `stop(ok)`."""
        return _NoLive()

    def system_prompt(self, conv: Conversation, provider: str | None = None) -> str:
        raise NotImplementedError

    def turn_env(self, conv: Conversation) -> dict:
        raise NotImplementedError

    def job_fallback(self, conv: Conversation) -> str | None:
        """Where a finished job reports when the conversation that launched it is gone (None: nowhere)."""
        return None

    # ================================================================== plumbing
    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def _lang(self) -> str:
        return (getattr(self, "_owner_lang", "") or "").split("-")[0].lower()

    def _provider(self, conv: Conversation) -> str:
        return conv.provider if conv.provider in engine.PROVIDERS else self.cfg.provider()

    def _model(self, conv: Conversation, provider: str) -> tuple[str, str]:
        """(model, effort) for a turn: the conversation's override, else the config default of its provider.
        The override always belongs to the conversation's current provider (switching providers clears it)."""
        if provider == "codex":
            return conv.model or self.cfg.get("CODEX_MODEL"), ""
        effort = conv.effort or self.cfg.get("CLAUDE_EFFORT")
        return conv.model or self.cfg.get("CLAUDE_MODEL"), (effort if effort in engine.EFFORTS else "")

    # ================================================================== commands
    def command_handlers(self) -> dict:
        return {"new": self._cmd_new, "status": self._cmd_status, "model": self._cmd_model,
                "provider": self._cmd_provider, "stop": self._cmd_stop, "learn": self._cmd_learn,
                "jobs": self._cmd_jobs, "help": self._cmd_help}

    def run_command(self, conv: Conversation, name: str, args: str) -> bool:
        """Start the handler of /name, if there is one. Commands never wait for the running turn."""
        handler = self.command_handlers().get(name.lower())
        if handler is None:
            return False   # unknown slash text is a normal message (the engine neutralises the slash)
        self.spawn(handler(conv, args.strip()))
        return True

    async def _cmd_new(self, conv: Conversation, args: str) -> None:
        had = self.registry.clear_session(conv.key)
        busy = conv.key in self.running
        text = (f"🧹 New session for this {self.PLACE}: I start from a clean slate here. Other {self.PLACES} are "
                "not affected.")
        if not had:
            text = (f"🧹 There was no session in this {self.PLACE} yet, so nothing was lost. The next message "
                    "starts one.")
        if busy:
            text += "\nA task is still running here; its result will not continue in the new session."
        await self.send_text(conv.key, text)

    async def _cmd_status(self, conv: Conversation, args: str) -> None:
        conv = self.registry.get(conv.key) or conv
        provider = self._provider(conv)
        model, effort = self._model(conv, provider)
        sid = conv.sessions.get(provider)
        run = self.running.get(conv.key)
        open_convs = [c for c in self.registry.all(door=self.PREFIX) if c.state != "deleted"]
        up = int(time.time() - self.started)
        lines = [
            f"**{conv.display_title()}** · {conv.state}",
            f"· agent: `{provider}` · model: `{model or 'CLI default'}`"
            + (f" · effort: {effort or 'default'}" if provider == "claude" else ""),
            f"· session: `{sid[:8]}…`" if sid else "· session: none yet (the next message starts one)",
        ]
        if run:
            lines.append(f"· running: {clock(run.status.elapsed)}, {len(run.status.steps)} steps"
                         + (f", now: {run.status.current}" if run.status.current else ""))
        else:
            lines.append("· running: nothing")
        lines += [f"· conversations: {len(open_convs)}, running now: {len(self.running)}",
                  f"· voice notes: {'on' if voice.available(self.cfg) else 'off (no OpenAI key)'}",
                  f"· up for {up // 3600} h {up % 3600 // 60} min"]
        await self.send_text(conv.key, "\n".join(lines))

    async def _cmd_model(self, conv: Conversation, args: str) -> None:
        conv = self.registry.get(conv.key) or conv
        provider = self._provider(conv)
        model, effort = self._model(conv, provider)
        place, c = self.PLACE, self.CMD_PREFIX
        if not args:
            await self.send_text(conv.key, "\n".join([
                f"Model in this {place}: `{model or 'CLI default'}`"
                + (f" · effort: {effort or 'default'}" if provider == "claude" else "") + f" ({provider})",
                "",
                f"· `{c}model sonnet` or `{c}model claude-sonnet-4-5`: another model for this {place}",
                f"· `{c}model opus high`: model and reasoning effort; `{c}model high`: effort only",
                f"· `{c}model default`: back to the default from the config",
                f"· efforts: {', '.join(engine.EFFORTS)}",
            ]))
            return
        new_model, new_effort, err = parse_model_args(args.split(), provider)
        if err:
            await self.send_text(conv.key, f"⚠️ {err}")
            return
        if new_model == "default":
            self.registry.upsert(conv.key, model="", effort="")
            conv = self.registry.get(conv.key)
            m, e = self._model(conv, provider)
            await self.send_text(conv.key, f"✅ Back to the default: `{m or 'CLI default'}`")
            return
        cand_model = new_model or model
        cand_effort = new_effort if new_effort is not None else ("" if new_model else effort)
        ok, why = await engine.probe_model(provider, cand_model, cand_effort, cwd=self.home)
        if not ok:
            await self.send_text(conv.key, f"⚠️ Not switched: `{cand_model}` did not answer a test request ({why}).")
            return
        self.registry.upsert(conv.key, model=new_model or conv.model, effort=cand_effort or "")
        await self.send_text(conv.key, f"✅ This {place} now uses `{cand_model or 'CLI default'}`"
                                       + (f" with effort {cand_effort}" if cand_effort else "")
                                       + ". Checked with a test request.")

    async def _cmd_provider(self, conv: Conversation, args: str) -> None:
        conv = self.registry.get(conv.key) or conv
        current = self._provider(conv)
        want = args.lower().strip()
        c = self.CMD_PREFIX
        if not want:
            await self.send_text(conv.key, f"Agent in this {self.PLACE}: `{current}`. Switch: `{c}provider claude` "
                                           f"or `{c}provider codex`.")
            return
        if want not in engine.PROVIDERS:
            await self.send_text(conv.key, f"Usage: `{c}provider claude` or `{c}provider codex`")
            return
        binary = engine.resolve_binary(want)
        if not (os.path.isabs(binary) and os.path.exists(binary)) and not shutil.which(binary):
            await self.send_text(conv.key, f"⚠️ The `{want}` CLI is not installed on this server.")
            return
        changes = {"provider": want}
        if want != current:
            changes.update(model="", effort="")   # a model override never carries over to the other agent
        self.registry.upsert(conv.key, **changes)
        await self.send_text(conv.key, f"✅ This {self.PLACE} now talks to `{want}`. Sessions are kept per agent, "
                                       f"so an earlier {want} session here continues.")

    async def _cmd_stop(self, conv: Conversation, args: str) -> None:
        run = self.running.get(conv.key)
        if not run:
            await self.send_text(conv.key, f"Nothing is running in this {self.PLACE}.")
            return
        run.cancel.set()

    async def _cmd_learn(self, conv: Conversation, args: str) -> None:
        conv = self.registry.get(conv.key) or conv
        provider = self._provider(conv)
        try:
            text = await asyncio.to_thread(afterturn.review_now, self.DOOR, conv.key, provider=provider,
                                           session_id=conv.sessions.get(provider), home=self.home)
        except Exception as e:  # noqa: BLE001
            text = f"⚠️ Could not start the review: {e!r}"
        await self.send_text(conv.key, text)

    async def _cmd_jobs(self, conv: Conversation, args: str) -> None:
        try:
            lines = await asyncio.to_thread(jobs.status_lines, self.home, conversation=conv.key)
        except Exception as e:  # noqa: BLE001
            await self.send_text(conv.key, f"⚠️ Could not read the jobs: {e!r}")
            return
        if not lines:
            await self.send_text(conv.key, f"No jobs: nothing was started in the background from this {self.PLACE}.")
            return
        await self.send_text(conv.key, "\n".join([f"**Background jobs of this {self.PLACE}**",
                                                  *[f"· {line}" for line in lines]]))

    async def _cmd_help(self, conv: Conversation, args: str) -> None:
        name = self.cfg.agent_name()
        await self.send_text(conv.key, "\n".join([
            f"**{name}**: every {self.PLACE} is a separate conversation, {self.PLACES} run in parallel.",
            "",
            *[f"· {self.CMD_PREFIX}{c} — {d}" for c, d in self.COMMANDS],
            "",
            self.HELP_TAIL,
        ]))

    # ================================================================== text buffering
    def _buffer_text(self, key: str, text: str, src=None) -> None:
        """Several messages a moment apart (Telegram splits a long paste) become one turn."""
        buf = self._buffers.get(key)
        if buf and buf.get("timer"):
            buf["timer"].cancel()
        else:
            buf = self._buffers[key] = {"parts": [], "src": src}
        buf["parts"].append(text)
        buf["timer"] = self.spawn(self._flush_text(key))

    async def _flush_text(self, key: str) -> None:
        await asyncio.sleep(self.debounce_s)
        buf = self._buffers.pop(key, None)
        if buf:
            self.spawn(self.turn(key, "\n".join(buf["parts"]), src=buf.get("src")))

    # ================================================================== voice and keys
    async def ask_for_voice_key(self, key: str) -> None:
        await self.send_text(key, KEY_REQUEST.get(self._lang(), KEY_REQUEST_EN))

    async def voice_turn(self, key: str, path: Path | str, src=None) -> None:
        """Transcribe a downloaded voice note (the file is removed afterwards) and run it as a turn."""
        try:
            text = await voice.transcribe(str(path), self.cfg)
        except Exception as e:  # noqa: BLE001
            await self.send_text(key, f"⚠️ Transcription failed: {e}")
            return
        finally:
            Path(path).unlink(missing_ok=True)
        if not text:
            await self.send_text(key, "⚠️ I could not make out any speech in that voice note.")
            return
        await self.turn(key, f"[Voice message, transcribed automatically]\n{text}", src=src)

    async def store_openai_key(self, key: str, secret: str, deleted: bool) -> None:
        """The owner pasted an OpenAI key: check it, store it, confirm. `deleted`: the door managed to remove
        the owner's message with the key."""
        verdict = await voice.check_key(secret)
        if verdict is False:
            await self.send_text(key, "⚠️ OpenAI rejected this key, so I did not save it.")
            return
        self.cfg.set("OPENAI_API_KEY", secret)
        self.log("OPENAI_API_KEY stored", "verified" if verdict else "not verified")
        if self._lang() in ("ru", "uk"):
            text = "✅ Ключ сохранён в конфиг. Пришли голосовое ещё раз."
            if not verdict:
                text += " (Проверить ключ сейчас не получилось, он будет опробован на следующем голосовом.)"
            if not deleted:
                text += " Удали своё сообщение с ключом: у меня не вышло."
        else:
            text = "✅ Key saved to the config. Please send the voice note again."
            if not verdict:
                text += " (I could not verify it right now; it will be tried on the next voice note.)"
            if not deleted:
                text += " Please delete your message with the key, I could not do it."
        await self.send_text(key, text)

    # ================================================================== turns
    async def turn(self, key: str, prompt: str, images: list[str] | None = None, *, src=None) -> None:
        """Run one turn of conversation `key` (queued behind a running one) and deliver the answer. `src` is the
        door's reference to the message that started it (the live status may mark it)."""
        async with self.registry.lock(key):
            conv = self.registry.get(key)
            if conv is None or conv.state == "deleted":
                return
            provider = self._provider(conv)
            model, effort = self._model(conv, provider)
            gen = self.registry.generation(key)
            st = TurnStatus()
            live = self.make_live(conv, st, src)
            run = RunningTurn(key, asyncio.Event(), st, live)
            self.running[key] = run
            # The marker covers the whole turn INCLUDING delivery: `archi restart --when-idle` must not see
            # "idle" between the CLI's exit and the answer reaching the messenger.
            try:
                marker = running.start(self.DOOR, key, self.home, hard_s=engine.Limits().hard_s)
            except OSError as e:
                marker = None
                self.log("run marker not written", key, repr(e))
            keep_marker = False

            def on_event(ev: engine.Event) -> None:
                st.feed(ev)
                live.poke()

            try:
                await live.start()
                res = None
                try:
                    res = await self.runner(provider, f"{stamp(self.cfg)}\n{prompt}", conv.sessions.get(provider),
                                            cwd=self.home, images=images, on_event=on_event,
                                            env=self.turn_env(conv),
                                            system_prompt=self.system_prompt(conv, provider=provider),
                                            model=model, effort=effort, cancel=run.cancel)
                except Exception as e:  # noqa: BLE001
                    res = engine.TurnResult("", None, False, f"internal error: {e!r}")
                finally:
                    self.running.pop(key, None)
                    st.finish(res or engine.TurnResult("", None, False, "interrupted"))
                    await live.stop(bool(res and res.ok))
                try:
                    if res.session_id:
                        self.registry.set_session(key, provider, res.session_id, gen=gen)
                    self.registry.upsert(key, last_turn_at=time.time())
                except Exception as e:  # noqa: BLE001 - bookkeeping must never eat the answer
                    self.log("session save failed", key, repr(e))
                if self.stopping:
                    # Killed by the shutdown, not by the owner: the next door process reports it as
                    # interrupted (the marker stays), nothing is sent now.
                    keep_marker = True
                    return
                await self.deliver(key, res, st)
                if res.ok and res.session_id:
                    await self._after_turn(key, provider, res.session_id, prompt, res.answer, st.steps)
            except asyncio.CancelledError:
                keep_marker = True
                raise
            finally:
                if not keep_marker:
                    running.finish(marker)

    async def _after_turn(self, key: str, provider: str, session_id: str, prompt: str, answer: str,
                          steps: list[str]) -> None:
        try:
            kind = await asyncio.to_thread(afterturn.turn_finished, self.DOOR, key, provider=provider,
                                           session_id=session_id, tool_calls=len(steps), home=self.home,
                                           prompt=prompt, answer=answer, steps=list(steps))
            if kind:
                self.log("learning review started", key, kind)
        except Exception as e:  # noqa: BLE001 - the learning loop must never break the door
            self.log("learning hook failed", key, repr(e))

    async def deliver(self, key: str, res: engine.TurnResult, st: TurnStatus) -> None:
        if res.cancelled:
            await self.send_text(key, f"⏹ Stopped after {clock(st.elapsed)}.")
            return
        text = res.answer or ""
        if not res.ok:
            text = (text + "\n\n" if text else "") + f"⚠️ {res.error or 'the agent did not answer'}"
        clean, files, voices = split_answer(text)
        if clean and not await self.send_text(key, clean):
            return
        for _lang, speech, speed in voices:
            ogg = await voice.synthesize(speech, self.cfg, speed=speed) if voice.available(self.cfg) else None
            if ogg is None:
                await self.send_text(key, speech)
                continue
            try:
                if not await self.send_voice(key, ogg):
                    return
            finally:
                ogg.unlink(missing_ok=True)
        for path in files:
            if not await self.send_file(key, path):
                return

    # ================================================================== restarts and durable jobs
    async def _report_interrupted(self) -> None:
        """Turns that were running when the previous door process ended were killed with it (their run
        markers are still there): say so in the conversations where they ran."""
        for m in running.leftovers(self.DOOR, self.home):
            key = str(m.get("key") or "")
            if not key.startswith(f"{self.PREFIX}:"):
                continue
            conv = self.registry.get(key)
            if conv is not None and conv.state == "deleted":
                continue
            try:
                await self.send_text(key, INTERRUPTED_NOTICE)
            except Exception as e:  # noqa: BLE001
                self.log("restart notice failed", key, repr(e))

    async def report_jobs(self) -> int:
        """Start a report turn for every finished job of this door. Returns how many were started."""
        started = 0
        for job in await asyncio.to_thread(jobs.finished_unreported, self.DOOR, self.home):
            key = str(job.get("conversation") or "")
            conv = self.registry.get(key) if key else None
            if conv is not None and conv.state == "deleted":
                key = self.job_fallback(conv) or ""
                conv = self.registry.get(key) if key else None
            if conv is None:
                self.log("job without a conversation to report in", job.get("id"), job.get("conversation"))
                await asyncio.to_thread(jobs.mark_reported, str(job.get("id")), self.home)
                continue
            self.spawn(self.turn(key, jobs.report_prompt(job)))
            await asyncio.to_thread(jobs.mark_reported, str(job.get("id")), self.home)
            started += 1
        return started

    async def jobs_loop(self, interval: float = JOBS_POLL_S) -> None:
        while True:
            try:
                await self.report_jobs()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log("jobs loop error", repr(e))
            await asyncio.sleep(interval)


class _NoLive:
    async def start(self) -> None:
        pass

    def poke(self) -> None:
        pass

    async def stop(self, ok: bool) -> None:
        pass
