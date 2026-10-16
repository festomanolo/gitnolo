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
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

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


def _limit_handler(config: AppConfig, agent_cmd: List[str]) -> Optional[Callable[[str], Optional[List[str]]]]:
    """on_limit callback for supervise: switch Chrome profile, then restart the agent with a handoff prompt."""
    from . import failover

    fam = failover.family(agent_cmd[0])
    profiles = config.agent_profiles.get(fam) or []
    if len(profiles) < 2:
        return None

    def handle(screen: str) -> Optional[List[str]]:
        session = None
        try:
            session = latest_session(os.getcwd(), config)
            if session and failover.family(session.agent) != fam:
                session = None
        except Exception:
            pass
        ui.notify("gitnolo", f"{fam} hit its usage limit; switching account", urgent=True)
        res = failover.failover(fam, profiles, session, os.getcwd(), re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", screen),
                                until=session.resume_at if session else None)
        if not res:
            ui.notify("gitnolo", f"{fam}: every profile is at its limit", urgent=True)
            return None
        profile, prompt = res
        os.environ["BROWSER"] = failover.browser_wrapper(profile)
        ui.notify("gitnolo", f"{fam} switched to Chrome profile '{profile}'; continuing where it stopped", urgent=True)
        return failover.resume_cmd(fam, prompt) if config.failover_auto_resume else None

    return handle


def cmd_failover(args: argparse.Namespace, config: AppConfig) -> int:
    from . import failover

    fam = failover.family(args.agent) if args.agent else None
    if args.profiles:
        config.agent_profiles[fam] = args.profiles
        config.save()
        ui.ok(f"{fam}: {len(args.profiles)} profiles ({', '.join(args.profiles)})")
    if args.switch:
        profiles = config.agent_profiles.get(fam) or []
        res = failover.failover(fam, profiles, latest_session(os.getcwd(), config) if _in_repo() else None, os.getcwd())
        if not res:
            ui.error(f"no profile available to switch to for {fam}")
            return 1
        ui.ok(f"{fam} now on Chrome profile '{res[0]}'; run `gitnolo resume {fam}` to continue the work")
        return 0
    if not config.agent_profiles:
        ui.detail("no profiles yet: gitnolo failover kiro 'Default' 'Profile 1'   (Chrome profile directory names)")
        return 0
    limited = failover._load()
    for agent, profs in config.agent_profiles.items():
        cur = failover.current(agent, profs)
        for p in profs:
            until = limited.get(agent, {}).get("limited", {}).get(p, 0)
            state = f"limited, back in {_fmt_secs(until - time.time())}" if until > time.time() else "ready"
            ui.detail(f"{agent:<6} {p:<14} {'● active' if p == cur else '  ':<9} {state}")
    return 0


def _in_repo() -> bool:
    try:
        Repo(".")
        return True
    except GitError:
        return False


def _fmt_secs(n: float) -> str:
    n = int(max(n, 0))
    return f"{n // 3600}h{n % 3600 // 60:02d}m" if n >= 3600 else f"{n // 60}m"


def cmd_backfill(args: argparse.Namespace, config: AppConfig) -> int:
    """git init (if needed) and commit untracked files as micro-commits dated by file mtime."""
    from . import autoinit

    path = os.path.abspath(args.path)
    if not os.path.isdir(os.path.join(path, ".git")):
        if args.dry_run:
            ui.warn(f"{path} is not a git repository; it would be initialised")
            return 0
        if not autoinit.init(path, config, lambda k, t: (ui.warn if k == "warn" else ui.ok)(t)):
            ui.error(f"cannot init {path} (not a project folder?)")
            return 1
    n = autoinit.backfill(path, args.per_commit, lambda k, t: (ui.warn if k == "warn" else ui.ok)(t), args.dry_run)
    if args.dry_run:
        ui.ok(f"would create {n} dated commits")
    elif not n:
        ui.ok("nothing untracked to backfill")
    return 0


