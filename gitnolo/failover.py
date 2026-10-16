"""
Account failover for kiro-cli / agy: when an agent hits its usage limit, switch to
the next Chrome profile (each profile is signed in to a different account), then
continue the work with a handoff prompt built from where the previous run stopped.

  config.agent_profiles = {"kiro": ["Default", "Profile 1"], "agy": ["Profile 2", "Profile 3"]}

Switching = log out, log in with BROWSER pointing at a wrapper that opens the login
URL in the next Chrome profile. State (current profile, cooldowns) lives in
~/.gitnolo/failover.json.
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = ["json", "re", "shlex", "stat", "subprocess"]

import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

from .config import CONFIG_DIR

STATE_FILE = os.path.join(CONFIG_DIR, "failover.json")
BROWSER_DIR = os.path.join(CONFIG_DIR, "browser")
HANDOFF_DIR = os.path.join(CONFIG_DIR, "handoff")
COOLDOWN = 5 * 3600.0  # a limited profile is skipped for this long unless the agent says when it resets

# Stricter than agents.LIMIT_RE: this runs on raw terminal text, where "rate limit" appears in code and docs.
SCREEN_LIMIT = re.compile(
    r"(?:reached|hit|exceeded)[^\n]{0,40}(?:usage|monthly|daily|session|rate|request)[^\n]{0,15}limit|"
    r"(?:usage|monthly|session) limit (?:reached|exceeded)|out of (?:credits|requests|usage)|"
    r"too many requests|RESOURCE_EXHAUSTED|quota (?:exceeded|exhausted)|limit (?:reached|exceeded)",
    re.I,
)

# agent -> (logout, login) argv; agy signs in from inside the app, so only BROWSER is redirected for it.
LOGIN_CMDS: Dict[str, Tuple[Optional[List[str]], Optional[List[str]]]] = {
    "kiro": (["kiro-cli", "logout"], ["kiro-cli", "login"]),
    "agy": (None, None),
}
BINARIES = {"kiro": "kiro-cli", "agy": "agy"}


def family(agent: str) -> str:
    a = os.path.basename(agent).lower()
    return "kiro" if a.startswith("kiro") else "agy" if a in ("agy", "antigravity") else a


def screen_limited(text: str) -> bool:
    return bool(SCREEN_LIMIT.search(text[-700:]))


def _load() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data: Dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)


def current(agent: str, profiles: List[str]) -> Optional[str]:
    cur = _load().get(family(agent), {}).get("current")
    return cur if cur in profiles else (profiles[0] if profiles else None)


def mark_limited(agent: str, profile: str, until: Optional[float] = None) -> None:
    data = _load()
    slot = data.setdefault(family(agent), {})
    slot.setdefault("limited", {})[profile] = until or time.time() + COOLDOWN
    _save(data)


def next_profile(agent: str, profiles: List[str], now: Optional[float] = None) -> Optional[str]:
    """First profile after the current one that is not cooling down, or None when all are limited."""
    now = now or time.time()
    if len(profiles) < 2:
        return None
    limited = _load().get(family(agent), {}).get("limited", {})
    cur = current(agent, profiles)
    start = profiles.index(cur) if cur in profiles else -1
    for i in range(1, len(profiles) + 1):
        cand = profiles[(start + i) % len(profiles)]
        if cand != cur and limited.get(cand, 0) <= now:
            return cand
    return None


def browser_wrapper(profile: str) -> str:
    """A BROWSER script that opens URLs in the given Chrome profile."""
    os.makedirs(BROWSER_DIR, exist_ok=True)
    path = os.path.join(BROWSER_DIR, re.sub(r"[^\w.-]", "_", profile) + ".sh")
    if sys.platform == "darwin":
        body = f'open -na "Google Chrome" --args --profile-directory={shlex.quote(profile)} "$@"'
    else:
        body = f'exec google-chrome --profile-directory={shlex.quote(profile)} "$@"'
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def env_for(profile: str) -> Dict[str, str]:
    return {**os.environ, "BROWSER": browser_wrapper(profile)}


def switch(agent: str, profile: str, timeout: float = 180.0) -> bool:
    """Signs the agent in under `profile`. True when the profile is active (login finished or not needed)."""
    logout, login = LOGIN_CMDS.get(family(agent), (None, None))
    env = env_for(profile)
    ok = True
    if logout:
        subprocess.run(logout, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if login:
        try:
            ok = subprocess.run(login, env=env, timeout=timeout, check=False).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            ok = False
    if ok:
        data = _load()
        data.setdefault(family(agent), {})["current"] = profile
        _save(data)
    return ok


def handoff_prompt(agent: str, session=None, cwd: str = ".", screen_tail: str = "", reason: str = "usage limit") -> str:
    """What the next account needs to continue without redoing finished work."""
    parts = [f"The previous {family(agent)} session stopped ({reason}). Continue the same task from exactly where it left off. "
             "Do not redo finished work; check `git status` and `git diff` first."]
    if session is not None and getattr(session, "title", ""):
        parts.append(f"Original task: {session.title.strip()[:400]}")
    last = ""
    if session is not None:
        msgs = [m[1] for m in (getattr(session, "messages", None) or []) if m[1].strip()]
        last = (msgs[-1] if msgs else getattr(session, "final_message", "")).strip()
    if last:
        parts.append("Last thing the previous agent said:\n" + last[-1200:])
    elif screen_tail.strip():
        parts.append("Last terminal output:\n" + screen_tail.strip()[-900:])
    try:
        out = subprocess.run(["git", "-C", cwd, "status", "--short"], capture_output=True, text=True, timeout=5).stdout.strip()
        if out:
            parts.append("Files changed so far:\n" + "\n".join(out.splitlines()[:25]))
    except (OSError, subprocess.SubprocessError):
        pass
    return "\n\n".join(parts)


def save_handoff(cwd: str, agent: str, prompt: str) -> str:
    os.makedirs(HANDOFF_DIR, exist_ok=True)
    path = os.path.join(HANDOFF_DIR, re.sub(r"\W+", "_", os.path.realpath(cwd)) + f".{family(agent)}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(prompt)
    return path


def load_handoff(cwd: str, agent: str) -> Optional[str]:
    path = os.path.join(HANDOFF_DIR, re.sub(r"\W+", "_", os.path.realpath(cwd)) + f".{family(agent)}.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def resume_cmd(agent: str, prompt: str) -> List[str]:
    """Argv that starts the agent in the current directory with the handoff as its first message."""
    fam = family(agent)
    if fam == "kiro":
        return ["kiro-cli", "chat", "--resume", prompt]
    return [BINARIES.get(fam, agent), "--continue", "-i", prompt] if fam == "agy" else [agent, prompt]


def failover(agent: str, profiles: List[str], session=None, cwd: str = ".", screen_tail: str = "",
             until: Optional[float] = None, do_switch: bool = True) -> Optional[Tuple[str, str]]:
    """Marks the current profile limited, moves to the next one, returns (profile, handoff prompt) or None."""
    cur = current(agent, profiles)
    if cur:
        mark_limited(agent, cur, until)
    nxt = next_profile(agent, profiles)
    if not nxt:
        return None
    if do_switch and not switch(agent, nxt):
        return None
    prompt = handoff_prompt(agent, session, cwd, screen_tail)
    save_handoff(cwd, agent, prompt)
    return nxt, prompt
