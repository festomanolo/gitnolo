"""
GitLens-style repository intelligence for the terminal.

blame (heatmap)  history (file)  line history  graph  compare  branches
stashes  worktrees  contributors  hotspots  search  show  timeline  insights
"""

from __future__ import annotations

import os
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from rich.syntax import Syntax
from rich.text import Text

from . import ui
from .gitcore import GitError, Repo

SEP = "\x1f"
REC = "\x1e"


def _commits(repo: Repo, *args: str, limit: int = 50) -> List[Dict[str, str]]:
    fmt = SEP.join(["%h", "%H", "%an", "%ae", "%at", "%s", "%D"]) + REC
    out = repo.run("log", f"--format={fmt}", f"-n{limit}", *args, check=False)
    rows = []
    for rec in out.split(REC):
        rec = rec.strip("\n")
        if not rec:
            continue
        p = rec.split(SEP)
        if len(p) >= 7:
            rows.append({"short": p[0], "sha": p[1], "author": p[2], "email": p[3], "time": p[4], "subject": p[5], "refs": p[6]})
    return rows


def _commit_table(rows: List[Dict[str, str]], title: Optional[str] = None):
    t = ui.table("commit", "when", "author", "subject*", title=title)
    for r in rows:
        subj = Text(r["subject"])
        if r.get("refs"):
            subj = Text.assemble((f"({r['refs']}) ", "branch"), r["subject"])
        t.add_row(Text(r["short"], style="hash"), Text(ui.ago(float(r["time"])), style="muted"), Text(r["author"][:18], style="info"), subj)
    return t


# ------------------------------------------------------------------ blame
def blame(repo: Repo, path: str, start: Optional[int] = None, end: Optional[int] = None) -> None:
    args = ["blame", "--line-porcelain"]
    if start:
        args += ["-L", f"{start},{end or start + 40}"]
    args += ["--", path]
    out = repo.run(*args)
    lines: List[Tuple[str, str, float, str, int]] = []
    meta: Dict[str, Dict[str, str]] = {}
    cur = None
    lineno = 0
    for raw in out.split("\n"):
        if not raw:
            continue
        if raw.startswith("\t"):
            m = meta[cur]
            lines.append((cur[:7], m.get("author", "?"), float(m.get("author-time", 0)), raw[1:], lineno))
            continue
        parts = raw.split(" ")
        if len(parts[0]) in (40, 64) and all(c in "0123456789abcdef" for c in parts[0]):
            cur = parts[0]
            lineno = int(parts[2])
            meta.setdefault(cur, {})
        elif cur:
            k, _, v = raw.partition(" ")
            meta[cur][k] = v
    if not lines:
        ui.warn("No blame data")
        return
    now = time.time()
    ages = [now - l[2] for l in lines]
    newest, oldest = min(ages), max(ages)
    ui.step(f"Blame {path}")
    t = ui.table("", "line", "commit", "author", "age", "code*")
    prev = None
    for (sha, author, ts, code, n), age in zip(lines, ages):
        color = ui.heat(age, newest, oldest)
        same = sha == prev
        prev = sha
        t.add_row(
            Text("▌", style=color),
            Text(str(n), style="faint"),
            Text("" if same else sha, style="hash"),
            Text("" if same else author[:16], style="info"),
            Text("" if same else ui.ago(ts), style="muted"),
            Text(code.expandtabs(4)[:120]),
        )
    ui.console.print(t)
    authors = Counter(l[1] for l in lines)
    ui.detail("  ".join(f"{a} {c * 100 // len(lines)}%" for a, c in authors.most_common(4)))


# ------------------------------------------------------------------ history
def file_history(repo: Repo, path: str, limit: int = 30) -> None:
    rows = _commits(repo, "--follow", "--", path, limit=limit)
    ui.step(f"History of {path}  ({len(rows)} commits)")
    ui.console.print(_commit_table(rows))


