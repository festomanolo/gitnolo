"""
PTY supervisor with safe prompt auto-approval.

Runs an agent CLI inside a pseudo-terminal, mirrors it to your terminal, and
answers confirmation prompts:
  * [y/N] style prompts      -> "y" + Enter
  * menu prompts (Claude Code "Do you want to proceed?  > 1. Yes") -> Enter
Destructive commands visible on screen (rm -rf /, force pushes, DROP TABLE,
disk formatting, ...) are never auto-approved; you are alerted instead.

Rapid response: questions that are not approvals (Claude Code's
AskUserQuestion menu and similar pickers) are answered with the first option,
which agents list as their recommendation, but only once you have been away
from the keyboard for `away_seconds` and the question has waited
`choice_delay` seconds. Any keystroke counts as being present and resets both.
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "fcntl", "pty", "re", "select", "signal", "struct", "termios", "tty",
]

import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import tty
from typing import Callable, List, Optional, Tuple

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

YES_NO = [
    re.compile(r"\[[yY]/[nN]\]\s*:?\s*$"),
    re.compile(r"\([yY]/[nN]\)\s*:?\s*$"),
    re.compile(r"\[[nN]/[yY]\]\s*:?\s*$"),
    re.compile(r"\(yes/no\)\s*:?\s*$", re.I),
]
MENU = [
    re.compile(r"do you want to (?:proceed|make this edit|create|run|allow)[^\n]*\?\s*\n.*?(?:❯|>)\s*1\.\s*yes", re.I | re.S),
    re.compile(r"(?:❯|>)\s*1\.\s*(?:yes|allow|accept)", re.I),
    re.compile(r"press enter to (?:continue|confirm)", re.I),
]
# A selection menu (not a yes/no approval): a highlighted first option plus picker hints.
CHOICE = re.compile(r"(?:❯|›)\s*1\.\s*\S")
CHOICE_HINT = re.compile(r"enter to (?:select|confirm|submit)|esc to (?:cancel|go back)|↑/↓|to navigate|type something", re.I)
DANGER = re.compile(
    r"rm\s+-[a-z]*r[a-z]*f?\s+(?:/|~|\$HOME|\*)(?:\s|$)|rm\s+-rf\s+\.\s*$|git\s+push\s+.*(?:--force|-f\b)|"
    r"git\s+reset\s+--hard\s+origin|git\s+clean\s+-[a-z]*f[a-z]*d|drop\s+(?:table|database)|truncate\s+table|"
    r"mkfs|dd\s+if=.*of=/dev/|chmod\s+-R\s+777\s+/|:\(\)\s*\{|shutdown|reboot|diskutil\s+erase|"
    r"curl[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z)?sh|sudo\s+rm",
    re.I,
)


def _winsize(fd: int) -> bytes:
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except OSError:
        return struct.pack("HHHH", 40, 120, 0, 0)


def supervise_command(
    cmd_args: List[str],
    auto_accept: bool = True,
    notify: Optional[Callable[[str, str], None]] = None,
    rapid: bool = True,
    away_seconds: float = 45.0,
    choice_delay: float = 15.0,
) -> int:
    if not cmd_args:
        return 1
    stdin_fd = sys.stdin.fileno()
    is_tty = os.isatty(stdin_fd)
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvp(cmd_args[0], cmd_args)
        except Exception as e:
            sys.stderr.write(f"gitnolo: cannot run {cmd_args[0]}: {e}\n")
            os._exit(127)

    def sync_size(*_a) -> None:
        if is_tty:
            try:
                fcntl.ioctl(master, termios.TIOCSWINSZ, _winsize(stdin_fd))
            except OSError:
                pass

    sync_size()
    old_handler = signal.signal(signal.SIGWINCH, sync_size)
    old_attr = None
    if is_tty:
        old_attr = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)

    screen = ""
    last_answer = 0.0
    approvals = 0
    answered_questions = 0
    last_input = time.time()
    choice_since: Optional[float] = None
    out_fd = sys.stdout.fileno()

    shown = [""]

    def title(text: str) -> None:
        if text != shown[0]:  # the loop ticks 10x a second; only write changes
            shown[0] = text
            os.write(out_fd, f"\033]0;{text}\007".encode())
    try:
        while True:
            fds = [master] + ([stdin_fd] if is_tty else [])
            try:
                r, _, _ = select.select(fds, [], [], 0.1)
            except InterruptedError:
                continue
            if master in r:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
                if auto_accept:
                    screen = (screen + ANSI_RE.sub("", data.decode("utf-8", "ignore")))[-4000:]
            if is_tty and stdin_fd in r:
                try:
                    user = os.read(stdin_fd, 1024)
                except OSError:
                    break
                if not user:
                    break
                os.write(master, user)
                screen = ""  # the human answered; start fresh
                last_input = time.time()
                if choice_since is not None:
                    choice_since = None
                    title(cmd_args[0])
            if auto_accept and screen and time.time() - last_answer > 1.5:
                kind, answer = classify(screen)
                now = time.time()
                if kind == "choice":
                    if not rapid:
                        continue
                    if choice_since is None:
                        choice_since = now
                        if notify:
                            notify("gitnolo", f"{cmd_args[0]} is asking a question; option 1 in {int(choice_delay)}s if you are away")
                    away = not is_tty or now - last_input >= away_seconds
                    wait = max(choice_delay - (now - choice_since), (away_seconds - (now - last_input)) if is_tty else 0)
                    if not away or now - choice_since < choice_delay:
                        title(f"gitnolo: answering option 1 in {int(wait) + 1}s (press any key to answer yourself)")
                        continue
                    os.write(master, answer or b"\r")
                    answered_questions += 1
                    last_answer = now
                    choice_since = None
                    screen = ""
                    title(cmd_args[0])
                    if notify:
                        notify("gitnolo", f"answered {cmd_args[0]}'s question with option 1 (you were away)")
                    continue
                choice_since = None
                if kind == "danger":
                    msg = "Destructive command detected: approval left to you"
                    os.write(sys.stdout.fileno(), f"\r\n\033[38;2;224;179;84m[gitnolo] {msg}\033[0m\r\n".encode())
                    if notify:
                        notify("gitnolo", msg)
                    screen = ""
                    last_answer = time.time()
                elif answer is not None:
                    time.sleep(0.15)  # let the prompt finish rendering
                    os.write(master, answer)
                    approvals += 1
                    last_answer = time.time()
                    screen = ""
    finally:
        signal.signal(signal.SIGWINCH, old_handler)
        if old_attr is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attr)
        try:
            os.close(master)
        except OSError:
            pass
    _, status = os.waitpid(pid, 0)
    if approvals or answered_questions:
        sys.stdout.write(f"\n[gitnolo] auto-approved {approvals} prompt(s)"
                         + (f", answered {answered_questions} question(s) while you were away" if answered_questions else "") + "\n")
    return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)


def classify(screen: str) -> Tuple[Optional[str], Optional[bytes]]:
    """(kind, bytes to send) for the prompt on screen. kind: approve | choice | danger | None."""
    tail = screen[-1500:]
    yes_no = any(p.search(tail.rstrip()) for p in YES_NO)
    approval = yes_no or any(p.search(tail) for p in MENU)
    choice = not approval and bool(CHOICE.search(tail) and CHOICE_HINT.search(tail))
    if not (approval or choice):
        return None, None
    if DANGER.search(tail):
        return "danger", None
    if yes_no:
        return "approve", b"y\r"
    return ("approve" if approval else "choice"), b"\r"


def _decide(screen: str, rapid: bool = False) -> Optional[bytes]:
    """Bytes to send for a visible prompt, "danger", or None. Choices are answered only when `rapid`."""
    kind, answer = classify(screen)
    if kind == "danger":
        return "danger"  # type: ignore[return-value]
    if kind == "approve" or (kind == "choice" and rapid):
        return answer
    return None
