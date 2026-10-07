"""
Live multi-repo watcher.

Turns agent lifecycle events into pipeline runs:
  1. an agent turn finishes (transcript) or an agent process exits,
  2. no other agent is still working in that repository,
  3. the working tree has stopped changing for `settle_seconds`,
then the repo is committed/pushed/PR'd/merged in a background worker while the
dashboard keeps updating. Agents without transcripts fall back to a longer
quiet period (`fallback_idle_seconds`).
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "queue", "threading", "rich.console", "rich.live", "rich.text", "gitnolo", "gitnolo.agents",
    "gitnolo.config", "gitnolo.gitcore", "gitnolo.pipeline", "gitnolo.state", "gitnolo.live_issues", "gitnolo.scene",
    "gitnolo.checkpoints",
]

import os
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from rich.console import Group
from rich.live import Live
from rich.text import Text

from . import __version__, checkpoints, scene, ui
from .agents import FINISHED, RUNNING, AgentEvent, Session, Tracker
from .live_issues import LiveIssues
from .config import AppConfig
from .gitcore import GitError, Repo
from .pipeline import Pipeline, RunOptions, RunResult
from .state import State


@dataclass
class Pending:
    repo: str
    reason: str
    session: Optional[Session]
    since: float = field(default_factory=time.time)
    sig: str = ""
    sig_at: float = 0.0


class Watcher:
    def __init__(self, config: AppConfig, auto: bool = True, dry_run: bool = False, catch_up: bool = False, plain: bool = False):
        self.config = config
        self.state = State()
        self.auto = auto
        self.dry_run = dry_run
        self.catch_up = catch_up or config.catch_up
        self.plain = plain or not sys.stdout.isatty()
        self.tracker = Tracker(config.monitored_agents, config.transcript_horizon_hours)
        self.pending: Dict[str, Pending] = {}
        self.log: Deque[Tuple[float, str, str]] = deque(maxlen=12)
        self.jobs: "queue.Queue[Pending]" = queue.Queue()
        self.active_job: Optional[str] = None
        self.active_step: str = ""
        self.active_stage: str = ""
        self.live = LiveIssues(config, self.state, self.note, catch_up=self.catch_up) if config.live_issues else None
        self.lanes = scene.Lanes()
        self.last_checkpoint: Dict[str, float] = {}
        self.repo_changes: Dict[str, Tuple[float, int]] = {}
        self.proc_sigs: Dict[str, Tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.offline = False

    # ------------------------------------------------------------ logging
    def note(self, kind: str, text: str) -> None:
        with self._lock:
            self.log.append((time.time(), kind, text))
        if self.plain:
            stamp = time.strftime("%H:%M:%S")
            ui.console.print(Text.assemble((stamp + "  ", "faint"), (kind.ljust(6), _KIND_STYLE.get(kind, "muted")), text))

    # ------------------------------------------------------------ loop
    def run(self) -> None:
        sessions, _ = self.tracker.poll()
        for s in sessions:
            if s.turn_id and not self.catch_up:
                self.state.mark_turn(s.turn_id)
            elif s.state == FINISHED and s.turn_id and s.repo and not self.state.turn_seen(s.turn_id):
                self.pending[s.repo] = Pending(s.repo, f"{s.agent} finished (catch-up)", s)
        self.note("info", f"watching {len(sessions)} agent sessions; {'auto' if self.auto else 'confirm'} mode"
                  + ("; dry run" if self.dry_run else ""))
        if self.auto:
            threading.Thread(target=self._worker, daemon=True).start()
        if not self.plain:
            threading.Thread(target=self._refresh_changes, daemon=True).start()
        threading.Thread(target=self._probe_network, daemon=True).start()
        if self.live:
            self.live.observe(sessions)  # baseline: history from before the watcher started
            if not self.dry_run:  # a dry run snaps issues but never publishes or closes them
                threading.Thread(target=self._sync_issues, daemon=True).start()
        try:
            if self.plain:
                while not self._stop.is_set():
                    self._tick()
                    time.sleep(1.0)
            else:
                with Live(self.render(), console=ui.console, refresh_per_second=8, transient=False, screen=False) as live:
                    while not self._stop.is_set():
                        self._tick(live)
                        live.update(self.render())
                        time.sleep(0.25 if self.active_job else 0.8)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            self.state.save()
            ui.console.print(Text("  watcher stopped", style="muted"))

    def _tick(self, live: Optional[Live] = None) -> None:
        sessions, events = self.tracker.poll()
        for ev in events:
            self._on_event(ev)
        if self.live:
            self.live.observe(sessions)
        self._fallback_triggers(sessions)
        self._check_pending(live)

    def _on_event(self, ev: AgentEvent) -> None:
        s = ev.session
        where = s.repo_name
        if ev.kind == "turn_finished":
            if not s.repo or (s.turn_id and self.state.turn_seen(s.turn_id)):
                return
            self.note("done", f"{s.agent} finished in {where}" + (f": {s.title[:50]}" if s.title else ""))
            self.pending[s.repo] = Pending(s.repo, f"{s.agent} finished", s)
            if self.config.notify:
                ui.notify("gitnolo", f"{s.agent} finished in {where}")
        elif ev.kind == "waiting":
            self.note("wait", f"{s.agent} in {where} is waiting for you" + (f" ({s.detail.split(':', 1)[-1][:50]})" if s.detail else ""))
            if self.config.notify:
                ui.notify("gitnolo", f"{s.agent} in {where} needs approval")
        elif ev.kind == "stopped":
            self.note("stop", f"{s.agent} in {where} was interrupted")
            if s.repo and s.turn_id and not self.state.turn_seen(s.turn_id):
                self.pending[s.repo] = Pending(s.repo, f"{s.agent} interrupted by user, saving partial work", s)
        elif ev.kind == "stalled":
            self.note("stall", f"{s.agent} in {where} has gone quiet for {ui.ago(s.updated)}")
        elif ev.kind in ("limited", "errored", "closed"):
            why = s.stop_reason or ev.kind
            extra = ""
            if s.resume_at:
                extra = f", resets {time.strftime('%H:%M', time.localtime(s.resume_at))} (in {_until(s.resume_at)})"
            self.note("stop" if ev.kind != "limited" else "wait",
                      f"{s.agent} in {where} stopped: {why}{extra}" + (f" ({s.stop_detail[:60]})" if s.stop_detail and not extra else ""))
            if self.config.notify:
                ui.notify("gitnolo", f"{s.agent} in {where} stopped: {why}{extra}")
            if s.repo and s.turn_id and not self.state.turn_seen(s.turn_id):
                self.pending[s.repo] = Pending(s.repo, f"{s.agent} stopped ({why}), saving partial work", s)
        elif ev.kind == "limit_reset":
            self.note("start", f"{s.agent} usage limit has reset; {where} can continue")
            if self.config.notify:
                ui.notify("gitnolo", f"{s.agent} limit reset: resume work in {where}")
        elif ev.kind == "exited":
            self.note("exit", f"{s.agent} (pid {s.pid}) exited in {where}")
            if s.repo and s.repo not in self.pending:
                self.pending[s.repo] = Pending(s.repo, f"{s.agent} exited", s)
        elif ev.kind == "started":
            self.note("start", f"{s.agent} started in {where}")
            self._checkpoint(s)
        elif ev.kind == "turn_started":
            self._checkpoint(s)
        elif ev.kind == "looping":
            self.note("loop", f"{s.agent} in {where} looks stuck: {s.detail.split(':', 1)[-1]}")
            if self.config.notify:
                ui.notify("gitnolo", f"{s.agent} in {where} looks stuck in a loop")

    def _checkpoint(self, s: Session) -> None:
        """Snapshots the repo as the agent starts working, in the background (gitnolo rewind)."""
        if not (self.config.checkpoints and s.repo) or self.dry_run:
            return
        now = time.time()
        if now - self.last_checkpoint.get(s.repo, 0) < 60:
            return
        self.last_checkpoint[s.repo] = now
        repo_path, label = s.repo, f"{s.agent} started: {(s.title or 'new turn')[:60]}"

        def work() -> None:
            try:
                cp = checkpoints.create(Repo(repo_path), label, self.config.checkpoint_keep)
            except GitError as e:
                self.note("warn", f"checkpoint failed: {e}")
                return
            if cp:
                self.note("ckpt", f"{s.repo_name}: checkpoint {cp.short} (gitnolo rewind)")

        threading.Thread(target=work, daemon=True).start()

    def _sync_issues(self) -> None:
        while not self._stop.is_set():
            try:
                self.live.sync()  # type: ignore[union-attr]
            except Exception as e:  # never let the thread die
                self.note("warn", f"issue sync: {e}")
            self._stop.wait(3.0)

    def _fallback_triggers(self, sessions: List[Session]) -> None:
        """Process-only agents (no transcript): commit after a long quiet period."""
        now = time.time()
        for s in sessions:
            if s.state != RUNNING or not s.repo or s.repo in self.pending:
                continue
            prev = self.proc_sigs.get(s.repo)
            if prev and now - prev[1] < 5:
                continue
            try:
                sig = Repo(s.repo).status_signature()
            except GitError:
                continue
            if not sig:
                self.proc_sigs[s.repo] = ("", now)
                continue
            if prev and prev[0] == sig:
                quiet_since = self.proc_sigs.get(s.repo + "#since", (sig, prev[1]))[1]
                if now - quiet_since >= self.config.fallback_idle_seconds:
                    self.pending[s.repo] = Pending(s.repo, f"{s.agent} idle {int(now - quiet_since)}s", s)
                    self.proc_sigs.pop(s.repo + "#since", None)
            else:
                self.proc_sigs[s.repo + "#since"] = (sig, now)
            self.proc_sigs[s.repo] = (sig, now)

    def _check_pending(self, live: Optional[Live]) -> None:
        now = time.time()
        for repo, p in list(self.pending.items()):
            exclude = p.session.key if p.session else None
            if self.tracker.busy_in_repo(repo, exclude_key=exclude):
                continue
            if self.active_job == repo:
                continue
            try:
                r = Repo(repo)
                sig = r.status_signature()
            except GitError:
                self.pending.pop(repo, None)
                continue
            if not sig:
                if p.session and p.session.turn_id:
                    self.state.mark_turn(p.session.turn_id)
                self.note("info", f"{Repo(repo).name}: nothing to commit")
                self.pending.pop(repo, None)
                continue
            if sig != p.sig:
                p.sig, p.sig_at = sig, now
                continue
            if now - p.sig_at < self.config.settle_seconds:
                continue
            self.pending.pop(repo, None)
            if self.auto:
                self.jobs.put(p)
            else:
                if live:
                    live.stop()
                try:
                    self._execute(p, interactive=True)
                finally:
                    if live:
                        live.start()

    # ------------------------------------------------------------ jobs
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                p = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._execute(p, interactive=False)
            except Exception as e:  # never let the worker die
                self.note("error", f"{p.repo}: {e}")
            finally:
                self.active_job = None
                self.active_step = ""
                self.active_stage = ""

    def _execute(self, p: Pending, interactive: bool) -> RunResult:
        self.active_job = p.repo
        s = p.session
        name = Repo(p.repo).name
        self.note("run", f"{name}: {p.reason}, starting pipeline")

        def emit(kind: str, text: str) -> None:
            if kind == "stage":
                self.active_stage = text
                return
            self.active_step = text
            if kind in ("ok", "warn", "error", "step"):
                self.note({"step": "step", "ok": "ok", "warn": "warn", "error": "error"}[kind], f"{name}: {text}")
            if interactive:
                ui.emit_printer(kind, text)

        confirm = None
        if interactive:
            from .cli import confirm_plan

            confirm = confirm_plan
        related: List[Tuple[int, str, str]] = []
        if self.live and not self.dry_run:
            self.live.sync(p.repo)  # the final report's issues are due at once; file them before the PR
            related = self.live.related(p.repo, s.key if s else None)
        pipe = Pipeline(self.config, self.state, emit)
        res = pipe.run(
            p.repo,
            RunOptions(
                dry_run=self.dry_run,
                agent=s.agent if s else "",
                session_title=s.title if s else "",
                final_message=s.final_message if s else "",
                partial=bool(s and s.partial),
                stop_reason=s.stop_reason if s else "",
                issues=False if self.live else None,
                related_issues=related,
                confirm=confirm,
            ),
        )
        # A run that wrote commits but failed later (e.g. push) is not retried: a retry
        # would rebuild every commit on a new branch and fail the same way.
        if s and s.turn_id and (res.ok or res.commits or "nothing" in " ".join(res.notes)):
            self.state.mark_turn(s.turn_id)
            self.state.save()
        if res.ok and res.commits:
            summary = f"{name}: {res.commits} commits" + (f", PR #{res.pr_number}" if res.pr_number else "") + (
                " merged" if res.merged else "") + (f", {len(res.issues)} issues" if res.issues else "") + f" in {res.elapsed:.1f}s"
            self.note("ok", summary)
            if self.config.notify:
                ui.notify("gitnolo", summary)
        elif not res.ok:
            self.note("error", f"{name}: {res.error}")
        self.active_job = None
        self.active_step = ""
        self.active_stage = ""
        return res

    # ------------------------------------------------------------ view
    def render(self) -> Group:
        rows = self.tracker.visible()
        today = self.state.totals_today()
        net = Text.assemble(("  ", ""), ("offline", "err")) if self.offline else Text("")
        head = Text.assemble(
            (ui.STAR + " ", "accent"), ("gitnolo", "accent.bold"), (f" v{__version__}", "muted"), ("  watch", "bold"),
            ("   ", ""), (f"{sum(1 for r in rows if r.state in ('working', 'running'))} working", "accent"),
            ("  ", ""), (f"{sum(1 for r in rows if r.state == 'waiting')} waiting", "warn"),
            ("   today ", "muted"), (f"{today['commits']} commits  {today['prs']} PRs  {today['merged']} merged", ""),
            ("   issues ", "muted"), (f"{len(self.state.open_ledger())} open", "warn" if self.state.open_ledger() else "faint"),
        )
        world: List[Any] = []
        if self.config.show_map and self.config.animations_enabled:
            world = self.lanes.render(rows, ui.console.width, now=time.time(), limit=4)
            job = self.active_job
            if job:
                world += [Text(""), scene.pipeline_map(os.path.basename(job), self.active_stage)]
            if world:
                world = [Text("")] + world
        t = ui.table("", "agent", "repository", "task*", "activity", "idle", "changes")
        now = time.time()
        for s in rows[:12]:
            changes = self._changes(s.repo, now)
            pending = s.repo in self.pending if s.repo else False
            act = s.detail.replace("tool:", "").replace("approval:", "approve: ").replace("needs input:", "input: ")
            if s.stop_reason and s.stop_reason != "completed":
                act = s.stop_reason + (f", resets in {_until(s.resume_at)}" if s.resume_at and s.resume_at > now else "")
                if s.stop_reason == "stuck in a loop":
                    act = "stuck in a loop, needs you"
            if self.active_job and s.repo == self.active_job:
                act = Text.assemble((ui.spinner_frame() + " ", "accent"), (self.active_step[:40], "accent"))
            elif pending:
                act = Text("queued: settling", style="info")
            t.add_row(
                ui.state_badge(s.state),
                Text(s.agent, style="bold"),
                Text(s.repo_name, style="path"),
                Text((s.title or "")[:34], style="muted"),
                act if isinstance(act, Text) else Text(act[:36], style="muted"),
                Text(ui.ago(s.updated), style="faint"),
                Text(f"{changes} files" if changes else "clean", style="warn" if changes else "faint"),
            )
        if not rows:
            t.add_row("", Text("no agents yet", style="muted"), "", "", "", "", "")
        with self._lock:
            entries = list(self.log)
        log_lines = [
            Text.assemble((time.strftime("%H:%M:%S", time.localtime(ts)) + "  ", "faint"),
                          (_KIND_GLYPH.get(kind, "·") + " ", _KIND_STYLE.get(kind, "muted")), (text, ""))
            for ts, kind, text in entries
        ]
        foot = Text("  ctrl+c to stop" + ("  ·  auto: commits, pushes, opens and merges PRs" if self.auto else "  ·  confirm mode"),
                    style="faint")
        head.append_text(net)
        return Group(Text(""), head, *world, Text(""), t, Text(""), *log_lines, Text(""), foot)

    def _changes(self, repo: Optional[str], now: float) -> int:
        cached = self.repo_changes.get(repo or "")
        return cached[1] if cached else 0

    def _probe_network(self) -> None:
        """Cheap connectivity probe so 'agent stopped' can be explained by an outage."""
        import socket

        while not self._stop.is_set():
            ok = False
            for host in ("1.1.1.1", "8.8.8.8"):
                try:
                    with socket.create_connection((host, 443), timeout=3):
                        ok = True
                        break
                except OSError:
                    continue
            if ok == self.offline:  # state flipped
                self.offline = not ok
                self.note("warn" if self.offline else "info", "network is down: agents will stall" if self.offline else "network is back")
            self._stop.wait(20.0)

    def _refresh_changes(self) -> None:
        """Background: keeps per-repo dirty counts fresh without blocking the UI."""
        while not self._stop.is_set():
            repos = {s.repo for s in self.tracker.visible() if s.repo}
            for repo in repos:
                try:
                    out = Repo(repo).run("status", "--porcelain=v1", "-z", check=False)
                    n = len([e for e in out.split("\0") if len(e) > 3])
                except Exception:
                    n = 0
                self.repo_changes[repo] = (time.time(), n)
                if self._stop.is_set():
                    return
            self._stop.wait(5.0)


def _until(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    d = max(0, int(ts - time.time()))
    return f"{d // 3600}h {d % 3600 // 60:02d}m" if d >= 3600 else f"{d // 60}m"


_KIND_GLYPH = {"done": "✓", "ok": ui.DOT, "run": ui.DOT, "step": ui.ELBOW, "warn": "!", "error": "×", "wait": "◆",
               "stop": "■", "stall": "◌", "exit": "○", "start": "●", "info": "·", "issue": "⚑", "loop": "↻", "ckpt": "◈"}
_KIND_STYLE = {"done": "ok", "ok": "ok", "run": "accent", "step": "muted", "warn": "warn", "error": "err", "wait": "warn",
               "stop": "err", "stall": "muted", "exit": "faint", "start": "info", "info": "muted", "issue": "warn",
               "loop": "warn", "ckpt": "info"}
