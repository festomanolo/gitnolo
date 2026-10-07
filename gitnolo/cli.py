"""
gitnolo command line.
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "argparse", "rich.prompt", "rich.text", "gitnolo", "gitnolo.agents", "gitnolo.config",
    "gitnolo.gitcore", "gitnolo.state",
]

import argparse
import os
import sys
import time
from typing import Any, Dict, List, Optional

from rich.prompt import Confirm, Prompt
from rich.text import Text

from . import __version__, edition, lens, ui
from .agents import FINISHED, Session, Tracker
from .config import AppConfig
from .gitcore import GitError, Repo
from .state import State


# ------------------------------------------------------------------ helpers
def _repo(path: Optional[str]) -> Repo:
    try:
        return Repo(path or ".")
    except GitError as e:
        ui.error(str(e))
        sys.exit(2)


def latest_session(repo_root: str, config: AppConfig) -> Optional[Session]:
    """Most recent agent session for a repository (to reuse its title and final report)."""
    tr = Tracker(config.monitored_agents, config.transcript_horizon_hours, processes=False)
    tr.poll()
    cands = [s for s in tr.sessions.values() if s.repo and os.path.samefile(s.repo, repo_root)]
    finished = [s for s in cands if s.state == FINISHED and s.final_message]
    pool = finished or cands
    return max(pool, key=lambda s: s.updated) if pool else None


def confirm_plan(plan: Any, ctx: Dict[str, Any]) -> bool:
    add, rem = plan.stats()
    policy = "private" if ctx["is_private"] else "public"
    where = {
        "pr": f"new branch -> PR into {ctx['default']} -> merge",
        "feature-branch": f"{ctx['current']} -> PR into {ctx['default']}",
        "direct": f"commit on {ctx['current'] or 'HEAD'}",
    }[ctx["mode"]]
    ui.step(f"Plan: {len(plan.commits)} commits  ({policy} policy)")
    ui.detail(f"{len(plan.files)} files  +{add} -{rem}  ·  {where}")
    t = ui.table("#", "subject*")
    show = plan.commits if len(plan.commits) <= 12 else plan.commits[:8] + [None] + plan.commits[-3:]
    for i, c in enumerate(show):
        if c is None:
            t.add_row("", Text(f"… {len(plan.commits) - 11} more", style="muted"))
        else:
            idx = plan.commits.index(c) + 1
            t.add_row(Text(str(idx), style="faint"), c.subject)
    ui.console.print(t)
    return Confirm.ask(Text("  Proceed?", style="accent"), default=True)


def _print_result(res: Any) -> None:
    if res.error:
        ui.error(res.error)
        return
    if not res.commits:
        return
    parts = [f"{res.commits} commits"]
    if res.pr_url:
        parts.append(f"PR #{res.pr_number}" + (" merged" if res.merged else " open"))
    if res.issues:
        parts.append(f"{len(res.issues)} issues")
    ui.ok(" · ".join(parts) + f"  in {res.elapsed:.1f}s")
    if res.pr_url:
        ui.detail(res.pr_url, "info")
    for n, url, title in res.issues:
        ui.detail(f"#{n} {title}", "muted")


# ------------------------------------------------------------------ commands
def cmd_commit(args: argparse.Namespace, config: AppConfig) -> int:
    from .pipeline import Pipeline, RunOptions

    repo = _repo(args.repo)
    session = latest_session(repo.root, config) if not args.no_agent else None
    if session:
        ui.detail(f"agent context: {session.agent}" + (f" · {session.title[:60]}" if session.title else ""), "faint")
    target = 1 if args.single else args.target
    opts = RunOptions(
        target=target,
        push=False if args.no_push else None,
        pr=False if args.no_pr else None,
        merge=False if args.no_merge else None,
        issues=False if args.no_issues else None,
        use_ai=not args.no_ai,
        dry_run=args.dry_run,
        agent=session.agent if session else "",
        session_title=args.title or (session.title if session else ""),
        final_message=session.final_message if session else "",
        confirm=None if args.yes else confirm_plan,
    )
    res = Pipeline(config, State(), ui.emit_printer).run(repo.root, opts)
    if args.dry_run and res.subjects:
        t = ui.table("#", "subject*")
        for i, s in enumerate(res.subjects[:40], 1):
            t.add_row(Text(str(i), style="faint"), s)
        ui.console.print(t)
        if len(res.subjects) > 40:
            ui.detail(f"… {len(res.subjects) - 40} more")
    _print_result(res)
    return 0 if res.ok else 1


def cmd_watch(args: argparse.Namespace, config: AppConfig) -> int:
    from .watcher import Watcher

    Watcher(config, auto=args.yes, dry_run=args.dry_run, catch_up=args.catch_up, plain=args.plain).run()
    return 0


def agents_table(tr: Tracker, show_all: bool = False):
    rows = tr.visible(24 * 3600 if show_all else 3 * 3600)
    t = ui.table("", "agent", "repository", "task*", "activity", "idle", "pid")
    for s in rows:
        t.add_row(
            ui.state_badge(s.state, animate=False),
            Text(s.agent, style="bold"),
            Text(s.repo_name, style="path"),
            Text((s.title or "")[:40], style="muted"),
            Text(s.detail.replace("tool:", "")[:30], style="muted"),
            Text(ui.ago(s.updated), style="faint"),
            Text(str(s.pid or ""), style="faint"),
        )
    if not rows:
        t.add_row("", Text("no agent activity", style="muted"), "", "", "", "", "")
    return t


def cmd_agents(args: argparse.Namespace, config: AppConfig) -> int:
    tr = Tracker(config.monitored_agents, config.transcript_horizon_hours if not args.all else 24)
    tr.poll()
    if not args.live:
        ui.step("Agents")
        ui.console.print(agents_table(tr, args.all))
        return 0
    from rich.live import Live

    try:
        with Live(agents_table(tr, args.all), console=ui.console, refresh_per_second=4) as live:
            while True:
                time.sleep(1)
                tr.poll()
                live.update(agents_table(tr, args.all))
    except KeyboardInterrupt:
        return 0


def cmd_issues(args: argparse.Namespace, config: AppConfig) -> int:
    from . import issues
    from .ai import make_client
    from .gitlab import for_remote as forge_for_remote

    repo = _repo(args.repo)
    if args.list:
        return _issues_list(repo, State(), args.all)
    s = latest_session(repo.root, config)
    if not s or not s.final_message:
        ui.warn("No finished agent report found for this repository")
        return 1
    ui.step(f"Issues from {s.agent}'s last report" + (f" ({s.title[:50]})" if s.title else ""))
    llm = None if args.no_ai else make_client(config)
    drafts = issues.extract(s.final_message, repo.name, s.agent, s.title, llm=llm, limit=config.max_issues_per_turn)
    if not drafts:
        ui.detail("No unresolved problems found in the report")
        return 0
    for d in drafts:
        ui.detail(f"{d.title}  [{', '.join(d.labels)}]", "")
    if args.dry_run:
        return 0
    gh = forge_for_remote(repo.remote_url(), config)
    if not gh or not gh.token:
        ui.error("No GitHub/GitLab remote or token available")
        return 1
    if not args.yes and not Confirm.ask(Text(f"  Open {len(drafts)} issue(s) on {gh.slug}?", style="accent"), default=True):
        return 0
    state = State()
    existing = gh.list_issues()
    for d in drafts:
        dup = next((i for i in existing if issues.similar(d.title, i.get("title", ""))), None)
        if dup or state.issue_known(repo.root, d.fingerprint):
            ui.detail(f"already tracked: {d.title[:60]}", "faint")
            continue
        it = gh.create_issue(d.title, d.body, d.labels)
        state.add_issue(repo.root, d.fingerprint, it["number"])
        ui.detail(f"#{it['number']} {it.get('html_url', '')}", "ok")
    state.save()
    return 0


def _issues_list(repo: Repo, state: State, show_all: bool) -> int:
    entries = state.ledger(repo.root)
    if not show_all:
        entries = [e for e in entries if e.get("status") in ("snapped", "open", "local", "closing")]
    ui.step(f"Agent-reported issues in {repo.name}" + ("" if show_all else " (open; --all for history)"))
    styles = {"snapped": "info", "open": "warn", "local": "warn", "closing": "ok", "closed": "ok", "resolved": "faint"}
    t = ui.table("status", "#", "title*", "agent", "age")
    for e in reversed(entries[-60:]):
        t.add_row(Text(e.get("status", ""), style=styles.get(e.get("status", ""), "muted")),
                  Text(str(e.get("number") or ""), style="hash"), e["title"], Text(e.get("agent") or "", style="muted"),
                  Text(ui.ago(e.get("t")), style="faint"))
    if not entries:
        t.add_row("", "", Text("nothing tracked yet; gitnolo watch snaps problems as agents report them", style="muted"), "", "")
    ui.console.print(t)
    return 0


def cmd_pr(args: argparse.Namespace, config: AppConfig) -> int:
    from .gitlab import for_remote as forge_for_remote

    repo = _repo(args.repo)
    gh = forge_for_remote(repo.remote_url(), config)
    if not gh or not gh.token:
        ui.error("No GitHub/GitLab remote or token available")
        return 1
    if args.action == "merge":
        if not args.number:
            ui.error("usage: gitnolo pr merge <number>")
            return 2
        ok, detail = gh.merge_pr(int(args.number), args.method or config.merge_method)
        (ui.ok if ok else ui.error)(f"PR #{args.number} " + ("merged" if ok else f"not merged: {detail}"))
        return 0 if ok else 1
    prs = gh.list_prs()
    ui.step(f"Open pull requests on {gh.slug}")
    t = ui.table("#", "title*", "head", "age")
    for p in prs:
        from datetime import datetime

        ts = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")).timestamp()
        t.add_row(Text(str(p["number"]), style="hash"), p["title"], Text(p["head"]["ref"], style="branch"), Text(ui.ago(ts), style="muted"))
    ui.console.print(t)
    return 0


def cmd_supervise(args: argparse.Namespace, config: AppConfig) -> int:
    from .auto_accept import supervise_command
    from .pipeline import Pipeline, RunOptions

    if not args.agent_cmd:
        ui.error("usage: gitnolo supervise <agent command...>")
        return 2
    ui.step(f"Supervising {' '.join(args.agent_cmd)}")
    rapid = config.rapid_response and not args.no_rapid
    ui.detail("auto-approving prompts; destructive commands are left to you"
              + (f"; questions get option 1 after {int(config.rapid_choice_delay)}s once you are away "
                 f"{int(config.rapid_away_seconds)}s" if rapid else ""), "faint")
    try:
        from . import checkpoints

        cp = checkpoints.create(Repo("."), f"before {args.agent_cmd[0]} (supervise)", config.checkpoint_keep) if config.checkpoints else None
        if cp:
            ui.detail(f"checkpoint {cp.short}; undo everything with gitnolo rewind", "faint")
    except GitError:
        pass
    code = supervise_command(args.agent_cmd, auto_accept=True, notify=ui.notify if config.notify else None, rapid=rapid,
                             away_seconds=config.rapid_away_seconds, choice_delay=config.rapid_choice_delay)
    ui.detail(f"{args.agent_cmd[0]} exited with code {code}")
    try:
        repo = Repo(".")
    except GitError:
        return code
    if repo.has_changes():
        session = latest_session(repo.root, config)
        res = Pipeline(config, State(), ui.emit_printer).run(
            repo.root,
            RunOptions(agent=args.agent_cmd[0], session_title=session.title if session else "",
                       final_message=session.final_message if session else "", confirm=None if args.yes else confirm_plan),
        )
        _print_result(res)
    return code


def cmd_map(args: argparse.Namespace, config: AppConfig) -> int:
    from rich.console import Group
    from rich.live import Live

    from . import scene

    tr = Tracker(config.monitored_agents, config.transcript_horizon_hours)
    lanes = scene.Lanes()

    def frame():
        rows = tr.visible()
        head = Text.assemble((ui.STAR + " ", "accent"), ("gitnolo", "accent.bold"), ("  map", "bold"),
                             ("   ", ""), (f"{sum(1 for r in rows if r.state in ('working', 'running'))} driving", "accent"),
                             ("  ", ""), (f"{sum(1 for r in rows if r.state == 'waiting')} waiting", "warn"),
                             ("  ", ""), (f"{sum(1 for r in rows if r.state == 'finished')} parked", "ok"))
        body = lanes.render(rows, ui.console.width, limit=args.limit) or [Text("  no agents on the road", style="muted")]
        return Group(Text(""), head, Text(""), *body, Text(""), Text("  ctrl+c to stop", style="faint"))

    tr.poll()
    try:
        with Live(frame(), console=ui.console, refresh_per_second=10) as live:
            n = 0
            while True:
                time.sleep(0.1)
                n += 1
                if n % 8 == 0:
                    tr.poll()
                live.update(frame())
    except KeyboardInterrupt:
        return 0


def cmd_rewind(args: argparse.Namespace, config: AppConfig) -> int:
    from . import checkpoints

    repo = _repo(args.repo)
    if args.save is not None:
        cp = checkpoints.create(repo, args.save or "manual checkpoint", config.checkpoint_keep)
        ui.ok(f"checkpoint {cp.short}" if cp else "nothing changed since the last checkpoint")
        return 0
    cps = checkpoints.list_all(repo)
    if not cps:
        ui.warn("No checkpoints yet. gitnolo watch takes one whenever an agent starts a turn; gitnolo rewind --save takes one now")
        return 1
    if not args.checkpoint:
        ui.step(f"Checkpoints in {repo.name} (newest first)")
        t = ui.table("#", "taken", "label*", "files differ")
        for i, cp in enumerate(cps[: args.limit], 1):
            try:
                changed, added, deleted = checkpoints.diff_from_now(repo, cp) if i <= 10 else ([], [], [])
                diff = f"{len(changed) + len(added) + len(deleted)}" if i <= 10 else ""
            except GitError:
                diff = "?"
            t.add_row(Text(str(i), style="accent"), Text(ui.ago(cp.t) + " ago", style="muted"), cp.label, Text(diff, style="warn"))
        ui.console.print(t)
        ui.detail("restore with: gitnolo rewind <#>  (your current state is saved first)", "faint")
        return 0
    cp = checkpoints.find(repo, args.checkpoint)
    if not cp:
        ui.error(f"no checkpoint {args.checkpoint}")
        return 1
    changed, added, deleted = checkpoints.diff_from_now(repo, cp)
    if not (changed or added or deleted):
        ui.ok("working tree already matches that checkpoint")
        return 0
    ui.step(f"Rewind {repo.name} to {cp.label} ({ui.ago(cp.t)} ago)")
    ui.detail(f"{len(changed)} restored · {len(added)} new files removed · {len(deleted)} deleted files brought back")
    if not args.yes and not Confirm.ask(Text("  Rewind?", style="accent"), default=False):
        return 0
    safety, n = checkpoints.restore(repo, cp)
    ui.ok(f"rewound {n} files")
    if safety:
        ui.detail(f"undo the rewind with: gitnolo rewind {safety.short}", "faint")
    return 0


def cmd_brief(args: argparse.Namespace, config: AppConfig) -> int:
    from . import brief

    repo = _repo(args.repo)
    tr = Tracker(config.monitored_agents, 48, processes=False)
    tr.poll()
    text = brief.build(repo, State(), list(tr.sessions.values()), args.days)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        ui.ok(f"brief written to {args.out}")
    elif args.copy and sys.platform == "darwin":
        import subprocess

        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
        ui.ok(f"brief copied to the clipboard ({len(text.splitlines())} lines)")
    else:
        print(text)
    return 0


def cmd_hooks(args: argparse.Namespace, config: AppConfig) -> int:
    from . import hooks

    if args.action == "install":
        added = hooks.install()
        ui.ok(("installed" if added else "already installed") + f" in {ui.short_path(hooks.SETTINGS, 60)}")
        ui.detail(f"Claude Code tool calls are approved instantly (scope: {config.rapid_hook_scope}); destructive "
                  "commands and sensitive paths still ask you. New sessions pick it up.", "faint")
        return 0
    if args.action == "uninstall":
        ui.ok("removed" if hooks.uninstall() else "was not installed")
        return 0
    on = hooks.installed()
    ui.step("Rapid response")
    ui.detail(f"claude code hook   {'installed' if on else 'not installed (gitnolo hooks install)'}", "ok" if on else "muted")
    ui.detail(f"scope              {config.rapid_hook_scope}  (gitnolo config set rapid_hook_scope edits|all)")
    ui.detail(f"supervise          questions answered with option 1 after {int(config.rapid_choice_delay)}s, "
              f"once you are away {int(config.rapid_away_seconds)}s" if config.rapid_response else "supervise          off")
    return 0


def cmd_conflict(args: argparse.Namespace, config: AppConfig) -> int:
    from .ai import make_client
    from .conflict_resolver import ConflictResolver

    repo = _repo(args.repo)
    from . import tui

    if tui.interactive():
        return 0 if tui.merge_editor(repo, make_client(config)) else 1
    return 0 if ConflictResolver(repo, make_client(config)).resolve_interactive() else 1


def cmd_config(args: argparse.Namespace, config: AppConfig) -> int:
    if args.action == "set":
        if not args.key or args.value is None:
            ui.error("usage: gitnolo config set <key> <value>")
            return 2
        try:
            val = config.set_value(args.key, args.value)
        except (KeyError, ValueError) as e:
            ui.error(f"invalid setting: {e}")
            return 2
        config.save()
        shown = "********" if "token" in args.key or "key" in args.key else val
        ui.ok(f"{args.key} = {shown}")
        return 0
    if args.action == "get":
        print(config.as_dict().get(args.key))
        return 0
    ui.step("Configuration")
    t = ui.table("key", "value*")
    for k, v in config.as_dict().items():
        if k in ("preferred_models", "debounce_seconds"):
            continue
        t.add_row(Text(k, style="muted"), Text(", ".join(v) if isinstance(v, list) else str(v)))
    ui.console.print(t)
    ui.detail("change with: gitnolo config set <key> <value>", "faint")
    return 0


def cmd_ai(args: argparse.Namespace, config: AppConfig) -> int:
    from .ai import RateGuard, make_client

    t0 = time.time()
    client = make_client(config)
    if not client:
        ui.warn("AI off or unavailable: heuristics only (instant, offline)")
        return 0
    kind = type(client).__name__.replace("Client", "")
    ui.step(f"AI provider: {kind}  ·  model {client.model_name}  ({time.time() - t0:.1f}s to resolve)")
    if hasattr(client, "guard"):
        s = RateGuard(config.ai_per_minute, config.ai_per_day).status()
        ui.detail(f"usage today {s['today']}/{s['per_day']}  ·  this minute {s['minute']}/{s['per_minute']}")
    if args.test:
        t0 = time.time()
        try:
            out = client.commit_subject("api/client.ts", "+ async function fetchUser(id) {\n+   return retry(() => get(`/users/${id}`), 3)\n+ }")
            ui.detail(f"{out or '(unusable output)'}  ·  {time.time() - t0:.1f}s", "ok" if out else "warn")
        except Exception as e:
            ui.detail(str(e), "err")
    return 0


def cmd_doctor(args: argparse.Namespace, config: AppConfig) -> int:
    from .ai import make_client
    from .github import GitHub, resolve_token
    from .gitcore import GIT

    ui.step("Diagnostics")
    ui.detail(f"git        {GIT}", "")
    token = resolve_token(config.github_token)
    login = GitHub("x/y", token).viewer() if token else None
    ui.detail(f"github     {'token ok, signed in as ' + login if login else ('token found but rejected' if token else 'no token (set GITHUB_TOKEN or gitnolo config set github_token ...)')}",
              "ok" if login else "warn")
    from .gitlab import GitLab

    try:
        gl = GitLab.for_remote(Repo(".").remote_url(), config.gitlab_token)
    except GitError:
        gl = None
    if gl:
        who = gl.viewer() if gl.token else None
        ui.detail(f"gitlab     {gl.host}: " + ("signed in as " + who if who else "no working token (set GITLAB_TOKEN)"),
                  "ok" if who else "warn")
    client = make_client(config)
    ui.detail(f"ai         {type(client).__name__.replace('Client', '') + ' · ' + str(client.model_name) if client else 'off (heuristics only)'}",
              "ok" if client else "muted")
    tr = Tracker(config.monitored_agents, config.transcript_horizon_hours)
    t0 = time.time()
    tr.poll()
    ui.detail(f"agents     {len(tr.visible())} active sessions (scan {time.time() - t0:.2f}s)", "")
    try:
        r = Repo(".")
        ui.detail(f"repo       {r.name} on {r.branch_display()} · remote {r.remote_url() or 'none'}", "")
    except GitError:
        pass
    return 0


def cmd_lens(args: argparse.Namespace, config: AppConfig) -> int:
    repo = _repo(getattr(args, "repo", None))
    c = args.command
    try:
        if c == "blame":
            path, start, end = _parse_range(args.target)
            lens.blame(repo, path, start, end)
        elif c == "history":
            path, start, end = _parse_range(args.target)
            if start:
                lens.line_history(repo, path, start, end or start)
            else:
                lens.file_history(repo, path, args.limit)
        elif c == "graph":
            from . import tui

            if tui.interactive() and not args.current:
                return tui.graph(repo, max(args.limit, 300))
            lens.graph(repo, args.limit, not args.current)
        elif c == "compare":
            lens.compare(repo, args.a, args.b or "HEAD")
        elif c == "branches":
            lens.branches(repo)
        elif c == "contributors":
            lens.contributors(repo, args.since)
        elif c == "hotspots":
            lens.hotspots(repo, args.since or "90 days ago")
        elif c == "search":
            lens.search(repo, args.query, args.mode, args.author)
        elif c == "show":
            lens.show(repo, args.rev)
        elif c == "timeline":
            lens.timeline(repo, args.days)
        elif c == "insights":
            lens.insights(repo)
        elif c == "stash":
            if args.action == "list":
                lens.stashes(repo)
            else:
                extra = ["-m", args.arg] if args.action == "push" and args.arg else ([args.arg] if args.arg else [])
                out = repo.run("stash", args.action, *extra)
                ui.ok(out.strip() or f"stash {args.action} done")
        elif c == "worktree":
            if args.action == "list":
                lens.worktrees(repo)
            elif args.action == "add":
                repo.run("worktree", "add", *([args.path] + ([args.branch] if args.branch else [])))
                ui.ok(f"worktree added at {args.path}")
            elif args.action == "remove":
                repo.run("worktree", "remove", args.path)
                ui.ok(f"worktree removed: {args.path}")
    except GitError as e:
        ui.error(str(e))
        return 1
    return 0


def _parse_range(target: str):
    if ":" in target:
        path, _, rng = target.rpartition(":")
        if rng.replace("-", "").replace(",", "").isdigit():
            parts = rng.replace(",", "-").split("-")
            return path, int(parts[0]), int(parts[1]) if len(parts) > 1 and parts[1] else None
    return target, None, None


# ------------------------------------------------------------------ home
PALETTE = [
    ("w", "watch", "Live dashboard; auto-commit, PR and merge when agents finish"),
    ("c", "commit", "Micro-commit this repo now (with agent context)"),
    ("o", "ops", "Stage, commit, push, pull, branch, merge, tag: no commands"),
    ("e", "rebase", "Interactive rebase: reorder, squash, reword, drop"),
    ("x", "explain", "AI explains the latest commit (or `gitnolo explain FILE`)"),
    ("a", "agents", "Which agent is doing what, where"),
    ("i", "insights", "Repository overview: activity, hotspots, contributors"),
    ("g", "graph", "Commit graph"),
    ("b", "branches", "Branches with upstream sync state"),
    ("m", "map", "Live map: every agent as a car on its own road"),
    ("u", "rewind", "Undo an agent's work: restore a checkpoint"),
    ("f", "brief", "Handoff brief for your next agent session"),
    ("r", "conflict", "Resolve merge conflicts"),
    ("d", "doctor", "Check GitHub/GitLab, AI and agent detection"),
    ("q", "quit", ""),
]


def _n(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _home_lines(config: AppConfig, rows: List[Session], repo: Optional[Repo], state: State) -> List[Text]:
    """What matters right now, in priority order, instead of static settings."""
    pairs: List[Any] = []
    if repo:
        vis = None
        slug = None
        try:
            from .github import parse_slug

            slug = parse_slug(repo.remote_url())
            vis = state.visibility(slug, ttl=30 * 86400) if slug else None
        except GitError:
            pass
        where = f"{repo.name} on {repo.branch_display()}"
        if vis is not None:
            where += " · private" if vis else " · public"
        elif not slug:
            from .gitlab import parse_remote

            where += " · GitLab" if parse_remote(repo.remote_url()) else " · no GitHub/GitLab remote"
        pairs.append(("repo", where, "path"))
    else:
        pairs.append(("repo", "not in a repository; watching every repo on this machine", "muted"))

    waiting = [s for s in rows if s.state == "waiting"]
    busy = [s for s in rows if s.state in ("working", "running")]
    stopped = [s for s in rows if s.state in ("error", "limited", "closed")]
    if waiting or busy or stopped:
        parts = [f"{s.agent} waiting for you in {s.repo_name}" for s in waiting[:2]]
        parts += [f"{s.agent} working in {s.repo_name}" for s in busy[:3 - len(parts)]]
        parts += [f"{s.agent} stopped in {s.repo_name} ({s.stop_reason})" for s in stopped[:max(0, 3 - len(parts))]]
        pairs.append(("agents", " · ".join(parts), "warn" if waiting or stopped else "accent"))
    elif rows:
        last = max(rows, key=lambda s: s.updated)
        pairs.append(("agents", f"none running · {last.agent} {last.state} in {last.repo_name} {ui.ago(last.updated)} ago", "muted"))
    else:
        pairs.append(("agents", "none seen in the last 3 hours", "muted"))

    dirty = 0
    if repo:
        try:
            stat = repo.numstat()
            dirty = len(repo.changes())  # numstat misses untracked files
            if dirty:
                add = sum(a for a, _ in stat.values())
                rem = sum(r for _, r in stat.values())
                pairs.append(("changes", f"{dirty} files uncommitted  +{add} -{rem}", "warn"))
            else:
                ahead = repo.run("rev-list", "--left-right", "--count", "@{u}...HEAD", check=False).split()
                sync = ""
                if len(ahead) == 2:
                    behind, fwd = int(ahead[0]), int(ahead[1])
                    sync = " · in sync with upstream" if not (behind or fwd) else f" · {fwd} ahead, {behind} behind upstream"
                pairs.append(("changes", "clean" + sync, "ok"))
        except (GitError, ValueError):
            pass

    open_issues = state.open_ledger(repo.root if repo else None)
    if open_issues:
        first = open_issues[-1]
        ref = f"#{first['number']} " if first.get("number") else ""
        more = f" (+{len(open_issues) - 1} more)" if len(open_issues) > 1 else ""
        pairs.append(("issues", f"{len(open_issues)} open from agents · {ref}{first['title'][:48]}{more}", "warn"))
    else:
        fixed = sum(1 for e in state.ledger(repo.root if repo else None)
                    if e.get("status") in ("closed", "resolved") and time.time() - e.get("resolved_at", 0) < 7 * 86400)
        pairs.append(("issues", "none open" + (f" · {fixed} fixed by agents this week" if fixed else ""), "ok" if fixed else "muted"))

    today = state.totals_today()
    if today["runs"]:
        pairs.append(("today", f"{_n(today['runs'], 'run')} · {_n(today['commits'], 'commit')} · "
                               f"{_n(today['merged'], 'PR')} merged", ""))

    safety = []
    if repo and config.checkpoints:
        from . import checkpoints

        try:
            cp = checkpoints.latest(repo)
            safety.append(f"checkpoint {ui.ago(cp.t)} ago" if cp else "no checkpoints yet")
        except GitError:
            pass
    if config.rapid_response:
        from . import hooks

        safety.append("rapid response " + ("hook on" if hooks.installed() else "via supervise"))
    if safety:
        pairs.append(("safety", " · ".join(safety), "muted"))

    if waiting:
        hint = f"{waiting[0].agent} needs you in {waiting[0].repo_name}; or run agents under `gitnolo supervise` to answer for you"
    elif dirty and not any(s.repo == (repo.root if repo else None) for s in busy):
        hint = "c ships these changes now: micro-commits, PR, merge"
    elif busy:
        hint = "w to watch; each finished turn is shipped as it ends"
    elif open_issues:
        hint = "f writes a handoff brief with the open problems for your next agent"
    else:
        hint = "w to start watching; gitnolo ships every agent turn the moment it ends"
    pairs.append(("next", hint, "accent"))
    return ui.kv_lines(pairs)


def home(config: AppConfig) -> int:
    tr = Tracker(config.monitored_agents, config.transcript_horizon_hours)
    tr.poll()
    rows = tr.visible()
    try:
        repo: Optional[Repo] = Repo(".")
    except GitError:
        repo = None
    lines = _home_lines(config, rows, repo, State())
    if edition.is_community():
        lines += ui.kv_lines([("edition", "community · public repos only", "muted")])
    ui.header(__version__, "Autonomous git for coding agents", lines)
    if rows:
        ui.console.print(agents_table(tr))
    from . import tui

    last = 0
    while True:
        ui.console.print()
        if tui.interactive():
            try:
                pick = tui.menu("gitnolo", [(n, d) for _, n, d in PALETTE], last, [k for k, _, _ in PALETTE])
            except KeyboardInterrupt:
                return 0
            if pick is None or PALETTE[pick][1] == "quit":
                return 0
            last, name = pick, PALETTE[pick][1]
            try:
                main([name] if name != "watch" else ["watch", "-y"], config=config)
            except KeyboardInterrupt:
                pass
            continue
        for key, name, desc in PALETTE:
            ui.console.print(Text.assemble(("  " + key + "  ", "accent.bold"), (f"{name:<10}", "bold"), (desc, "muted")))
        try:
            choice = Prompt.ask(Text("\n  >", style="accent"), choices=[k for k, _, _ in PALETTE], default="w", show_choices=False)
        except (KeyboardInterrupt, EOFError):
            return 0
        if choice == "q":
            return 0
        name = next(n for k, n, _ in PALETTE if k == choice)
        try:
            main([name] if name != "watch" else ["watch", "-y"], config=config)
        except KeyboardInterrupt:
            pass


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gitnolo" if edition.is_community() else "gitnolo-x", description="Autonomous git for coding agents")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__} ({edition.EDITION}, "
                   f"python {sys.version_info[0]}.{sys.version_info[1]})")
    sub = p.add_subparsers(dest="command")

    c = sub.add_parser("commit", help="micro-commit, push, PR and merge the current changes")
    c.add_argument("--repo")
    c.add_argument("-y", "--yes", action="store_true", help="no confirmation")
    c.add_argument("-n", "--target", type=int, help="number of commits (overrides policy)")
    c.add_argument("--single", action="store_true", help="one commit")
    c.add_argument("--title", help="PR/topic title")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--no-push", action="store_true")
    c.add_argument("--no-pr", action="store_true")
    c.add_argument("--no-merge", action="store_true")
    c.add_argument("--no-issues", action="store_true")
    c.add_argument("--no-ai", action="store_true")
    c.add_argument("--no-agent", action="store_true", help="ignore agent transcripts")

    w = sub.add_parser("watch", help="live dashboard; act when agents finish")
    w.add_argument("-y", "--yes", action="store_true", help="fully autonomous (no confirmations)")
    w.add_argument("--dry-run", action="store_true")
    w.add_argument("--catch-up", action="store_true", help="also handle turns that finished before start")
    w.add_argument("--plain", action="store_true", help="line log instead of dashboard (for daemons)")

    a = sub.add_parser("agents", help="agent sessions and their state")
    a.add_argument("--all", action="store_true")
    a.add_argument("--live", action="store_true")

    i = sub.add_parser("issues", help="open GitHub issues from the last agent report")
    i.add_argument("--repo")
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("--no-ai", action="store_true")
    i.add_argument("-y", "--yes", action="store_true")
    i.add_argument("--list", action="store_true", help="issues gitnolo is tracking (snapped, open, closed)")
    i.add_argument("--all", action="store_true", help="with --list: include closed and resolved")

    pr = sub.add_parser("pr", help="list or merge pull requests")
    pr.add_argument("action", nargs="?", default="list", choices=["list", "merge"])
    pr.add_argument("number", nargs="?")
    pr.add_argument("--method", choices=["merge", "squash", "rebase"])
    pr.add_argument("--repo")

    s = sub.add_parser("supervise", help="run an agent in a PTY, auto-approving safe prompts")
    s.add_argument("-y", "--yes", action="store_true")
    s.add_argument("--no-rapid", action="store_true", help="never answer questions for you")
    s.add_argument("agent_cmd", nargs=argparse.REMAINDER)

    mp = sub.add_parser("map", help="live map: agents as cars on the road")
    mp.add_argument("-n", "--limit", type=int, default=10)

    rw = sub.add_parser("rewind", help="list checkpoints, or restore one (undo an agent's work)")
    rw.add_argument("checkpoint", nargs="?", help="# from the list, or a checkpoint name")
    rw.add_argument("--save", nargs="?", const="", metavar="LABEL", help="take a checkpoint now")
    rw.add_argument("-n", "--limit", type=int, default=15)
    rw.add_argument("-y", "--yes", action="store_true")
    rw.add_argument("--repo")

    bf = sub.add_parser("brief", help="handoff brief for the next agent session")
    bf.add_argument("--days", type=int, default=7)
    bf.add_argument("-o", "--out", help="write to a file")
    bf.add_argument("-c", "--copy", action="store_true", help="copy to the clipboard")
    bf.add_argument("--repo")

    hk = sub.add_parser("hooks", help="rapid response hook for Claude Code")
    hk.add_argument("action", nargs="?", default="status", choices=["status", "install", "uninstall"])

    cf = sub.add_parser("conflict", help="resolve merge conflicts")
    cf.add_argument("--repo")

    cg = sub.add_parser("config", help="show or change settings")
    cg.add_argument("action", nargs="?", default="show", choices=["show", "get", "set"])
    cg.add_argument("key", nargs="?")
    cg.add_argument("value", nargs="?")

    ai = sub.add_parser("ai", help="AI provider status")
    ai.add_argument("--test", action="store_true", help="time one real generation")

    sub.add_parser("doctor", help="diagnostics")
    sub.add_parser("status", help="alias of doctor")

    ex = sub.add_parser("explain", help="AI explains a commit, a file's history, or FILE:START-END")
    ex.add_argument("target", nargs="?", default="HEAD")
    sub.add_parser("ops", help="arrow-key git operations: stage, commit, push, pull, branch, merge, tag")
    rb = sub.add_parser("rebase", help="interactive rebase: reorder, squash, reword, drop")
    rb.add_argument("base", nargs="?", help="rebase commits after this ref (default: upstream or HEAD~10)")
    # GitLens
    b = sub.add_parser("blame", help="blame heatmap: FILE or FILE:START-END")
    b.add_argument("target")
    h = sub.add_parser("history", help="file history, or line history with FILE:START-END")
    h.add_argument("target")
    h.add_argument("-n", "--limit", type=int, default=30)
    g = sub.add_parser("graph", help="commit graph")
    g.add_argument("-n", "--limit", type=int, default=40)
    g.add_argument("--current", action="store_true", help="current branch only")
    cp = sub.add_parser("compare", help="compare two refs")
    cp.add_argument("a")
    cp.add_argument("b", nargs="?")
    sub.add_parser("branches", help="branches with sync state")
    ct = sub.add_parser("contributors", help="authors by commits and lines")
    ct.add_argument("--since")
    hs = sub.add_parser("hotspots", help="most frequently changed files")
    hs.add_argument("--since")
    se = sub.add_parser("search", help="search commits")
    se.add_argument("query")
    se.add_argument("--mode", choices=["message", "code", "pickaxe", "file"], default="message")
    se.add_argument("--author")
    sh = sub.add_parser("show", help="commit details")
    sh.add_argument("rev", nargs="?", default="HEAD")
    tl = sub.add_parser("timeline", help="commit activity sparkline")
    tl.add_argument("--days", type=int, default=30)
    sub.add_parser("insights", help="repository overview")
    st = sub.add_parser("stash", help="list/push/pop/apply/drop stashes")
    st.add_argument("action", nargs="?", default="list", choices=["list", "push", "pop", "apply", "drop", "show"])
    st.add_argument("arg", nargs="?")
    wt = sub.add_parser("worktree", help="list/add/remove worktrees")
    wt.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove"])
    wt.add_argument("path", nargs="?")
    wt.add_argument("branch", nargs="?")
    for sp in (b, h, g, cp, ct, hs, se, sh, tl, st, wt):
        sp.add_argument("--repo")
    return p


LENS_COMMANDS = {"blame", "history", "graph", "compare", "branches", "contributors", "hotspots", "search", "show",
                 "timeline", "insights", "stash", "worktree"}


def main(argv: Optional[List[str]] = None, config: Optional[AppConfig] = None) -> int:
    args = build_parser().parse_args(argv)
    config = config or AppConfig.load()
    cmd = args.command
    try:
        if cmd is None:
            return home(config)
        if cmd == "commit":
            return cmd_commit(args, config)
        if cmd == "watch":
            return cmd_watch(args, config)
        if cmd == "agents":
            return cmd_agents(args, config)
        if cmd == "issues":
            return cmd_issues(args, config)
        if cmd == "pr":
            return cmd_pr(args, config)
        if cmd == "supervise":
            return cmd_supervise(args, config)
        if cmd == "conflict":
            return cmd_conflict(args, config)
        if cmd == "map":
            return cmd_map(args, config)
        if cmd == "rewind":
            return cmd_rewind(args, config)
        if cmd == "brief":
            return cmd_brief(args, config)
        if cmd == "hooks":
            return cmd_hooks(args, config)
        if cmd == "config":
            return cmd_config(args, config)
        if cmd == "ai":
            return cmd_ai(args, config)
        if cmd in ("doctor", "status"):
            return cmd_doctor(args, config)
        if cmd == "explain":
            from . import tui
            from .ai import make_client

            return tui.explain(_repo(None), args.target, make_client(config))
        if cmd in ("ops", "rebase"):
            from . import tui

            repo = _repo(None)
            return tui.ops(repo) if cmd == "ops" else tui.rebase(repo, args.base)
        if cmd in LENS_COMMANDS:
            return cmd_lens(args, config)
    except KeyboardInterrupt:
        ui.console.print()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