def line_history(repo: Repo, path: str, start: int, end: int, limit: int = 20) -> None:
    out = repo.run("log", f"-n{limit}", "--format=%x1e%h%x1f%an%x1f%at%x1f%s", "-L", f"{start},{end}:{path}", check=False)
    ui.step(f"Line history {path}:{start}-{end}")
    for block in out.split("\x1e"):
        if not block.strip():
            continue
        head, _, diff = block.partition("\n")
        p = head.split("\x1f")
        if len(p) < 4:
            continue
        ui.console.print(Text.assemble((p[0] + " ", "hash"), (ui.ago(float(p[2])) + " ", "muted"), (p[1] + "  ", "info"), p[3]))
        body = "\n".join(l for l in diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---")))
        if body:
            ui.console.print(Syntax(body[:3000], "diff", theme="ansi_dark", background_color="default"))


def graph(repo: Repo, limit: int = 40, all_refs: bool = True) -> None:
    args = ["log", "--graph", "--color=always", f"-n{limit}",
            "--format=%C(#c792ea)%h%C(reset) %C(#6cb6d9)%d%C(reset) %s %C(#5c5c5c)%an, %ar%C(reset)"]
    if all_refs:
        args.append("--all")
    ui.step("Commit graph")
    ui.console.print(Text.from_ansi(repo.run(*args, check=False)))


def compare(repo: Repo, a: str, b: str) -> None:
    counts = repo.run("rev-list", "--left-right", "--count", f"{a}...{b}").split()
    behind, ahead = int(counts[0]), int(counts[1])
    ui.step(f"Compare {a}...{b}")
    ui.detail(f"{b} is {ahead} ahead and {behind} behind {a}")
    if ahead:
        ui.console.print(_commit_table(_commits(repo, f"{a}..{b}", limit=30), title=f"Only in {b}"))
    if behind:
        ui.console.print(_commit_table(_commits(repo, f"{b}..{a}", limit=30), title=f"Only in {a}"))
    _numstat_table(repo.run("diff", "--numstat", f"{a}...{b}", check=False), "Files changed")


def _numstat_table(out: str, title: str) -> None:
    rows = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) == 3:
            rows.append((int(p[0]) if p[0].isdigit() else 0, int(p[1]) if p[1].isdigit() else 0, p[2]))
    if not rows:
        return
    peak = max(a + d for a, d, _ in rows) or 1
    t = ui.table("file*", "+", "-", "", title=title)
    for a, d, path in sorted(rows, key=lambda r: -(r[0] + r[1]))[:40]:
        bar = ui.bar(a, peak, 16, "add")
        bar.append_text(ui.bar(d, peak, 16, "del"))
        t.add_row(Text(path, style="path"), Text(str(a), style="add"), Text(str(d), style="del"), bar)
    ui.console.print(t)


def branches(repo: Repo) -> None:
    fmt = SEP.join(["%(refname:short)", "%(objectname:short)", "%(committerdate:unix)", "%(authorname)",
                    "%(upstream:short)", "%(upstream:track)", "%(HEAD)", "%(subject)"])
    out = repo.run("for-each-ref", "--sort=-committerdate", f"--format={fmt}", "refs/heads", check=False)
    t = ui.table("", "branch", "tip", "updated", "upstream", "sync", "subject*", title=None)
    for line in out.splitlines():
        p = line.split(SEP)
        if len(p) < 8:
            continue
        name, tip, ts, author, up, track, head, subj = p
        t.add_row(
            Text("●" if head == "*" else " ", style="accent"),
            Text(name, style="branch" if head != "*" else "accent.bold"),
            Text(tip, style="hash"),
            Text(ui.ago(float(ts)) if ts else "-", style="muted"),
            Text(up or "-", style="faint"),
            Text(track.strip("[]") or ("in sync" if up else "local"), style="warn" if "behind" in track else "muted"),
            Text(subj[:60]),
        )
    ui.step("Branches")
    ui.console.print(t)


def stashes(repo: Repo) -> None:
    out = repo.run("stash", "list", "--format=%gd%x1f%ct%x1f%gs", check=False)
    ui.step("Stashes")
    if not out.strip():
        ui.detail("No stashes")
        return
    t = ui.table("ref", "age", "message*", "files")
    for line in out.splitlines():
        ref, ts, msg = (line.split("\x1f") + ["", ""])[:3]
        files = repo.run("stash", "show", "--name-only", ref, check=False).split()
        t.add_row(Text(ref, style="hash"), Text(ui.ago(float(ts)), style="muted"), msg, Text(str(len(files)), style="info"))
    ui.console.print(t)


def worktrees(repo: Repo) -> None:
    out = repo.run("worktree", "list", "--porcelain", check=False)
    ui.step("Worktrees")
    t = ui.table("path*", "branch", "head")
    cur: Dict[str, str] = {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                t.add_row(Text(ui.short_path(cur.get("worktree"), 60), style="path"),
                          Text(cur.get("branch", "detached").replace("refs/heads/", ""), style="branch"),
                          Text(cur.get("HEAD", "")[:8], style="hash"))
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v or "yes"
    ui.console.print(t)


def contributors(repo: Repo, since: Optional[str] = None) -> None:
    args = ["log", "--format=\x1e%aN", "--numstat", "--no-merges"]
    if since:
        args.append(f"--since={since}")
    out = repo.run(*args, check=False)
    stats: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0])
    for block in out.split("\x1e"):
        if not block.strip():
            continue
        name, _, rest = block.partition("\n")
        s = stats[name.strip()]
        s[0] += 1
        for line in rest.splitlines():
            p = line.split("\t")
            if len(p) == 3:
                s[1] += int(p[0]) if p[0].isdigit() else 0
                s[2] += int(p[1]) if p[1].isdigit() else 0
    if not stats:
        ui.detail("No commits")
        return
    peak = max(v[0] for v in stats.values())
    t = ui.table("author", "commits", "", "+", "-")
    for name, (c, a, d) in sorted(stats.items(), key=lambda kv: -kv[1][0])[:20]:
        t.add_row(Text(name, style="info"), str(c), ui.bar(c, peak, 20), Text(str(a), style="add"), Text(str(d), style="del"))
    ui.step("Contributors" + (f" since {since}" if since else ""))
    ui.console.print(t)


