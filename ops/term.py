"""Terminal helpers for the interactive setup: prompts, hidden input, and running a command on the terminal
while keeping a copy of what it printed.

The installer runs `archi setup </dev/tty` (under `curl … | bash` the script itself is stdin), so fd 0 is
the terminal here. Lines are read with os.read: in canonical mode the kernel returns one line per read, so
nothing is left in a Python buffer and select() sees exactly what is still pending (a token pasted as two
lines can be joined).
"""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import sys
import termios
import time
import tty

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()*+][0-9A-Za-z]"
                  r"|\x1b[0-9=>@-Z\\-_]")
CURSOR_FORWARD = re.compile(r"\x1b\[(\d*)C")      # terminal UIs draw spaces as "move right N"


class Aborted(Exception):
    """The user gave up (Ctrl+C, Ctrl+D) or there is no terminal to ask on."""


def is_tty() -> bool:
    return os.isatty(0)


def say(text: str = "") -> None:
    print(text, flush=True)


def _readline(fd: int = 0) -> str:
    chunks = []
    while True:
        data = os.read(fd, 4096)
        if not data:
            if not chunks:
                raise Aborted("end of input")
            break
        chunks.append(data)
        if data.endswith(b"\n"):
            break
    return b"".join(chunks).decode("utf-8", "replace")


def ask(prompt: str, default: str = "") -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        line = _readline().strip()
    except KeyboardInterrupt:
        raise Aborted("interrupted") from None
    return line or default


def ask_secret(prompt: str, *, join_paste: bool = False) -> str:
    """Hidden input. With join_paste, lines that arrive right after the first one (a paste that contained a
    line break, as happens with long tokens copied from a wrapped terminal) are joined to it."""
    fd = 0
    if not os.isatty(fd):
        return _readline(fd).strip()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~termios.ECHO
    sys.stdout.write(prompt)
    sys.stdout.flush()
    parts: list[str] = []
    try:
        termios.tcsetattr(fd, termios.TCSAFLUSH, new)
        parts.append(_readline(fd))
        while join_paste and select.select([fd], [], [], 0.4)[0]:
            parts.append(_readline(fd))
    except KeyboardInterrupt:
        raise Aborted("interrupted") from None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        sys.stdout.flush()
    if join_paste:
        return "".join("".join(p.split()) for p in parts)
    return parts[0].strip()


def ask_yes(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        ans = ask(f"{prompt} {hint} ").lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def ask_choice(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    """options: [(value, label)]; the user answers with the number or the value."""
    for i, (value, label) in enumerate(options, 1):
        say(f"  {i}) {label}")
    values = [v for v, _ in options]
    while True:
        ans = ask(f"{prompt} [{values.index(default) + 1}] ").lower()
        if not ans:
            return default
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return values[int(ans) - 1]
        if ans in values:
            return ans


def strip_ansi(text: str) -> str:
    text = CURSOR_FORWARD.sub(lambda m: " " * int(m.group(1) or 1), text)
    return ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


_CSI = re.compile(r"\x1b\[([0-?]*)[ -/]*([@-~])")


def render(raw: str) -> str:
    """The text a terminal would show after `raw`: a small screen emulator for what full-screen CLI
    renderers send (cursor up/down/left/right, column jumps, line and screen erases). Escape stripping
    is not enough for them: they place words with "move to column N" instead of spaces and redraw in
    place, so stripped output glues words together and repeats every redraw."""
    lines: list[list[str]] = [[]]
    row = col = 0
    i, n = 0, len(raw)

    def line() -> list[str]:
        while len(lines) <= row:
            lines.append([])
        return lines[row]

    while i < n:
        ch = raw[i]
        if ch == "\x1b":
            m = _CSI.match(raw, i)
            if m:
                params, final = m.group(1), m.group(2)
                nums = [int(x) if x.isdigit() else 0 for x in params.lstrip("?").split(";")] if params else []
                k = (nums[0] if nums else 0) or 1
                if final == "A":
                    row = max(0, row - k)
                elif final == "B":
                    row += k
                elif final == "C":
                    col += k
                elif final == "D":
                    col = max(0, col - k)
                elif final == "G":
                    col = k - 1
                elif final in "Hf" and not params.startswith("?"):
                    row, col = (nums[0] or 1) - 1 if nums else 0, (nums[1] or 1) - 1 if len(nums) > 1 else 0
                elif final == "K":
                    cur, mode = line(), (nums[0] if nums else 0)
                    if mode == 2:
                        cur.clear()
                    elif mode == 1:
                        cur[:col] = [" "] * min(col, len(cur))
                    else:
                        del cur[col:]
                elif final == "J":
                    mode = nums[0] if nums else 0
                    if mode in (2, 3):
                        lines[:] = [[]]
                        row = col = 0
                    elif mode == 0:
                        del line()[col:]
                        del lines[row + 1:]
                i = m.end()
                continue
            o = ANSI.match(raw, i)
            i = o.end() if o else i + 1
            continue
        if ch == "\r":
            col = 0
        elif ch == "\n":
            row += 1
            line()
        elif ch == "\b":
            col = max(0, col - 1)
        elif ch >= " " or ch == "\t":
            cur = line()
            if len(cur) < col:
                cur.extend(" " * (col - len(cur)))
            if col < len(cur):
                cur[col] = ch
            else:
                cur.append(ch)
            col += 1
        i += 1
    return "\n".join("".join(x).rstrip() for x in lines)


def run_captured(argv: list[str], env: dict | None = None) -> tuple[int, str]:
    """Run `argv` on a pseudo-terminal wired to ours: the user sees it and types into it as usual (an
    interactive login needs a real terminal), and the final screen text is returned too (see render()).
    The child gets our window size, so what it draws looks right on the user's screen.

    Ctrl+C three times within two seconds stops the child: some CLI screens (claude setup-token) only
    clear their input on Ctrl+C and offer no other way out."""
    stdin_fd, stdout_fd = 0, 1
    pid, master = pty.fork()
    if pid == 0:   # child: the pty slave is its controlling terminal and stdio
        try:
            os.execvpe(argv[0], argv, env if env is not None else os.environ)
        except OSError as e:
            os.write(2, f"cannot run {argv[0]}: {e}\n".encode())
        os._exit(127)
    try:
        size = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master, termios.TIOCSWINSZ, size)
    except OSError:
        pass
    saved = None
    if os.isatty(stdin_fd):
        saved = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
    out = bytearray()
    presses: list[float] = []
    kill_at = None
    try:
        watch = [master, stdin_fd]
        while True:
            if kill_at is not None and time.monotonic() >= kill_at:
                _signal_group(pid, signal.SIGKILL)
                kill_at = None
            try:
                ready, _, _ = select.select(watch, [], [], 0.5)
            except InterruptedError:
                continue
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:      # EIO: the child closed the terminal
                    data = b""
                if not data:
                    break
                os.write(stdout_fd, data)
                out += data
            if stdin_fd in ready:
                data = os.read(stdin_fd, 4096)
                if not data:
                    watch.remove(stdin_fd)
                    continue
                if b"\x03" in data:
                    now = time.monotonic()
                    presses = [t for t in presses if now - t < 2.0] + [now] * data.count(b"\x03")
                    if len(presses) >= 3:
                        _signal_group(pid, signal.SIGTERM)
                        kill_at = time.monotonic() + 2.0
                        presses.clear()
                os.write(master, data)
    finally:
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, saved)
        os.close(master)
        _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), render(out.decode("utf-8", "replace"))


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)      # the child leads its own session (pty.fork), helpers included
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass
