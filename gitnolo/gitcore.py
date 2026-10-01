"""
Fast, shell-free Git layer.

Every call goes through argv lists (no shell quoting bugs), parses NUL-delimited
output, and uses plumbing (cat-file --batch, fast-import, update-ref) so large
operations stay fast and never touch the user's working tree.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
}


class GitError(RuntimeError):
    pass


def _resolve_git() -> str:
    """Bypasses the macOS /usr/bin/git xcrun shim, which doubles spawn cost."""
    override = os.environ.get("GITNOLO_GIT")
    if override:
        return override
    found = shutil.which("git") or "git"
    if found == "/usr/bin/git" and sys.platform == "darwin":
        for cand in (
            "/Library/Developer/CommandLineTools/usr/bin/git",
            "/Applications/Xcode.app/Contents/Developer/usr/bin/git",
        ):
            if os.access(cand, os.X_OK):
                return cand
    return found


GIT = _resolve_git()
_ENV_CACHE: Optional[Dict[str, str]] = None


def git_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    global _ENV_CACHE
    if _ENV_CACHE is None:
        _ENV_CACHE = os.environ.copy()
        _ENV_CACHE.update(GIT_ENV)
    if extra:
        env = dict(_ENV_CACHE)
        env.update(extra)
        return env
    return _ENV_CACHE


@dataclass
class FileChange:
    path: str
    kind: str  # "add" | "modify" | "delete"
    old_mode: Optional[str] = None
    new_mode: Optional[str] = None


class Repo:
    """A git repository rooted at `root`."""

    def __init__(self, path: str = "."):
        path = os.path.abspath(os.path.expanduser(path))
        root = find_root(path)
        if not root:
            raise GitError(f"Not a git repository: {path}")
        self.root = root
        self._git_dir: Optional[str] = None
        self._object_format: Optional[str] = None

    # ------------------------------------------------------------------ basics
    def run(
        self,
        *args: str,
        input: Optional[bytes] = None,
        check: bool = True,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = 120,
    ) -> str:
        code, out, err = self.run_bytes(*args, input=input, env=env, timeout=timeout)
        if check and code != 0:
            raise GitError(f"git {' '.join(args)} failed: {err.decode('utf-8', 'replace').strip()}")
        return out.decode("utf-8", "replace")

    def run_bytes(
        self,
        *args: str,
        input: Optional[bytes] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = 120,
    ) -> Tuple[int, bytes, bytes]:
        proc = subprocess.run(
            [GIT, *args],
            cwd=self.root,
            input=input,
            capture_output=True,
            env=git_env(env),
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def ok(self, *args: str) -> bool:
        code, _, _ = self.run_bytes(*args)
        return code == 0

    @property
    def name(self) -> str:
        return os.path.basename(self.root)

    @property
    def git_dir(self) -> str:
        if self._git_dir is None:
            gd = self.run("rev-parse", "--git-dir").strip()
            self._git_dir = gd if os.path.isabs(gd) else os.path.join(self.root, gd)
        return self._git_dir

    def head(self) -> Optional[str]:
        code, out, _ = self.run_bytes("rev-parse", "--verify", "-q", "HEAD")
        return out.decode().strip() if code == 0 else None

    def branch(self) -> Optional[str]:
        code, out, _ = self.run_bytes("symbolic-ref", "--short", "-q", "HEAD")
        return out.decode().strip() if code == 0 else None

    def branch_display(self) -> str:
        b = self.branch()
        if b:
            return b
        h = self.head()
        return f"detached@{h[:7]}" if h else "(no commits)"

    def remote_url(self, remote: str = "origin") -> Optional[str]:
        """Configured URL as written (before insteadOf rewriting), so the GitHub slug is preserved."""
        code, out, _ = self.run_bytes("config", "--get", f"remote.{remote}.url")
        return out.decode().strip() if code == 0 and out.strip() else None

    def config_get(self, key: str) -> Optional[str]:
        code, out, _ = self.run_bytes("config", "--get", key)
        return out.decode().strip() if code == 0 else None

    def identity(self) -> Tuple[str, str]:
        """(name, email) in one process via `git var`, with a safe fallback."""
        code, out, _ = self.run_bytes("var", "GIT_AUTHOR_IDENT")
        text = out.decode("utf-8", "replace").strip()
        if code == 0 and "<" in text and ">" in text:
            name = text.split("<", 1)[0].strip()
            email = text.split("<", 1)[1].split(">", 1)[0].strip()
            if name and email:
                return name, email
        return "gitnolo", "gitnolo@localhost"

    @property
    def object_format(self) -> str:
        if self._object_format is None:
            out = self.run("rev-parse", "--show-object-format", check=False).strip()
            self._object_format = "sha256" if out == "sha256" else "sha1"
        return self._object_format

    def default_branch(self, remote: str = "origin") -> str:
        code, out, _ = self.run_bytes("symbolic-ref", "-q", "--short", f"refs/remotes/{remote}/HEAD")
        if code == 0 and out.strip():
            return out.decode().strip().split("/", 1)[-1]
        for cand in ("main", "master", "trunk", "develop"):
            if self.ok("show-ref", "--verify", "-q", f"refs/heads/{cand}"):
                return cand
        return self.branch() or "main"

    # ---------------------------------------------------------------- state
    def operation_in_progress(self) -> Optional[str]:
        gd = self.git_dir
        for marker, label in (
            ("MERGE_HEAD", "merge"),
            ("rebase-merge", "rebase"),
            ("rebase-apply", "rebase"),
            ("CHERRY_PICK_HEAD", "cherry-pick"),
            ("REVERT_HEAD", "revert"),
            ("BISECT_LOG", "bisect"),
        ):
            if os.path.exists(os.path.join(gd, marker)):
                return label
        return None

    def conflicted_files(self) -> List[str]:
        out = self.run("diff", "--name-only", "-z", "--diff-filter=U", check=False)
        return sorted({p for p in out.split("\0") if p})

    def changes(self, include_untracked: bool = True, head: Optional[str] = "") -> List[FileChange]:
        """Working tree (staged + unstaged + untracked) relative to HEAD."""
        result: Dict[str, FileChange] = {}
        if head == "":
            head = self.head()
        if head:
            out = self.run("diff", "--raw", "-z", "--no-renames", "HEAD", check=False)
            parts = out.split("\0")
            i = 0
            while i < len(parts) - 1:
                meta = parts[i]
                if not meta.startswith(":"):
                    i += 1
                    continue
                path = parts[i + 1]
                fields = meta[1:].split()
                old_mode, new_mode, status = fields[0], fields[1], fields[4][0]
                if old_mode == "160000" or new_mode == "160000":
                    i += 2
                    continue  # submodules are never auto-committed
                if status == "D":
                    kind = "delete"
                elif status == "A":
                    kind = "add"
                else:
                    kind = "modify"
                result[path] = FileChange(
                    path, kind, None if old_mode == "000000" else old_mode, None if new_mode == "000000" else new_mode
                )
                i += 2
        else:
            out = self.run("ls-files", "-z", "--cached", check=False)
            for p in out.split("\0"):
                if p:
                    result[p] = FileChange(p, "add", None, None)
        if include_untracked:
            out = self.run("ls-files", "-z", "--others", "--exclude-standard", check=False)
            for p in out.split("\0"):
                if p and p not in result:
                    if os.path.isdir(os.path.join(self.root, p)):
                        continue  # nested repository
                    result[p] = FileChange(p, "add", None, None)
        return [result[k] for k in sorted(result)]

    def has_changes(self) -> bool:
        out = self.run("status", "--porcelain=v1", "-z", "--untracked-files=normal", check=False)
        return bool(out.strip("\0"))

    def status_signature(self) -> str:
        """Cheap fingerprint of the dirty state, used to detect when edits settle."""
        out = self.run("status", "--porcelain=v1", "-z", "--untracked-files=all", check=False)
        sig = []
        for entry in out.split("\0"):
            if len(entry) < 4:
                continue
            p = os.path.join(self.root, entry[3:])
            try:
                st = os.lstat(p)
                sig.append(f"{entry}:{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                sig.append(entry)
        return "|".join(sig)

    def numstat(self) -> Dict[str, Tuple[int, int]]:
        stats: Dict[str, Tuple[int, int]] = {}
        if self.head():
            out = self.run("diff", "--numstat", "-z", "--no-renames", "HEAD", check=False)
            for rec in out.split("\0"):
                bits = rec.split("\t")
                if len(bits) == 3:
                    a, d, p = bits
                    stats[p] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)
        return stats

    def diff_text(self, max_chars: int = 12000) -> str:
        out = self.run("diff", "HEAD", "--stat", "--patch", "--no-color", check=False) if self.head() else ""
        untracked = self.run("ls-files", "--others", "--exclude-standard", check=False).strip()
        if untracked:
            out += "\n# New files:\n" + "\n".join(f"+ {p}" for p in untracked.splitlines())
        if len(out) > max_chars:
            out = out[:max_chars] + f"\n[... truncated to {max_chars} chars ...]"
        return out

    # --------------------------------------------------------------- objects
    def read_blobs(self, specs: Sequence[str]) -> Dict[str, Optional[bytes]]:
        """Reads many `<rev>:<path>` objects with a single cat-file process."""
        result: Dict[str, Optional[bytes]] = {}
        if not specs:
            return result
        proc = subprocess.Popen(
            [GIT, "cat-file", "--batch"],
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=git_env(),
        )
        assert proc.stdin and proc.stdout
        try:
            for spec in specs:
                if "\n" in spec:
                    result[spec] = None
                    continue
                proc.stdin.write(spec.encode() + b"\n")
                proc.stdin.flush()
                header = proc.stdout.readline().decode().strip()
                if header.endswith("missing") or header.endswith("ambiguous"):
                    result[spec] = None
                    continue
                size = int(header.split()[2])
                data = proc.stdout.read(size)
                proc.stdout.read(1)
                result[spec] = data
        finally:
            proc.stdin.close()
            proc.wait(timeout=30)
        return result

    def worktree_entry(self, path: str) -> Optional[Tuple[str, bytes]]:
        """Returns (mode, content) of a working tree file, or None if gone."""
        full = os.path.join(self.root, path)
        try:
            st = os.lstat(full)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return "120000", os.readlink(full).encode()
        if not stat.S_ISREG(st.st_mode):
            return None
        mode = "100755" if st.st_mode & 0o111 else "100644"
        with open(full, "rb") as f:
            return mode, f.read()

    # ------------------------------------------------------------- commits
    def fast_import(self, stream: bytes) -> Dict[str, str]:
        """Runs fast-import and returns its exported marks (":n" -> object id)."""
        import tempfile

        fd, marks = tempfile.mkstemp(prefix="gitnolo-marks-")
        os.close(fd)
        try:
            code, out, err = self.run_bytes(
                "fast-import", "--quiet", "--done", "--force", f"--export-marks={marks}", input=stream, timeout=600
            )
            if code != 0:
                raise GitError(f"fast-import failed: {err.decode('utf-8', 'replace').strip()}")
            result: Dict[str, str] = {}
            with open(marks, "r", encoding="utf-8") as f:
                for line in f:
                    k, _, v = line.strip().partition(" ")
                    if k and v:
                        result[k] = v
            return result
        finally:
            try:
                os.remove(marks)
            except OSError:
                pass

    def update_ref(self, ref: str, new: str, old: Optional[str] = None, msg: str = "gitnolo") -> None:
        args = ["update-ref", "-m", msg, ref, new]
        if old is not None:
            args.append(old)
        self.run(*args)

    def delete_ref(self, ref: str) -> None:
        self.run("update-ref", "-d", ref, check=False)

    def rev(self, spec: str) -> Optional[str]:
        code, out, _ = self.run_bytes("rev-parse", "--verify", "-q", spec + "^{commit}")
        return out.decode().strip() if code == 0 else None

    def sync_index(self, paths: Iterable[str], rev: str = "HEAD") -> None:
        """Make the index match `rev` for the given paths, leaving the working tree alone."""
        paths = list(paths)
        for i in range(0, len(paths), 500):
            chunk = paths[i : i + 500]
            self.run("reset", "-q", rev, "--", *chunk, check=False)

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)

    def push(self, src: str, dst_branch: str, remote: str = "origin", set_upstream: bool = False) -> str:
        args = ["push", "--porcelain"]
        if set_upstream:
            args.append("-u")
        args += [remote, f"{src}:refs/heads/{dst_branch}"]
        return self.run(*args, timeout=180)

    def fetch(self, remote: str = "origin", *refs: str) -> bool:
        code, _, _ = self.run_bytes("fetch", "--quiet", "--prune", remote, *refs, timeout=120)
        return code == 0

    def commit_count(self, rev_range: str) -> int:
        out = self.run("rev-list", "--count", rev_range, check=False).strip()
        return int(out) if out.isdigit() else 0

    def log_lines(self, rev_range: str, fmt: str = "%h %s", limit: int = 50) -> List[str]:
        out = self.run("log", f"--format={fmt}", f"-n{limit}", rev_range, check=False)
        return [l for l in out.splitlines() if l]


def find_root(path: str) -> Optional[str]:
    """Finds the git top-level without spawning a process (fast path), falling back to git."""
    cur = os.path.abspath(path)
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)
    probe = cur
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        proc = subprocess.run(
            [GIT, "rev-parse", "--show-toplevel"],
            cwd=cur if os.path.isdir(cur) else None,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def fi_path(path: str) -> bytes:
    """Encodes a path for fast-import, C-quoting only when required."""
    if path.startswith('"') or "\n" in path:
        escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'.encode()
    return path.encode()


def fi_data(payload: bytes) -> bytes:
    return b"data " + str(len(payload)).encode() + b"\n" + payload + b"\n"


def tz_offset() -> str:
    off = -time.altzone if time.localtime().tm_isdst > 0 else -time.timezone
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return f"{sign}{off // 3600:02d}{(off % 3600) // 60:02d}"
