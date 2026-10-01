"""
Durable runtime state (~/.gitnolo/state.json): processed agent turns,
issue fingerprints, visibility cache, open work branches and run history.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from .config import CONFIG_DIR

STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
MAX_TURNS = 4000
MAX_HISTORY = 300


class State:
    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self._lock = threading.RLock()
        self.data: Dict[str, Any] = {"turns": [], "issues": {}, "visibility": {}, "branches": {}, "history": []}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except Exception:
            pass
        self._turns = set(self.data.get("turns", []))

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.data["turns"] = list(self._turns)[-MAX_TURNS:]
            self.data["history"] = self.data.get("history", [])[-MAX_HISTORY:]
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)

    # turns
    def turn_seen(self, turn_id: str) -> bool:
        return turn_id in self._turns

    def mark_turn(self, turn_id: str) -> None:
        with self._lock:
            self._turns.add(turn_id)

    # visibility cache
    def visibility(self, slug: str, ttl: float = 3600) -> Optional[bool]:
        v = self.data.get("visibility", {}).get(slug)
        if v and time.time() - v.get("t", 0) < ttl:
            return bool(v.get("private"))
        return None

    def set_visibility(self, slug: str, private: bool) -> None:
        with self._lock:
            self.data.setdefault("visibility", {})[slug] = {"private": private, "t": time.time()}

    # issue fingerprints
    def issue_known(self, repo: str, fp: str) -> Optional[int]:
        return self.data.get("issues", {}).get(repo, {}).get(fp)

    def add_issue(self, repo: str, fp: str, number: int) -> None:
        with self._lock:
            self.data.setdefault("issues", {}).setdefault(repo, {})[fp] = number

    # open work branches (PR not merged yet)
    def open_branch(self, repo: str) -> Optional[Dict[str, Any]]:
        return self.data.get("branches", {}).get(repo)

    def set_open_branch(self, repo: str, info: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            branches = self.data.setdefault("branches", {})
            if info is None:
                branches.pop(repo, None)
            else:
                branches[repo] = info

    # history
    def record(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            entry.setdefault("t", time.time())
            self.data.setdefault("history", []).append(entry)

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        return list(self.data.get("history", []))[-limit:]

    def totals_today(self) -> Dict[str, int]:
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        out = {"runs": 0, "commits": 0, "prs": 0, "merged": 0, "issues": 0}
        for h in self.data.get("history", []):
            if h.get("t", 0) >= start:
                out["runs"] += 1
                out["commits"] += int(h.get("commits", 0))
                out["prs"] += 1 if h.get("pr") else 0
                out["merged"] += 1 if h.get("merged") else 0
                out["issues"] += len(h.get("issues", []))
        return out