def cmd_resume(args: argparse.Namespace, config: AppConfig) -> int:
    """Start the agent with the saved handoff prompt for this directory."""
    from . import failover
    from .auto_accept import supervise_command

    fam = failover.family(args.agent)
    prompt = failover.load_handoff(os.getcwd(), fam)
    if not prompt:
        session = latest_session(os.getcwd(), config) if _in_repo() else None
        prompt = failover.handoff_prompt(fam, session, os.getcwd())
    cmd = failover.resume_cmd(fam, prompt)
    profiles = config.agent_profiles.get(fam) or []
    if profiles:
        os.environ["BROWSER"] = failover.browser_wrapper(failover.current(fam, profiles))
    ui.step(f"Resuming {fam} with a handoff prompt")
    return supervise_command(cmd, auto_accept=True, notify=ui.notify if config.notify else None, rapid=config.rapid_response,
                             away_seconds=config.rapid_away_seconds, choice_delay=config.rapid_choice_delay,
                             on_limit=_limit_handler(config, cmd))


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
                             away_seconds=config.rapid_away_seconds, choice_delay=config.rapid_choice_delay,
                             on_limit=_limit_handler(config, args.agent_cmd))
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


def cmd_achievements(args: argparse.Namespace, config: AppConfig) -> int:
    from . import achievements

    login, rows = achievements.collect()
    if not login:
        ui.error("No GitHub token or offline. Run `gh auth login` or set GITHUB_TOKEN.")
        return 1
    ui.console.print(Text.assemble(("\n  achievements  ", "accent.bold"), (f"@{login}", "muted")))
    for name, n, hint in rows:
        label, nxt = achievements.tier_of(name, n)
        ui.console.print(Text.assemble(("  " + name.ljust(12), "bold"), (f"{n:<6}", "accent"), (label.ljust(9), "ok" if label != "locked" else "faint"),
                                       (f"next at {nxt}" if nxt else "maxed", "muted")))
        ui.console.print(Text("    " + hint, style="faint"))
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
    from . import auth

    ui.detail(f"account    {'signed in as ' + config.auth_email if config.auth_token else ('not signed in (gitnolo login)' if auth.base_url(config) else 'sign-in not configured')}",
              "ok" if config.auth_token else "warn")
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
                lens.line_history(repo, path, start, end or start, 0 if args.all else 20, args.since)
            else:
                lens.file_history(repo, path, 0 if args.all or args.since else args.limit, args.since)
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
# (key, command, description, argument prompt or None). Key " " = no shortcut.
GROUPS = [
    ("Autopilot", [
        ("w", "watch", "Live dashboard of the autopilot: commits, PRs, merges, account switches", None),
        ("a", "agents", "Which agent is doing what, where", None),
        ("m", "map", "Live map: every agent as a car on its own road", None),
        ("p", "supervise", "Run an agent with auto-approve, answers when you're away, account failover", "agent command (e.g. kiro-cli chat)"),
        ("f", "failover", "Chrome profiles to rotate through on usage limits", "agent and profiles, or empty for status (kiro Default 'Profile 1')"),
        ("y", "resume", "Continue where the last agent stopped, with a handoff prompt", "agent (kiro | agy)"),
    ]),
    ("Ship", [
        ("c", "commit", "Micro-commit this repo now (with agent context)", None),
        ("o", "ops", "Stage, commit, push, pull, branch, merge, tag: no commands", None),
        ("r", "pr", "List or merge pull requests", None),
        ("i", "issues", "Open GitHub issues from the last agent report", None),
        ("l", "release", "Build and publish a release", None),
    ]),
    ("History & review", [
        ("g", "graph", "Interactive commit graph", None),
        ("b", "branches", "Branches with upstream sync state", None),
        ("e", "rebase", "Interactive rebase: reorder, squash, reword, drop", None),
        ("x", "explain", "AI explains a commit, file, or FILE:A-B history", "rev, FILE or FILE:A-B (empty = latest commit)"),
        ("h", "history", "File or line history", "FILE or FILE:START-END"),
        ("k", "blame", "Blame heatmap", "FILE or FILE:START-END"),
        ("d", "compare", "Compare two refs", "two refs (A B)"),
        ("t", "search", "Search commits", "query"),
        ("n", "insights", "Repository overview: activity, hotspots, contributors", None),
        (" ", "show", "Commit details", "rev (empty = HEAD)"),
        (" ", "timeline", "Commit activity sparkline", None),
        (" ", "contributors", "Authors by commits and lines", None),
        (" ", "hotspots", "Most frequently changed files", None),
        (" ", "stash", "List, push, pop, apply, drop stashes", "action (list|push|pop|apply|drop|show)"),
        (" ", "worktree", "List, add, remove worktrees", "action [path] [branch] (empty = list)"),
    ]),
    ("Safety", [
        ("u", "rewind", "Undo an agent's work: restore a checkpoint", None),
        ("j", "conflict", "Resolve merge conflicts", None),
        ("v", "brief", "Handoff brief for your next agent session", None),
        (" ", "hooks", "Rapid-response hook for Claude Code", "status | install | uninstall"),
    ]),
    ("Setup & account", [
        ("s", "setup", "Connect AI (OpenRouter key or Ollama)", None),
        ("?", "doctor", "Check GitHub/GitLab, AI and agent detection", None),
        ("z", "achievements", "GitHub profile achievement progress", None),
        (" ", "ai", "AI provider status", None),
        (" ", "config", "Show or change settings", "show | get KEY | set KEY VALUE"),
        (" ", "login", "Sign in with Google", None),
        (" ", "logout", "Sign out", None),
    ]),
]
ITEMS = {name: (key, desc, ask) for _, items in GROUPS for key, name, desc, ask in items}
PALETTE = [(k, n, d) for _, items in GROUPS for k, n, d, _ in items]  # flat view for the non-TTY launch screen
NEEDS_ARG = {"supervise", "history", "blame", "compare", "search"}


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


