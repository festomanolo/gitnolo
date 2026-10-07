"""
Live issue tracking.

Every message an agent writes is read as it lands in the transcript, mid-turn
included. A line that reports an unresolved problem is *snapped* into the
ledger immediately; a line that says something is now fixed closes the
matching ledger entry, whether or not it was ever published.

Snapped issues are published to GitHub after `live_issue_grace_seconds`, so a
problem the agent fixes a few seconds later never reaches the tracker. Issues
from a finished turn's final report are published at once. Published issues
are closed with a comment quoting the agent's own resolution line.

Matching is topic based (shared keywords, `issues.resolves`) or explicit
(`fixes #12`); a bare "all tests pass" closes nothing.
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "gitnolo.github", "gitnolo.gitcore",
]

import os
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import issues as issue_mod
from .agents import CLOSED, ERRORED, FINISHED, INTERRUPTED, LIMITED, Session
from .config import AppConfig
from .github import GitHub, GitHubError
from .gitlab import for_remote as forge_for_remote
from .gitcore import GitError, Repo
from .state import State

Note = Callable[[str, str], None]

OPEN = ("snapped", "open", "local")
GH_TTL = 600.0
MAX_ATTEMPTS = 5
SESSION_WINDOW = 600.0  # at most max_issues_per_turn snaps per session in this window


class LiveIssues:
    def __init__(self, config: AppConfig, state: State, note: Optional[Note] = None, catch_up: bool = False):
        self.config = config
        self.state = state
        self.note: Note = note or (lambda kind, text: None)
        self.catch_up = catch_up
        self.seen: Dict[str, Set[str]] = {}
        self._gh: Dict[str, Tuple[float, Optional[GitHub]]] = {}
        self._lock = state._lock  # the ledger lives in state; share its lock so saves never see a half-edit

    # ------------------------------------------------------------ reading
    def observe(self, sessions: List[Session]) -> int:
        """Processes messages not seen before. Returns how many ledger entries changed."""
        changed = 0
        for s in sessions:
            if not s.repo or not s.messages:
                continue
            seen = self.seen.get(s.key)
            if seen is None:
                seen = self.seen[s.key] = set()
                if not self.catch_up:
                    seen.update(m[0] for m in s.messages)  # history from before gitnolo started
                    continue
            for mid, text, _ts in s.messages:
                if mid in seen:
                    continue
                seen.add(mid)
                changed += self.process(s, text)
        return changed

    def process(self, s: Session, text: str) -> int:
        repo = s.repo or ""
        name = os.path.basename(repo)
        changed = 0
        with self._lock:
            if self.config.auto_close_issues:
                for line in issue_mod.resolution_lines(text):
                    nums = issue_mod.referenced_numbers(line)
                    for e in self.state.open_ledger(repo):
                        if (e.get("number") and e["number"] in nums) or issue_mod.resolves(line, e["title"], e.get("source", "")):
                            self._resolve(repo, e, line, s)
                            changed += 1
            turn_over = s.state in (FINISHED, INTERRUPTED, LIMITED, ERRORED, CLOSED)
            recent = [e for e in self.state.ledger(repo)
                      if e.get("session") == s.key and time.time() - e.get("t", 0) < SESSION_WINDOW]
            for line in issue_mod.live_candidates(text):
                if len(recent) >= self.config.max_issues_per_turn:
                    break
                d = issue_mod.draft_for(line, name, s.agent, s.title, live=not turn_over)
                if any(e.get("fp") == d.fingerprint or issue_mod.similar(e["title"], d.title)
                       for e in self.state.open_ledger(repo)):
                    continue
                e = self.state.ledger_add(repo, {
                    "status": "snapped", "title": d.title, "source": line, "body": d.body, "labels": d.labels,
                    "fp": d.fingerprint, "agent": s.agent, "session": s.key, "task": s.title,
                    "publish_at": time.time() + (0 if turn_over else self.config.live_issue_grace_seconds),
                })
                recent.append(e)
                changed += 1
                self.note("issue", f"{name}: snapped \"{d.title[:70]}\"")
        return changed

    def _resolve(self, repo: str, e: Dict[str, Any], line: str, s: Session) -> None:
        e["resolved_by"] = line
        e["resolved_at"] = time.time()
        e["resolver"] = s.agent
        name = os.path.basename(repo)
        if e.get("number"):
            e["status"] = "closing"
            self.note("ok", f"{name}: #{e['number']} resolved by {s.agent}, closing")
        else:
            e["status"] = "resolved" if e["status"] == "snapped" else "closed"
            self.note("ok", f"{name}: \"{e['title'][:60]}\" resolved by {s.agent}"
                      + (" before it was filed" if e["status"] == "resolved" else ""))

    # ------------------------------------------------------------ GitHub
    def github(self, repo: str) -> Optional[GitHub]:
        cached = self._gh.get(repo)
        if cached and time.time() - cached[0] < GH_TTL:
            return cached[1]
        gh = None
        try:
            remote = Repo(repo).remote_url()
            gh = forge_for_remote(remote, self.config) if remote else None
            if gh and not gh.token:
                gh = None
        except GitError:
            gh = None
        self._gh[repo] = (time.time(), gh)
        return gh

    def sync(self, only_repo: Optional[str] = None) -> int:
        """Publishes due snapped issues and closes resolved ones. Network; call off the UI thread."""
        done = 0
        now = time.time()
        with self._lock:
            work = {repo: [e for e in entries if e.get("status") in ("snapped", "closing")]
                    for repo, entries in self.state.data.get("ledger", {}).items()
                    if only_repo is None or repo == only_repo}
        for repo, entries in work.items():
            due = [e for e in entries if e["status"] == "closing"
                   or (e["status"] == "snapped" and now >= e.get("publish_at", 0))]
            if not due:
                continue
            gh = self.github(repo) if self.config.auto_issues else None
            name = os.path.basename(repo)
            existing: Optional[List[Dict[str, Any]]] = None
            for e in due:
                if e.get("attempts", 0) >= MAX_ATTEMPTS:
                    continue
                if e["status"] == "snapped":
                    if not gh:
                        with self._lock:
                            if e["status"] == "snapped":
                                e["status"] = "local"
                        done += 1
                        continue
                    try:
                        if existing is None:
                            existing = gh.list_issues("open")
                        dup = next((i for i in existing if issue_mod.similar(e["title"], i.get("title", ""))), None)
                        it = dup or gh.create_issue(e["title"], e.get("body", ""), e.get("labels"))
                    except GitHubError as err:
                        e["attempts"] = e.get("attempts", 0) + 1
                        self.note("warn", f"{name}: could not file issue: {err}")
                        continue
                    with self._lock:
                        # The agent may have fixed it while the request was in flight: close it next round.
                        status = "closing" if e["status"] == "resolved" else "open"
                        e.update(status=status, number=it["number"], url=it.get("html_url", ""), published_at=time.time())
                        self.state.add_issue(repo, e["fp"], it["number"])
                    self.note("issue", f"{name}: " + (f"linked to existing #{it['number']}" if dup else f"filed #{it['number']}")
                              + f" {e['title'][:60]}")
                    done += 1
                elif e["status"] == "closing":
                    if not gh:
                        continue
                    comment = (f"Resolved by **{e.get('resolver') or 'the agent'}**:\n\n> {e.get('resolved_by', '')}\n\n"
                               "_Closed automatically by gitnolo._")
                    try:
                        gh.close_issue(int(e["number"]), comment)
                    except GitHubError as err:
                        e["attempts"] = e.get("attempts", 0) + 1
                        self.note("warn", f"{name}: could not close #{e['number']}: {err}")
                        continue
                    with self._lock:
                        e.update(status="closed", closed_at=time.time())
                    self.note("ok", f"{name}: closed #{e['number']} {e['title'][:60]}")
                    done += 1
        if done:
            try:
                self.state.save()
            except OSError:
                pass
        return done

    def related(self, repo: str, session_key: Optional[str] = None) -> List[Tuple[int, str, str]]:
        """Published open issues for a repo (optionally one session), for the PR body."""
        return [(int(e["number"]), e.get("url", ""), e["title"]) for e in self.state.ledger(repo)
                if e.get("status") == "open" and e.get("number") and (not session_key or e.get("session") == session_key)]
