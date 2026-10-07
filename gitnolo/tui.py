"""Arrow-key terminal UI: menus, multi-select, commit graph browser, git ops and interactive rebase."""
from __future__ import annotations

import os
import select
import shlex
import sys
import tempfile
from typing import Callable, List, Optional, Sequence, Tuple

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from . import ui
from .gitcore import GitError, Repo

KEYS = {"\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left", "\x1bOA": "up", "\x1bOB": "down",
        "\x1b[H": "home", "\x1b[F": "end", "\x1b[5~": "pgup", "\x1b[6~": "pgdn", "\r": "enter", "\n": "enter",
        " ": "space", "\x1b": "esc", "\x03": "ctrl-c", "\x7f": "backspace", "k": "up", "j": "down"}


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.name == "posix"


def read_key() -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1).decode(errors="ignore")
        if ch == "\x1b":
            while select.select([fd], [], [], 0.03)[0]:
                ch += os.read(fd, 1).decode(errors="ignore")
                if ch[-1].isalpha() or ch[-1] == "~":
                    break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch == "\x03":
        raise KeyboardInterrupt
    return KEYS.get(ch, ch)


def _footer(hint: str) -> Text:
    t = Text("\n  ")
    for i, part in enumerate(hint.split("  ")):
        k, _, v = part.partition(" ")
        t.append(f" {k} ", "reverse accent") if k else None
        t.append(f" {v}   ", "muted")
    return t