def hotspots(repo: Repo, since: str = "90 days ago", limit: int = 15) -> None:
    out = repo.run("log", f"--since={since}", "--format=", "--numstat", "--no-merges", check=False)
    changes: Counter = Counter()
    churn: Counter = Counter()
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) == 3:
            changes[p[2]] += 1
            churn[p[2]] += (int(p[0]) if p[0].isdigit() else 0) + (int(p[1]) if p[1].isdigit() else 0)
    if not changes:
        ui.detail("No changes in range")
        return
    peak = changes.most_common(1)[0][1]
    t = ui.table("file", "changes", "", "churn")
    for path, n in changes.most_common(limit):
        if not os.path.exists(os.path.join(repo.root, path)):
            continue
        t.add_row(Text(path, style="path"), str(n), ui.bar(n, peak, 20), Text(str(churn[path]), style="muted"))
    ui.step(f"Hotspots since {since}")
    ui.console.print(t)


def search(repo: Repo, query: str, mode: str = "message", author: Optional[str] = None, limit: int = 40) -> None:
    args: List[str] = []
    if mode == "message":
        args += [f"--grep={query}", "-i"]
    elif mode == "code":
        args += [f"-G{query}"]
    elif mode == "pickaxe":
        args += [f"-S{query}"]
    elif mode == "file":
        args += ["--", query]
    if author:
        args.insert(0, f"--author={author}")
    rows = _commits(repo, "--all", *args, limit=limit)
    ui.step(f"Search {mode}: {query}  ({len(rows)} results)")
    ui.console.print(_commit_table(rows))


def show(repo: Repo, rev: str) -> None:
    fmt = SEP.join(["%H", "%an", "%ae", "%at", "%P", "%D", "%B"])
    out = repo.run("show", "-s", f"--format={fmt}", rev)
    p = out.split(SEP)
    ui.step(f"Commit {p[0][:12]}")
    ui.console.print(ui.kv_lines([
        ("author", f"{p[1]} <{p[2]}>", "info"),
        ("date", time.strftime("%Y-%m-%d %H:%M", time.localtime(float(p[3]))) + f"  ({ui.ago(float(p[3]))} ago)"),
        ("parents", " ".join(x[:8] for x in p[4].split()) or "root", "hash"),
        *([("refs", p[5], "branch")] if p[5] else []),
    ]))
    ui.console.print(Text("\n" + p[6].strip() + "\n"))
    _numstat_table(repo.run("show", "--numstat", "--format=", rev, check=False), "Files")
    diff = repo.run("show", "--format=", "--patch", "--no-color", rev, check=False)
    if diff.strip():
        ui.console.print(Syntax(diff[:20000], "diff", theme="ansi_dark", background_color="default"))


def timeline(repo: Repo, days: int = 30) -> None:
    out = repo.run("log", "--all", f"--since={days} days ago", "--format=%at", check=False)
    buckets = [0] * days
    now = time.time()
    for ts in out.split():
        d = int((now - float(ts)) // 86400)
        if 0 <= d < days:
            buckets[days - 1 - d] += 1
    ui.step(f"Activity, last {days} days  ({sum(buckets)} commits)")
    ui.console.print(Text("  " + ui.sparkline(buckets), style="accent"))
    ui.detail(f"peak {max(buckets)}/day  avg {sum(buckets) / days:.1f}/day")


def insights(repo: Repo) -> None:
    head = repo.head()
    ui.header(
        __import__("gitnolo").__version__,
        f"{repo.name}  on  {repo.branch_display()}",
        ui.kv_lines([
            ("path", ui.short_path(repo.root, 60), "path"),
            ("remote", repo.remote_url() or "none", "info"),
            ("commits", repo.run("rev-list", "--count", "HEAD", check=False).strip() if head else "0"),
            ("changes", f"{len(repo.changes())} files uncommitted"),
        ]),
    )
    if not head:
        return
    timeline(repo, 30)
    ui.console.print(_commit_table(_commits(repo, limit=8), title="Recent"))
    hotspots(repo, limit=8)
    contributors(repo, since="90 days ago")