def _autopilot_line(bg: Any) -> Text:
    if bg is None:
        return Text("  ○ autopilot off  (autopilot=true in config, or open `watch`)", style="faint")
    t = Text.assemble(("  ● autopilot on", "ok"), ("  commits, PRs, merges and account switches happen on their own", "muted"))
    for ts, kind, text in bg.recent(2):
        t.append("\n    " + time.strftime("%H:%M", time.localtime(ts)) + "  ", "faint")
        t.append(text[:ui.console.width - 14], "muted")
    return t


def _start_autopilot(config: AppConfig) -> Any:
    if not config.autopilot or not tui_interactive():
        return None
    from .watcher import Watcher

    try:
        return Watcher(config, auto=True, quiet=True).start_background()
    except Exception:
        return None


def tui_interactive() -> bool:
    from . import tui

    return tui.interactive()


def _run_command(name: str, config: AppConfig) -> None:
    """Runs one menu command, asking for its argument when it needs one."""
    from . import tui

    _, _, ask = ITEMS[name]
    argv = [name]
    if name == "watch":
        argv = ["watch", "-y"]
    if ask:
        import shlex

        ui.console.print()
        answer = tui.ask(ask)
        try:
            extra = shlex.split(answer)
        except ValueError:
            extra = answer.split()
        if name in NEEDS_ARG and not extra:
            ui.warn("nothing entered")
            return
        argv += extra
    try:
        main(argv, config=config)
    except KeyboardInterrupt:
        pass
    except SystemExit:
        pass  # argparse rejected the argument; its message is already shown
    if ask or name in ("doctor", "ai", "config", "achievements", "agents", "hooks", "brief", "failover"):
        _pause()


def _pause() -> None:
    from . import tui

    if tui.interactive():
        ui.console.print(Text("\n  press any key to return", style="faint"))
        try:
            tui.read_key()
        except KeyboardInterrupt:
            pass


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

    bg = _start_autopilot(config)
    start = 0
    names = [n for _, items in GROUPS for _, n, _, _ in items]
    try:
        while True:
            ui.console.print()
            if tui.interactive():
                ui.console.print(_autopilot_line(bg))
                try:
                    name = tui.grouped_menu("gitnolo", [(c, [(k, n, d) for k, n, d, _ in items]) for c, items in GROUPS], start)
                except KeyboardInterrupt:
                    return 0
                if name is None:
                    return 0
                start = names.index(name)
                # one pipeline at a time: the foreground `watch` replaces the background autopilot
                if bg is not None and name == "watch":
                    bg.stop()
                    bg = None
                _run_command(name, config)
                if bg is None and name == "watch":
                    bg = _start_autopilot(config)
                continue
            for cat, items in GROUPS:
                ui.console.print(Text(f"\n  {cat.upper()}", style="accent.bold"))
                for key, name, desc, _ in items:
                    ui.console.print(Text.assemble(("  " + key + "  ", "faint"), (f"{name:<13}", "bold"), (desc, "muted")))
            try:
                choice = Prompt.ask(Text("\n  command >", style="accent"), default="watch", show_choices=False)
            except (KeyboardInterrupt, EOFError):
                return 0
            choice = {k: n for k, n, _ in PALETTE if k.strip()}.get(choice, choice)
            if choice in ("q", "quit", "exit"):
                return 0
            if choice in ITEMS:
                _run_command(choice, config)
            else:
                ui.warn(f"unknown command: {choice}")
    finally:
        if bg is not None:
            bg.stop()


