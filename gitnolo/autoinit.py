"""
Auto `git init` for folders an agent works in, plus history backfill.

When an agent runs in a project folder that is not a git repository, the
folder is initialised, linked to a same-named GitHub repo of the signed-in user
when one exists, and its files are *backfilled*: committed in micro-commits
dated by each file's modification time, oldest first, so a project written
years before it met git still gets a traceable history (`gitnolo history`).
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "gitnolo.github", "gitnolo.messages", "gitnolo.microcommit",
]

import os
import subprocess
import time
from collections import OrderedDict
from typing import Callable, List, Optional, Tuple

from .gitcore import GitError, Repo

Note = Callable[[str, str], None]

SKIP_DIRS = {"/", "/tmp", "/private/tmp", "/Applications", "/Library", "/System", "/Users", "/Volumes", "/opt", "/usr"}
HOME_SKIP = {"", "Desktop", "Documents", "Downloads", "Library", "Movies", "Music", "Pictures", "Public", "Applications"}
MARKERS = (
    "package.json", "pyproject.toml", "setup.py", "requirements.txt", "Cargo.toml", "go.mod", "pom.xml",
    "build.gradle", "Gemfile", "composer.json", "Makefile", "CMakeLists.txt", "index.html", "README.md",
    "CLAUDE.md", "AGENTS.md", "pubspec.yaml", "Package.swift", "deno.json", "src", "app", "lib",
)
GITIGNORE = """\
.DS_Store
node_modules/
__pycache__/
*.pyc
.venv/
venv/
.env
.env.*
dist/
build/
.next/
target/
*.log
"""
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_BACKFILL_FILES = 5000


def _git(path: str, *args: str, env: Optional[dict] = None) -> Tuple[int, str]:
    p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=120,
                       env={**os.environ, **(env or {})})
    return p.returncode, (p.stdout or p.stderr).strip()


def eligible(path: Optional[str]) -> bool:
    """A project folder worth initialising: not home, a system dir or a bare container folder."""
    if not path or not os.path.isdir(path):
        return False
    path = os.path.realpath(path)
    home = os.path.realpath(os.path.expanduser("~"))
    if path in SKIP_DIRS or path == home or os.path.dirname(path) == "/Volumes":
        return False
    if path.startswith(home + os.sep) and os.path.relpath(path, home).split(os.sep)[0] in ("Library", ".Trash"):
        return False
    if os.path.dirname(path) == home and os.path.basename(path) in HOME_SKIP:
        return False
    if any(part.startswith(".") for part in path.split(os.sep)[1:]):  # ~/.config, ~/.claude ...
        return False
    try:
        names = set(os.listdir(path))
    except OSError:
        return False
    return any(m in names for m in MARKERS) or any(n.endswith((".py", ".js", ".ts", ".go", ".rs", ".swift")) for n in names)


def _link_remote(path: str, config, note: Note) -> Optional[str]:
    """Adds `origin` when the signed-in GitHub user has a repo named like the folder."""
    from .github import GitHub, GitHubError, resolve_token
    token = resolve_token(getattr(config, "github_token", None))
    if not token:
        return None
    gh = GitHub("", token)
    login = gh.viewer()
    if not login:
        return None
    name = os.path.basename(os.path.realpath(path))
    try:
        info = gh.request("GET", f"/repos/{login}/{name}")
    except GitHubError:
        return None
    url = info.get("clone_url") or f"https://github.com/{login}/{name}.git"
    _git(path, "remote", "add", "origin", url)
    note("ok", f"{name}: linked to github.com/{login}/{name}")
    branch = info.get("default_branch") or "main"
    if _git(path, "fetch", "--quiet", "origin")[0] == 0 and _git(path, "rev-parse", "--verify", "-q", f"origin/{branch}")[0] == 0:
        # Adopt the remote history; the working tree stays as the agent left it.
        _git(path, "reset", "--quiet", f"origin/{branch}")
        _git(path, "branch", "--set-upstream-to", f"origin/{branch}")
    return url


def init(path: str, config=None, note: Optional[Note] = None) -> bool:
    """`git init` a folder (main branch, .gitignore, optional GitHub link). True when a repo was created."""
    note = note or (lambda k, t: None)
    if not eligible(path) or os.path.exists(os.path.join(path, ".git")):
        return False
    code, out = _git(path, "init", "-q", "-b", "main")
    if code != 0:
        note("warn", f"git init failed in {path}: {out}")
        return False
    gi = os.path.join(path, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write(GITIGNORE)
    note("ok", f"{os.path.basename(path)}: git init (agent was working outside git)")
    if config is None or getattr(config, "auto_init_link_remote", True):
        try:
            _link_remote(path, config, note)
        except Exception as e:  # network is optional here
            note("warn", f"remote link skipped: {e}")
    return True


def _untracked(repo: Repo) -> List[Tuple[float, str]]:
    from .microcommit import is_sensitive
    out = repo.run("ls-files", "--others", "--exclude-standard", "-z", check=False)
    files = []
    for rel in out.split("\0"):
        if not rel or is_sensitive(rel):
            continue
        try:
            st = os.lstat(os.path.join(repo.root, rel))
        except OSError:
            continue
        if st.st_size <= MAX_FILE_BYTES:
            files.append((st.st_mtime, rel))
    files.sort()
    return files[:MAX_BACKFILL_FILES]


def _subject(paths: List[str]) -> str:
    from .messages import summarize_multi
    s = summarize_multi(paths)
    return s.replace(": update ", ": add ", 1)


def backfill(path: str, per_commit: int = 3, note: Optional[Note] = None, dry_run: bool = False) -> int:
    """
    Commits untracked files as dated micro-commits: grouped by modification day and
    folder, at most `per_commit` files each, author/committer date = newest mtime in
    the group. Returns the number of commits (planned, with dry_run).
    """
    note = note or (lambda k, t: None)
    repo = Repo(path)
    files = _untracked(repo)
    groups: "OrderedDict[Tuple[str, str], List[Tuple[float, str]]]" = OrderedDict()
    for mt, rel in files:
        groups.setdefault((time.strftime("%Y-%m-%d", time.localtime(mt)), os.path.dirname(rel)), []).append((mt, rel))
    chunks = [g[i:i + max(1, per_commit)] for g in groups.values() for i in range(0, len(g), max(1, per_commit))]
    chunks.sort(key=lambda c: c[-1][0])
    if dry_run:
        return len(chunks)
    made = 0
    for chunk in chunks:
        paths = [rel for _, rel in chunk]
        when = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(chunk[-1][0]))
        try:
            repo.run("add", "--", *paths)
            repo.run("commit", "-q", "--no-verify", "-m", _subject(paths),
                     env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
        except GitError as e:
            note("warn", f"backfill stopped: {e}")
            break
        made += 1
    if made:
        note("ok", f"{os.path.basename(repo.root)}: backfilled {made} dated commits from {len(files)} files")
    return made


def ensure(path: str, config=None, note: Optional[Note] = None) -> bool:
    """init + backfill; the watcher calls this for agent folders outside git."""
    if not init(path, config, note):
        return False
    if config is None or getattr(config, "auto_backfill", True):
        backfill(path, getattr(config, "backfill_files_per_commit", 3), note)
    return True
