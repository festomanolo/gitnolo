"""
PTY supervisor with safe prompt auto-approval.

Runs an agent CLI inside a pseudo-terminal, mirrors it to your terminal, and
answers confirmation prompts:
  * [y/N] style prompts      -> "y" + Enter
  * menu prompts (Claude Code "Do you want to proceed?  > 1. Yes") -> Enter
Destructive commands visible on screen (rm -rf /, force pushes, DROP TABLE,
disk formatting, ...) are never auto-approved; you are alerted instead.
"""

from __future__ import annotations

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
from typing import List, Optional

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


def supervise_command(cmd_args: List[str], auto_accept: bool = True, notify=None) -> int:
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
            if auto_accept and screen and time.time() - last_answer > 1.5:
                answer = _decide(screen)
                if answer == "danger":
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
    if approvals:
        sys.stdout.write(f"\n[gitnolo] auto-approved {approvals} prompt(s)\n")
    return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)


def _decide(screen: str) -> Optional[bytes]:
    """Returns the bytes to send for a visible prompt, b'danger', or None."""
    tail = screen[-1500:]
    prompt_visible = any(p.search(tail) for p in MENU) or any(p.search(tail.rstrip()) for p in YES_NO)
    if not prompt_visible:
        return None
    if DANGER.search(tail):
        return "danger"  # type: ignore[return-value]
    if any(p.search(tail.rstrip()) for p in YES_NO):
        return b"y\r"
    return b"\r"