CREDITS = "Made by festomanolo · github.com/festomanolo · festomanolo.com"


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gitnolo" if edition.is_community() else "gitnolo-x", description="Autonomous git for coding agents",
                                epilog=CREDITS)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__} ({edition.EDITION}, "
                   f"python {sys.version_info[0]}.{sys.version_info[1]})\n{CREDITS}")
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

    fo = sub.add_parser("failover", help="rotate Chrome profiles when kiro/agy hit a usage limit")
    fo.add_argument("agent", nargs="?", help="kiro | agy")
    fo.add_argument("profiles", nargs="*", help="Chrome profile directories, in rotation order (Default, 'Profile 1', ...)")
    fo.add_argument("--switch", action="store_true", help="switch to the next profile now")

    bf = sub.add_parser("backfill", help="git init + commit old untracked files as micro-commits dated by mtime")
    bf.add_argument("path", nargs="?", default=".")
    bf.add_argument("-k", "--per-commit", type=int, default=3)
    bf.add_argument("--dry-run", action="store_true")

    rs = sub.add_parser("resume", help="continue where the last agent stopped, using a handoff prompt")
    rs.add_argument("agent", nargs="?", default="kiro")

    mp = sub.add_parser("map", help="live map: agents as cars on the road")
    mp.add_argument("-n", "--limit", type=int, default=10)

    rw = sub.add_parser("rewind", help="list checkpoints, or restore one (undo an agent's work)")
    rw.add_argument("checkpoint", nargs="?", help="# from the list, or a checkpoint name")
    rw.add_argument("--save", nargs="?", const="", metavar="LABEL", help="take a checkpoint now")
    rw.add_argument("-n", "--limit", type=int, default=15)
    rw.add_argument("-y", "--yes", action="store_true")
    rw.add_argument("--repo")

    sub.add_parser("achievements", help="GitHub profile achievement progress")

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

    sub.add_parser("login", help="sign in with Google")
    sub.add_parser("logout", help="sign out")
    sub.add_parser("setup", help="connect AI (OpenRouter key from clipboard, or Ollama)")
    rl = sub.add_parser("release", help="build and publish a GitHub release")
    rl.add_argument("tag", nargs="?", help="tag, default: next patch")
    rl.add_argument("--no-build", action="store_true")
    rl.add_argument("-y", "--yes", action="store_true", help="skip the confirmations")
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
    h.add_argument("-a", "--all", action="store_true", help="entire history, renames followed (years back)")
    h.add_argument("--since", help='e.g. "5 years ago"')
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
    ui.set_sounds(config.notify_sound, config.notify_alert_sound)
    cmd = args.command
    try:
        if cmd not in ("login", "logout"):
            from . import auth, usage
            usage.bump("cmd:" + (cmd or "home"))
            auth.heartbeat(config)
        if cmd is None:
            if not config.onboarded and sys.stdin.isatty():
                from . import onboard
                onboard.run(config)
            return home(config)
        if cmd in ("login", "logout"):
            from . import auth, onboard
            if cmd == "logout":
                auth.logout(config)
                ui.ok("signed out")
                return 0
            r = onboard.Rail("gitnolo login")
            r.end("done") if auth.step(r, config, True) else r.end("not signed in")
            return 0
        if cmd == "setup":
            from . import onboard
            return onboard.run(config, force=True)
        if cmd == "release":
            from . import release
            return release.run(config, args.tag, args.no_build, args.yes)
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
        if cmd == "failover":
            return cmd_failover(args, config)
        if cmd == "resume":
            return cmd_resume(args, config)
        if cmd == "backfill":
            return cmd_backfill(args, config)
        if cmd == "conflict":
            return cmd_conflict(args, config)
        if cmd == "map":
            return cmd_map(args, config)
        if cmd == "rewind":
            return cmd_rewind(args, config)
        if cmd == "achievements":
            return cmd_achievements(args, config)
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