def _window(n: int, cur: int, size: int) -> Tuple[int, int]:
    size = max(3, size)
    top = min(max(0, cur - size // 2), max(0, n - size))
    return top, min(n, top + size)


class Screen:
    """A redraw-on-key list view. `render(cur)` builds rows; `on_key(key, cur)` returns new cur, or a result."""

    def __init__(self, title: str, n: Callable[[], int], row: Callable[[int, bool], Text], hint: str,
                 detail: Optional[Callable[[int], object]] = None, start: int = 0):
        self.title, self.n, self.row, self.hint, self.detail, self.cur = title, n, row, hint, detail, start

    def view(self):
        n = self.n()
        height = max(5, ui.console.size.height - (14 if self.detail else 8))
        top, end = _window(n, self.cur, height)
        body = Text()
        for i in range(top, end):
            body.append_text(self.row(i, i == self.cur))
            body.append("\n")
        if not n:
            body.append("  nothing here\n", "muted")
        more = f" {self.cur + 1}/{n} " if n > height else ""
        parts: List[object] = [Panel(body, title=Text(f" {self.title} ", "accent.bold"), title_align="left",
                                     subtitle=Text(more, "muted"), subtitle_align="right", border_style="faint",
                                     padding=(0, 1))]
        if self.detail and n:
            parts.append(self.detail(self.cur))
        parts.append(_footer(self.hint))
        return Group(*parts)

    def run(self, on_key: Callable[[str, "Screen"], object]):
        with Live(self.view(), console=ui.console, auto_refresh=False, transient=True) as live:
            while True:
                k = read_key()
                n = self.n()
                if k == "up":
                    self.cur = (self.cur - 1) % n if n else 0
                elif k == "down":
                    self.cur = (self.cur + 1) % n if n else 0
                elif k == "pgup":
                    self.cur = max(0, self.cur - 10)
                elif k == "pgdn":
                    self.cur = min(max(0, n - 1), self.cur + 10)
                elif k == "home":
                    self.cur = 0
                elif k == "end":
                    self.cur = max(0, n - 1)
                else:
                    res = on_key(k, self)
                    if res is not None:
                        return res
                live.update(self.view(), refresh=True)


def _pointer(sel: bool) -> Text:
    return Text(" ❯ " if sel else "   ", "accent.bold")


def menu(title: str, items: Sequence[Tuple[str, str]], start: int = 0, keys: Sequence[str] = ()) -> Optional[int]:
    """Arrow-key menu. Returns the chosen index, or None on esc/q. `keys` are optional shortcut letters."""
    width = max((len(a) for a, _ in items), default=0) + 3

    def row(i: int, sel: bool) -> Text:
        name, desc = items[i]
        t = _pointer(sel)
        t.append(f"{keys[i] if i < len(keys) else ' '} ", "faint")
        t.append(f"{name:<{width}}", "reverse bold" if sel else "bold")
        t.append(f" {desc}", "path" if sel else "muted")
        return t

    def on_key(k: str, s: Screen):
        if k in ("enter", "right"):
            return s.cur
        if k in ("esc", "q", "left"):
            return -1
        if k in keys:
            return list(keys).index(k)
        return None

    r = Screen(title, lambda: len(items), row, "↑↓ move  ⏎ select  esc back", start=start).run(on_key)
    return None if r == -1 else r


def multiselect(title: str, items: Sequence[Tuple[str, str]], chosen: Sequence[bool]) -> Optional[List[bool]]:
    marks = list(chosen)

    def row(i: int, sel: bool) -> Text:
        label, style = items[i]
        t = _pointer(sel)
        t.append("◉ " if marks[i] else "○ ", "ok" if marks[i] else "faint")
        t.append(label, style + (" reverse" if sel else ""))
        return t

    def on_key(k: str, s: Screen):
        if k == "space" and marks:
            marks[s.cur] = not marks[s.cur]
            s.cur = min(s.cur + 1, len(marks) - 1)
        elif k == "a":
            v = not all(marks)
            marks[:] = [v] * len(marks)
        elif k == "enter":
            return marks
        elif k in ("esc", "q"):
            return False
        return None

    r = Screen(title, lambda: len(items), row, "␣ toggle  a all  ⏎ apply  esc cancel").run(on_key)
    return None if r is False else r


def ask(label: str, default: str = "") -> str:
    try:
        return Prompt.ask(Text(f"  {label}", "accent"), default=default or None, console=ui.console) or ""
    except (EOFError, KeyboardInterrupt):
        return ""


READ_ONLY = {"status", "log", "show", "diff", "for-each-ref", "rev-parse", "merge-base", "fetch"}
_PUBLIC: dict = {}


def _do(repo: Repo, *args: str, done: str = "") -> bool:
    if args[0] not in READ_ONLY and not public_ok(repo):
        return False
    try:
        out = repo.run(*args)
        ui.ok(done or f"git {' '.join(args)}")
        if out.strip():
            ui.detail(out.strip()[-600:])
        return True
    except GitError as e:
        ui.error(str(e))
        return False


def public_ok(repo: Repo) -> bool:
    """Community build writes only to public repositories (checked once per repo)."""
    from . import edition

    if repo.root not in _PUBLIC:
        _PUBLIC[repo.root] = edition.public_repo_error(repo)
    if _PUBLIC[repo.root]:
        ui.error(_PUBLIC[repo.root])
    return not _PUBLIC[repo.root]


# ------------------------------------------------------------------ commit graph
GRAPH_FMT = "%x1f%h%x1f%d%x1f%s%x1f%an%x1f%ar"
LANES = ["#e06c75", "#6cb6d9", "#7fb069", "#e0b354", "#c792ea", "#56b6c2", "#d19a66"]


def _graph_rows(repo: Repo, limit: int) -> List[Tuple[Text, Optional[str]]]:
    out = repo.run("log", "--graph", "--all", "--date-order", f"-n{limit}", f"--format={GRAPH_FMT}", check=False)
    rows = []
    for line in out.splitlines():
        lanes, _, rest = line.partition("\x1f")
        t = Text()
        for j, ch in enumerate(lanes):
            t.append("●" if ch == "*" else ch, f"bold {LANES[(j // 2) % len(LANES)]}")
        sha = None
        if rest:
            sha, refs, subj, who, when = (rest.split("\x1f") + [""] * 5)[:5]
            t.append(f" {sha} ", "hash")
            for ref in filter(None, (r.strip() for r in refs.strip(" ()").split(","))):
                head = ref.startswith("HEAD")
                t.append(f" {ref.replace('HEAD -> ', '⏵ ')} ", "bold black on #e0b354" if head else
                         ("bold black on #c792ea" if ref.startswith("tag:") else "bold black on #6cb6d9"))
                t.append(" ")
            t.append(subj[:90], "path")
            t.append(f"  {who}, {when}", "faint")
        rows.append((t, sha))
    return rows


def graph(repo: Repo, limit: int = 300) -> int:
    rows = _graph_rows(repo, limit)
    commits = [i for i, (_, s) in enumerate(rows) if s]

    def detail(i: int):
        sha = rows[commits[i]][1]
        stat = repo.run("show", "--stat", "--format=%an <%ae>%n%ad%n%n%B", "--date=format:%Y-%m-%d %H:%M", sha,
                        check=False)
        lines = stat.strip().splitlines()
        return Panel(Text("\n".join(lines[:6] + (["…"] if len(lines) > 6 else [])), "muted"),
                     title=Text(f" {sha} ", "hash"), title_align="left", border_style="faint", height=8)

    def row(i: int, sel: bool) -> Text:
        t = _pointer(sel) + rows[commits[i]][0].copy()
        if sel:
            t.stylize("on #2a2a2a", 3)
        return t

    actions = [("show", "Full commit with diff"), ("checkout", "Check out this commit (detached)"),
               ("branch", "Create a branch here"), ("tag", "Tag this commit"),
               ("cherry-pick", "Apply onto current branch"), ("revert", "Make a commit that undoes it"),
               ("reset", "Move current branch here (keeps changes)"),
               ("rebase", "Interactively rebase everything after this commit"),
               ("explain", "AI: explain what this commit did and why")]

    while True:
        def on_key(k: str, s: Screen):
            if k in ("esc", "q"):
                return ("quit", 0)
            if k in ("enter", "right"):
                return ("act", s.cur)
            return None

        kind, cur = Screen("Commit graph", lambda: len(commits), row, "↑↓ move  ⏎ actions  esc back",
                           detail=detail).run(on_key)
        if kind == "quit":
            return 0
        sha = rows[commits[cur]][1]
        a = menu(f"Commit {sha}", actions)
        if a is None:
            continue
        name = actions[a][0]
        if name == "show":
            from . import lens

            with ui.console.pager(styles=True):
                lens.show(repo, sha)
            continue
        if name == "checkout":
            _do(repo, "checkout", sha)
        elif name == "branch":
            b = ask("branch name")
            b and _do(repo, "switch", "-c", b, sha)
        elif name == "tag":
            t = ask("tag name")
            t and _do(repo, "tag", t, sha)
        elif name == "cherry-pick":
            _do(repo, "cherry-pick", sha)
        elif name == "revert":
            _do(repo, "revert", "--no-edit", sha)
        elif name == "reset":
            _do(repo, "reset", "--mixed", sha)
        elif name == "rebase":
            rebase(repo, sha)
        elif name == "explain":
            explain(repo, sha)
            ask("⏎ back")
        rows[:] = _graph_rows(repo, limit)
        commits[:] = [i for i, (_, s) in enumerate(rows) if s]


# ------------------------------------------------------------------ point-and-select operations
def _stage(repo: Repo) -> None:
    out = repo.run("status", "--porcelain=v1", "-uall", check=False)
    files = [(ln[:2], ln[3:].split(" -> ")[-1].strip('"')) for ln in out.splitlines() if ln.strip()]
    if not files:
        ui.ok("working tree clean")
        return
    style = {"M": "warn", "A": "ok", "D": "err", "?": "info", "R": "info", "U": "err"}
    items = [(f"{xy}  {p}", style.get((xy.strip() or "M")[0], "path")) for xy, p in files]
    picked = multiselect("Stage files", items, [xy[0] not in " ?" for xy, _ in files])
    if picked is None:
        return
    add = [p for (xy, p), on in zip(files, picked) if on]
    unstage = [p for (xy, p), on in zip(files, picked) if not on and xy[0] not in " ?"]
    if add:
        _do(repo, "add", "-A", "--", *add, done=f"staged {len(add)} file(s)")
    if unstage:
        _do(repo, "restore", "--staged", "--", *unstage, done=f"unstaged {len(unstage)} file(s)")


def _suggest_message(repo: Repo) -> str:
    try:
        from .messages import summarize_multi

        paths = repo.run("diff", "--cached", "--name-only", check=False).split()
        return summarize_multi(paths) if paths else ""
    except Exception:  # message suggestion is best-effort
        return ""


def _commit(repo: Repo) -> None:
    if not repo.run("diff", "--cached", "--name-only", check=False).strip():
        ui.warn("nothing staged; pick files first")
        _stage(repo)
        if not repo.run("diff", "--cached", "--name-only", check=False).strip():
            return
    msg = ask("message", _suggest_message(repo))
    msg and _do(repo, "commit", "-m", msg, done="committed")


def _branches(repo: Repo, remote: bool = False) -> List[str]:
    ref = "refs/remotes" if remote else "refs/heads"
    return [b for b in repo.run("for-each-ref", "--format=%(refname:short)", ref, check=False).split() if b]


def _switch(repo: Repo) -> None:
    names = _branches(repo)
    cur = repo.branch()
    i = menu("Switch branch", [(b, "current" if b == cur else "") for b in names],
             start=names.index(cur) if cur in names else 0)
    if i is not None and names[i] != cur:
        _do(repo, "switch", names[i])


def _merge(repo: Repo) -> None:
    names = [b for b in _branches(repo) + _branches(repo, True) if b != repo.branch() and not b.endswith("/HEAD")]
    i = menu(f"Merge into {repo.branch_display()}", [(b, "") for b in names])
    if i is not None and not _do(repo, "merge", "--no-edit", names[i]) and repo.conflicted_files():
        ui.warn("conflicts: opening merge editor")
        merge_editor(repo)


OPS = [("stage", "Pick files to stage / unstage"), ("commit", "Commit staged changes (message suggested)"),
       ("push", "Push current branch"), ("pull", "Pull with rebase"), ("fetch", "Fetch all remotes"),
       ("switch", "Switch branch"), ("new branch", "Create and switch to a branch"),
       ("merge", "Merge a branch into this one"), ("tag", "Tag HEAD"), ("stash", "Stash changes"),
       ("pop", "Apply latest stash"), ("rebase", "Reorder / squash / drop recent commits"),
       ("conflicts", "Side-by-side merge conflict editor"), ("graph", "Browse the commit graph")]


def ops(repo: Repo) -> int:
    start = 0
    while True:
        st = repo.run("status", "-sb", check=False).splitlines()
        i = menu(f"{repo.name} · {st[0][3:] if st else repo.branch_display()} · {len(st) - 1} changed", OPS, start)
        if i is None:
            return 0
        start, name = i, OPS[i][0]
        ui.console.print()
        if name == "stage":
            _stage(repo)
        elif name == "commit":
            _commit(repo)
        elif name == "push":
            br = repo.branch()
            br and _do(repo, "push", "-u", "origin", br)
        elif name == "pull":
            if not _do(repo, "pull", "--rebase", "--autostash") and repo.conflicted_files():
                merge_editor(repo)
        elif name == "conflicts":
            merge_editor(repo)
        elif name == "fetch":
            _do(repo, "fetch", "--all", "--prune")
        elif name == "switch":
            _switch(repo)
        elif name == "new branch":
            b = ask("branch name")
            b and _do(repo, "switch", "-c", b)
        elif name == "merge":
            _merge(repo)
        elif name == "tag":
            t = ask("tag name")
            t and _do(repo, "tag", "-a", t, "-m", ask("tag message", t))
        elif name == "stash":
            _do(repo, "stash", "push", "-u")
        elif name == "pop":
            _do(repo, "stash", "pop")
        elif name == "rebase":
            rebase(repo)
        elif name == "graph":
            graph(repo)


# ------------------------------------------------------------------ interactive rebase
ACTION_STYLE = {"pick": "ok", "squash": "warn", "fixup": "warn", "reword": "info", "drop": "err strike"}


def rebase(repo: Repo, base: Optional[str] = None) -> int:
    if base is None:
        up = repo.rev("@{u}")
        base = repo.run("merge-base", "HEAD", up, check=False).strip() if up else ""
        if not base or base == repo.head():
            base = "HEAD~10" if repo.rev("HEAD~10") else ""
    if not public_ok(repo):
        return 1
    rng = f"{base}..HEAD" if base else "HEAD"
    log = repo.run("log", "--reverse", "--format=%h%x1f%s", rng, check=False).splitlines()
    todo = [["pick", *ln.split("\x1f", 1)] for ln in log if ln]
    if not todo:
        ui.warn("no commits to rebase")
        return 0
    grabbed = [False]

    def row(i: int, sel: bool) -> Text:
        act, sha, subj = todo[i]
        t = _pointer(sel)
        t.append("⇅ " if sel and grabbed[0] else "  ", "accent.bold")
        t.append(f"{act:<7}", ACTION_STYLE[act])
        t.append(f" {sha} ", "hash")
        t.append(subj, ("reverse " if sel else "") + ("faint strike" if act == "drop" else "path"))
        return t

    def on_key(k: str, s: Screen):
        c = s.cur
        if k == "space":
            grabbed[0] = not grabbed[0]
        elif k in ("p", "s", "f", "r", "d"):
            todo[c][0] = {"p": "pick", "s": "squash", "f": "fixup", "r": "reword", "d": "drop"}[k]
            if todo[c][0] == "reword":
                msg = ask(f"new message for {todo[c][1]}", todo[c][2])
                todo[c][2] = msg or todo[c][2]
        elif k in ("K", "J") or (grabbed[0] and k in ("up", "down")):
            j = c - 1 if k in ("K", "up") else c + 1
            if 0 <= j < len(todo):
                todo[c], todo[j] = todo[j], todo[c]
                s.cur = j
        elif k == "enter":
            return "go"
        elif k in ("esc", "q"):
            return "cancel"
        return None

    class Grab(Screen):  # arrows move the grabbed commit instead of the cursor
        def run(self, cb):
            with Live(self.view(), console=ui.console, auto_refresh=False, transient=True) as live:
                while True:
                    k = read_key()
                    if not grabbed[0] and k in ("up", "down"):
                        self.cur = (self.cur + (1 if k == "down" else -1)) % len(todo)
                    else:
                        r = cb(k, self)
                        if r:
                            return r
                    live.update(self.view(), refresh=True)

    hint = "␣ grab/drop  ↑↓ move  p pick  s squash  f fixup  r reword  d drop  ⏎ apply  esc cancel"
    if Grab(f"Rebase {len(todo)} commits onto {base[:10] or 'root'} (oldest first)", lambda: len(todo), row,
            hint).run(on_key) != "go":
        ui.detail("rebase cancelled")
        return 0
    if todo[0][0] in ("squash", "fixup"):
        todo[0][0] = "pick"
    lines = []
    for act, sha, subj in todo:
        if act == "reword":
            lines += [f"pick {sha}", f"exec git commit --amend --allow-empty -m {shlex.quote(subj)}"]
        else:
            lines.append(f"{act} {sha} {subj}")
    with tempfile.NamedTemporaryFile("w", suffix=".todo", delete=False) as f:
        f.write("\n".join(lines) + "\n")
    editor = f"cp {shlex.quote(f.name)}"
    try:
        out = repo.run("-c", f"sequence.editor={editor}", "-c", "core.editor=true", "rebase", "-i", "--autostash",
                       *([base] if base else ["--root"]))
        ui.ok("rebase complete")
        out.strip() and ui.detail(out.strip()[-400:])
    except GitError as e:
        ui.error(str(e))
        ui.warn("rebase stopped: resolve with `gitnolo conflict`, or `git rebase --abort`")
    finally:
        os.unlink(f.name)
    return 0


# ------------------------------------------------------------------ AI client (lazy, shared)
_LLM: List[object] = []


def _client():
    if not _LLM:
        try:
            from .ai import make_client
            from .config import AppConfig

            _LLM.append(make_client(AppConfig.load()))
        except Exception:  # AI is optional
            _LLM.append(None)
    return _LLM[0]


# ------------------------------------------------------------------ visual merge editor
def merge_editor(repo: Repo, llm=None) -> bool:
    """Side-by-side 3-way conflict editor: ours | base | theirs, result preview, arrow-key choices."""
    from rich.syntax import Syntax
    from rich.table import Table

    from .conflict_resolver import ConflictFileParser

    if not public_ok(repo):
        return False
    llm = llm if llm is not None else _client()
    files = repo.conflicted_files()
    if not files:
        ui.ok("no conflicts")
        return True
    for fi, path in enumerate(files):
        full = os.path.join(repo.root, path)
        hunks = ConflictFileParser.parse_file(full)
        if not hunks:
            continue
        lang = os.path.splitext(path)[1].lstrip(".") or "text"
        cur, opt, note = 0, 0, ""

        def options(h):
            o = [("ours", h.ours_content), ("both", h.ours_content + h.theirs_content), ("theirs", h.theirs_content)]
            if h.base_content is not None:
                o.append(("base", h.base_content))
            return o

        def pane(code: str, title: str, style: str, lines: int):
            body = "\n".join(code.rstrip("\n").splitlines()[:lines]) or " "
            return Panel(Syntax(body, lang, theme="ansi_dark", background_color="default", word_wrap=True),
                         title=Text(f" {title} ", style), title_align="left", border_style=style, padding=(0, 1))

        def view():
            h = hunks[cur]
            lines = max(4, (ui.console.size.height - 16) // 2)
            names = dict(ours=f"ours · {h.ours_label or 'HEAD'}", theirs=f"theirs · {h.theirs_label or 'incoming'}",
                         base="base")
            choice = options(h)[opt][0]
            grid = Table.grid(expand=True, padding=(0, 1))
            cols = ["ours"] + (["base"] if h.base_content is not None else []) + ["theirs"]
            for _ in cols:
                grid.add_column(ratio=1)
            hl = {"ours": ("ours", "both"), "theirs": ("theirs", "both"), "base": ("base",)}
            grid.add_row(*[pane(getattr(h, f"{c}_content"), names[c], "accent.bold" if choice in hl[c] else "faint",
                                lines) for c in cols])
            done = sum(1 for x in hunks if x.resolved_content is not None)
            result = h.resolved_content if h.resolved_content is not None else options(h)[opt][1]
            tabs = Text("  ")
            for i, (n, _) in enumerate(options(h)):
                tabs.append(f" {n} ", "reverse accent.bold" if i == opt else "muted")
                tabs.append(" ")
            head = Text.assemble((f" {path} ", "bold path"), (f"  file {fi + 1}/{len(files)}", "muted"),
                                 (f"  conflict {cur + 1}/{len(hunks)}", "warn"), (f"  {done} resolved", "ok"))
            res = pane(result, "result" + (" ✓" if h.resolved_content is not None else " (preview)"),
                       "ok" if h.resolved_content is not None else "info", lines)
            return Group(head, grid, tabs, res, *( [Text("  " + note, "muted")] if note else []),
                         _footer("←→ choose  ⏎ accept  ↑↓ conflict  a AI merge  e edit  u undo  esc quit"))

        with Live(view(), console=ui.console, auto_refresh=False, transient=True, screen=True) as live:
            while True:
                k, h, note = read_key(), hunks[cur], ""
                if k in ("left", "right"):
                    opt = (opt + (1 if k == "right" else -1)) % len(options(h))
                elif k in ("up", "down"):
                    cur, opt = (cur + (1 if k == "down" else -1)) % len(hunks), 0
                elif k == "enter":
                    h.resolved_content = options(h)[opt][1]
                    nxt = [i for i, x in enumerate(hunks) if x.resolved_content is None]
                    if not nxt:
                        break
                    cur, opt = nxt[0], 0
                elif k == "u":
                    h.resolved_content = None
                elif k == "a":
                    if not llm:
                        note = "AI is off: run `gitnolo ai` to set up Ollama or OpenRouter"
                    else:
                        live.update(Text("  AI is merging this conflict…", "accent"), refresh=True)
                        try:
                            r = llm.resolve_conflict_ai(path, h.ours_content, h.theirs_content, h.context_before,
                                                        h.context_after, h.base_content)
                            if r.get("merged_code"):
                                h.resolved_content = r["merged_code"]
                                note = "AI: " + r.get("explanation", "")[:200]
                            else:
                                note = "AI returned nothing"
                        except Exception as e:  # network / model errors
                            note = f"AI failed: {e}"
                elif k == "e":
                    live.stop()
                    h.resolved_content = _edit(h.resolved_content or options(h)[opt][1], path)
                    live.start()
                elif k in ("esc", "q"):
                    return False
                live.update(view(), refresh=True)
        if ConflictFileParser.apply_resolutions(full, hunks):
            _do(repo, "add", "--", path, done=f"resolved {path}")
    if repo.conflicted_files():
        return False
    op = repo.operation_in_progress()
    if op in ("merge", "rebase", "cherry-pick", "revert") and menu(f"All conflicts resolved. Continue the {op}?",
                                                                     [("continue", ""), ("not now", "")]) == 0:
        args = {"merge": ["commit", "--no-edit"]}.get(op, ["-c", "core.editor=true", op, "--continue"])
        _do(repo, *args, done=f"{op} completed")
    return True


def _edit(text: str, path: str) -> str:
    import subprocess

    with tempfile.NamedTemporaryFile("w", suffix=os.path.splitext(path)[1], delete=False) as f:
        f.write(text)
    try:
        subprocess.call(shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi") + [f.name])
        return open(f.name).read()
    finally:
        os.unlink(f.name)


# ------------------------------------------------------------------ AI: explain history
def explain(repo: Repo, target: str, llm=None) -> int:
    """Explain a commit, a file's history, or FILE:START-END line history in plain language."""
    llm = llm if llm is not None else _client()
    path, _, rng = target.rpartition(":")
    if path and rng.replace("-", "").isdigit():
        a, _, b = rng.partition("-")
        ctx = repo.run("log", f"-L{a},{b or a}:{path}", "-n15", "--format=commit %h %an %ad%n%s%n%b", "--date=short",
                       check=False)
        what = f"the history of lines {rng} of {path}"
    elif os.path.exists(os.path.join(repo.root, target)):
        ctx = repo.run("log", "--follow", "-p", "-n12", "--stat", "--format=commit %h %an %ad%n%s%n%b", "--date=short",
                       "--", target, check=False)
        what = f"the history of {target}"
    else:
        ctx = repo.run("show", "--stat", "-p", "--format=commit %h %an %ad%n%s%n%b", "--date=short", target)
        what = f"commit {target}"
    if not ctx.strip():
        ui.warn(f"no history for {target}")
        return 1
    if not llm:
        ui.warn("AI is off (run `gitnolo ai`); showing the raw history instead")
        ui.console.print(Text(ctx[:6000], "muted"))
        return 0
    prompt = (f"Explain {what} to a developer joining the project. Cover: what changed and why (infer intent from "
              "messages and diffs), how the code evolved, who drove it, and any risks or follow-ups. Be concise; use "
              f"short bullet points and markdown.\n\n{ctx[:14000]}")
    with ui.console.status(Text(f"  reading {what}…", "accent"), spinner="dots"):
        try:
            out = llm.generate(prompt, system="You are a senior engineer explaining git history.", timeout=120,
                               max_tokens=3000)
        except Exception as e:  # network / model errors
            ui.error(f"AI failed: {e}")
            return 1
    from rich.markdown import Markdown

    ui.step(f"Explain {what}")
    ui.console.print(Panel(Markdown(out.strip() or "(no answer)"), border_style="faint", padding=(1, 2)))
    return 0
