"""Sign in with Google from the terminal (device-style flow against the server in server/gitnolo-auth).

The browser shows a session id; gitnolo either receives it by polling or grabs it from the clipboard.
"""
from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Any, Dict, Optional

from . import edition
from .config import AppConfig

DEFAULT_AUTH_URL = "https://festomanolo.com/gitnolo/auth"  # users can override: gitnolo config set auth_url ...
REQUIRED = False       # True: setup refuses to finish without a signed-in session
TOKEN_RE = re.compile(r"gn_[a-f0-9]{48}")


def base_url(config: AppConfig) -> str:
    return (os.environ.get("GITNOLO_AUTH_URL") or getattr(config, "auth_url", "") or DEFAULT_AUTH_URL).rstrip("/")


def _call(url: str, token: Optional[str] = None, body: Optional[dict] = None, method: str = "GET", timeout: float = 10):
    req = urllib.request.Request(url, method="POST" if body is not None else method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("User-Agent", "gitnolo")
    if token:
        req.add_header("X-Gitnolo-Token", token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


def _client_info() -> Dict[str, str]:
    from . import __version__
    return {"version": __version__, "os": platform.system().lower(), "edition": edition.EDITION}


def verify(config: AppConfig, token: str) -> Optional[Dict[str, Any]]:
    """Validates a session and reports this client; None if rejected/unreachable."""
    code, data = _call(base_url(config) + "/session.php?a=ping", token, _client_info())
    return data if code == 200 and data.get("email") else None


def heartbeat(config: AppConfig) -> None:
    """Fire-and-forget, at most every 6h: lets the admin dashboard see active clients."""
    if not (config.auth_token and base_url(config)) or time.time() - config.auth_last_ping < 6 * 3600:
        return
    def go() -> None:
        code, _ = _call(base_url(config) + "/session.php?a=ping", config.auth_token, _client_info(), timeout=5)
        if code == 200:
            config.auth_last_ping = time.time()
            config.save()
        elif code == 401:  # revoked or banned
            config.auth_token = config.auth_email = None
            config.save()
    threading.Thread(target=go, daemon=True).start()


def logout(config: AppConfig) -> None:
    if config.auth_token and base_url(config):
        _call(base_url(config) + "/session.php?a=logout", config.auth_token, {})
    config.auth_token = config.auth_email = None
    config.save()


def step(r, config: AppConfig, force: bool = False, timeout: float = 300.0) -> bool:
    """Rail step. Returns True to continue setup."""
    from .onboard import read_clipboard

    base = base_url(config)
    if not base:
        r.dot("Sign-in not configured on this build", "muted", "○")
        r.bar()
        return not REQUIRED
    if config.auth_token and not force:
        who = verify(config, config.auth_token)
        if who:
            r.dot(f"Signed in as {who['email']}", "ok")
            r.bar()
            return True
    r.dot("Sign in with Google")
    code, data = _call(base + "/start.php", body={})
    if code != 200 or "url" not in data:
        r.note("could not reach the sign-in server", "err")
        r.bar()
        return not REQUIRED
    r.note(data["url"], "path")
    webbrowser.open(data["url"])
    r.note("finish in the browser · it comes back here by itself (or copy the session id)", "muted")
    stale = TOKEN_RE.search(read_clipboard())
    stale = stale.group(0) if stale else None
    token, t0, i = None, time.time(), 0
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    from . import ui
    from rich.text import Text
    with ui.console.status("", spinner="dots") as st:
        while time.time() - t0 < timeout and not token:
            st.update(Text.assemble(("│  ", "faint"), (f"{frames[i % 10]} waiting for Google sign-in ({int(timeout - (time.time() - t0))}s)", "muted")))
            if i % 3 == 0:
                c, d = _call(f"{base}/poll.php?code={data['code']}")
                if c == 410:
                    break
                token = d.get("token") if c == 200 and d.get("status") == "ok" else None
            m = TOKEN_RE.search(read_clipboard())
            if not token and m and m.group(0) != stale:
                token = m.group(0)
            i += 1
            time.sleep(0.7)
    who = verify(config, token) if token else None
    if not who:
        r.note("sign-in did not complete", "warn")
        r.bar()
        return not REQUIRED
    config.auth_token, config.auth_email, config.auth_last_ping = token, who["email"], time.time()
    config.save()
    r.note(f"signed in as {who['email']}", "ok")
    r.bar()
    return True
