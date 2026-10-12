"""First-run setup (`gitnolo setup`): connect AI via an OpenRouter key grabbed from the clipboard, or local Ollama.

Rendered as a vertical rail: `│` lines joined by `●` step dots.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from typing import List, Optional

from rich.text import Text

from . import ui
from .config import AppConfig

KEYS_URL = "https://openrouter.ai/settings/keys"
SIGNUP_URL = "https://openrouter.ai/sign-up"
KEY_RE = re.compile(r"sk-or-[A-Za-z0-9_\-]{20,}")


# ------------------------------------------------------------------ rail
class Rail:
    def __init__(self, title: str):
        ui.console.print()
        ui.console.print(Text.assemble(("┌  ", "faint"), (title, "bold accent")))
        self.bar()

    def bar(self) -> None:
        ui.console.print(Text("│", "faint"))

    def dot(self, text: str, style: str = "accent", sym: str = "●") -> None:
        ui.console.print(Text.assemble((f"{sym}  ", style), (text, "bold" if style == "accent" else style)))

    def note(self, text: str, style: str = "muted") -> None:
        ui.console.print(Text.assemble(("│  ", "faint"), (text, style)))

    def ask(self, label: str, options: str, default: str) -> str:
        if not sys.stdin.isatty():
            return default
        ui.console.print(Text.assemble(("◆  ", "info"), (label, "bold"), (f"  [{options}]", "muted")), end=" ")
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return default
        return ans[:1] or default

    def end(self, text: str) -> None:
        self.bar()
        ui.console.print(Text.assemble(("└  ", "faint"), (text, "ok")))
        ui.console.print()


# ------------------------------------------------------------------ helpers
def read_clipboard() -> str:
    cmds: List[List[str]] = []
    if sys.platform == "darwin":
        cmds = [["pbpaste"]]
    elif sys.platform.startswith("win"):
        cmds = [["powershell", "-NoProfile", "-Command", "Get-Clipboard"]]
    else:
        cmds = [["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "-ob"]]
    for c in cmds:
        if shutil.which(c[0]):
            try:
                return subprocess.run(c, capture_output=True, text=True, timeout=3).stdout or ""
            except Exception:
                pass
    return ""


def wait_for_key(timeout: float = 300.0) -> Optional[str]:
    """Polls the clipboard until an OpenRouter key shows up (ignores one already there)."""
    start = KEY_RE.search(read_clipboard())
    stale = start.group(0) if start else None
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    t0, i = time.time(), 0
    with ui.console.status("", spinner="dots") as st:
        while time.time() - t0 < timeout:
            st.update(Text.assemble(("│  ", "faint"), (f"{frames[i % len(frames)]} waiting for the key to hit your clipboard "
                                                        f"({int(timeout - (time.time() - t0))}s)", "muted")))
            m = KEY_RE.search(read_clipboard())
            if m and m.group(0) != stale:
                return m.group(0)
            i += 1
            time.sleep(0.5)
    return None


def key_valid(key: str) -> bool:
    try:
        req = urllib.request.Request("https://openrouter.ai/api/v1/auth/key", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def ollama_models(url: str) -> Optional[List[str]]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=2) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return None


# ------------------------------------------------------------------ flow
def run(config: AppConfig, force: bool = False) -> int:
    from .edition import is_community  # noqa: F401  (keeps both editions on one code path)

    r = Rail("gitnolo setup")
    from . import auth
    if not auth.step(r, config, force):
        r.end("Sign-in required · run `gitnolo login`")
        return 1
    r.dot("Checking for local models (Ollama)")
    models = ollama_models(config.ollama_url)
    if models:
        r.note(f"found {len(models)} model(s) · using {models[0]}", "ok")
        config.ai_provider, config.model_name = "ollama", config.model_name or models[0]
        config.onboarded = True
        config.save()
        r.end("Ollama configured automatically")
        return 0
    r.note("no Ollama running", "muted")
    r.bar()

    if config.openrouter_api_key and not force and key_valid(config.openrouter_api_key):
        r.dot("OpenRouter key already set", "ok")
        config.onboarded = True
        config.save()
        r.end("Ready")
        return 0

    r.dot("Connect OpenRouter (free models, no card)")
    has = r.ask("Do you have an OpenRouter account?", "y/n/skip", "y")
    if has == "s":
        config.ai_provider, config.onboarded = "off", True
        config.save()
        r.end("Skipped · heuristics only (run `gitnolo setup` any time)")
        return 0
    r.bar()
    if has == "n":
        r.dot("Sign up first, then come back to this window", "info", "○")
        r.note(SIGNUP_URL, "path")
        webbrowser.open(SIGNUP_URL)
        r.ask("Press enter once signed in", "enter", "")
        r.bar()

    r.dot("Create an API key")
    r.note(KEYS_URL, "path")
    webbrowser.open(KEYS_URL)
    r.note("click Create Key, then Copy · I'll take it from your clipboard", "muted")
    key = wait_for_key()
    if not key:
        r.bar()
        r.dot("Timed out · nothing copied", "warn", "▲")
        r.end("Run `gitnolo setup` to try again")
        return 1
    r.bar()
    r.dot("Verifying key")
    if not key_valid(key):
        r.note("OpenRouter rejected that key", "err")
        r.end("Run `gitnolo setup` to try again")
        return 1
    config.openrouter_api_key, config.ai_provider, config.onboarded = key, "openrouter", True
    config.save()
    if sys.platform == "darwin":
        subprocess.run(["pbcopy"], input=b"", check=False)  # don't leave the secret on the clipboard
    r.note(f"saved {key[:9]}…{key[-4:]} to ~/.gitnolo/config.json (0600) · clipboard cleared", "ok")
    r.end("AI connected")
    return 0
