"""Anonymous usage counters (numbers only, never code, paths or repo names) sent with the sign-in heartbeat."""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Dict

from .config import CONFIG_DIR

_FILE = os.path.join(CONFIG_DIR, "usage.json")
_LOCK = threading.Lock()
_OK = re.compile(r"^[a-z0-9_:.\-]{1,40}$")


def _load() -> Dict[str, int]:
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            return {k: int(v) for k, v in json.load(f).items()}
    except Exception:
        return {}


def _save(d: Dict[str, int]) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        pass


def bump(metric: str, n: int = 1) -> None:
    metric = metric.lower().replace(" ", "_")
    if n <= 0 or not _OK.match(metric):
        return
    with _LOCK:
        d = _load()
        d[metric] = d.get(metric, 0) + n
        _save(d)


def peek() -> Dict[str, int]:
    with _LOCK:
        return _load()


def subtract(sent: Dict[str, int]) -> None:
    """Removes counters that were delivered (anything bumped meanwhile is kept)."""
    with _LOCK:
        cur = _load()
        for k, v in sent.items():
            left = cur.get(k, 0) - v
            if left > 0:
                cur[k] = left
            else:
                cur.pop(k, None)
        _save(cur)
