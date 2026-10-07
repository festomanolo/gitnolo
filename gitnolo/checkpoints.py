"""
Checkpoints: a snapshot of the whole working tree (tracked and untracked,
minus ignored files) taken the moment an agent starts a turn, so any agent's
work can be rewound, not only the one with a built-in undo.

A checkpoint is a commit object under refs/gitnolo/checkpoints/, built from a
throwaway copy of the index: the working tree, the real index and HEAD are not
touched, and the refs are never pushed.
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "shutil", "tempfile", "gitnolo.gitcore",
]

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .gitcore import GitError, Repo

PREFIX = "refs/gitnolo/checkpoints/"


@dataclass
class Checkpoint:
    ref: str
    sha: str
    t: float
    label: str

    @property
    def short(self) -> str:
        return self.ref[len(PREFIX):]


def _snapshot_tree(repo: Repo) -> str:
    """Tree of the working tree as `git add -A` would stage it, without touching the real index."""
    fd, tmp_index = tempfile.mkstemp(prefix="gitnolo-index-")
    os.close(fd)
    try:
        real = os.path.join(repo.git_dir, "index")
        if os.path.exists(real):
            shutil.copyfile(real, tmp_index)  # keeps stat data, so only changed files are re-hashed
        else:
            os.remove(tmp_index)
        env = {"GIT_INDEX_FILE": tmp_index}
        repo.run("add", "-A", "--", ".", env=env, timeout=300)
        return repo.run("write-tree", env=env).strip()
    finally:
        if os.path.exists(tmp_index):
            os.remove(tmp_index)


def create(repo: Repo, label: str, keep: int = 60) -> Optional[Checkpoint]:
    """Snapshots the working tree. Returns None when nothing changed since the last checkpoint."""
    tree = _snapshot_tree(repo)
    last = latest(repo)
    if last and repo.run("rev-parse", f"{last.sha}^{{tree}}", check=False).strip() == tree:
        return None
    head = repo.head()
    args = ["commit-tree", tree, "-m", label] + (["-p", head] if head else [])
    sha = repo.run(*args).strip()
    t = time.time()
    ref = PREFIX + time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + f"-{int(t * 1000) % 1000:03d}"
    repo.update_ref(ref, sha, None, "gitnolo: checkpoint")
    prune(repo, keep)
    return Checkpoint(ref, sha, t, label)


def list_all(repo: Repo) -> List[Checkpoint]:
    out = repo.run("for-each-ref", "--sort=-creatordate", "--format=%(refname)%00%(objectname)%00%(creatordate:unix)%00%(subject)",
                   PREFIX, check=False)
    cps = []
    for line in out.splitlines():
        parts = line.split("\0")
        if len(parts) == 4:
            cps.append(Checkpoint(parts[0], parts[1], float(parts[2] or 0), parts[3]))
    return cps


def latest(repo: Repo) -> Optional[Checkpoint]:
    cps = list_all(repo)
    return cps[0] if cps else None


def prune(repo: Repo, keep: int) -> None:
    for cp in list_all(repo)[max(1, keep):]:
        repo.delete_ref(cp.ref)


def find(repo: Repo, ident: str) -> Optional[Checkpoint]:
    cps = list_all(repo)
    if ident.isdigit() and int(ident) < 1000:
        i = int(ident) - 1  # 1 = newest, as listed
        return cps[i] if 0 <= i < len(cps) else None
    for cp in cps:
        if cp.short == ident or cp.ref == ident or cp.sha.startswith(ident):
            return cp
    return None


def diff_from_now(repo: Repo, cp: Checkpoint) -> Tuple[List[str], List[str], List[str]]:
    """(changed, added_since, deleted_since) between the checkpoint and the current working tree."""
    now = _snapshot_tree(repo)
    out = repo.run("diff-tree", "-r", "--no-renames", "--name-status", "-z", f"{cp.sha}^{{tree}}", now, check=False)
    fields = [f for f in out.split("\0") if f]
    changed, added, deleted = [], [], []
    for status, path in zip(fields[::2], fields[1::2]):
        {"A": added, "D": deleted}.get(status[0], changed).append(path)
    return changed, added, deleted


def restore(repo: Repo, cp: Checkpoint) -> Tuple[Optional[Checkpoint], int]:
    """Makes the working tree match `cp`. Takes a safety checkpoint first. Returns (safety, files touched)."""
    safety = create(repo, f"before rewind to {cp.short}", keep=10**6)
    changed, added, deleted = diff_from_now(repo, cp)
    for path in added:  # files created after the checkpoint
        full = os.path.join(repo.root, path)
        try:
            os.remove(full)
        except FileNotFoundError:
            pass
        _prune_empty_dirs(repo.root, os.path.dirname(full))
    restore_paths = changed + deleted
    for i in range(0, len(restore_paths), 200):
        chunk = restore_paths[i : i + 200]
        code, _, err = repo.run_bytes("restore", f"--source={cp.sha}", "--worktree", "--", *chunk)
        if code != 0:
            raise GitError(f"rewind failed: {err.decode('utf-8', 'replace').strip()}")
    return safety, len(changed) + len(added) + len(deleted)


def _prune_empty_dirs(root: str, d: str) -> None:
    root = os.path.abspath(root)
    d = os.path.abspath(d)
    while d.startswith(root + os.sep) and os.path.isdir(d) and not os.listdir(d):
        os.rmdir(d)
        d = os.path.dirname(d)
