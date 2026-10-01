"""
Micro-commit engine.

Splits the working tree's changes into N ordered commits (from whole-file
groups down to single changed lines) and writes them all with one
`git fast-import` process. Nothing in the working tree or the user's index is
touched while planning or writing, so agents can keep editing concurrently.

Every intermediate commit is a real snapshot: the final commit's tree is
verified to equal the working tree snapshot byte for byte.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import heapq
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import messages
from .gitcore import FileChange, GitError, Repo, fi_data, fi_path, tz_offset

SENSITIVE_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    "*.keystore", "*.jks", "credentials.json", "service-account*.json", ".npmrc", ".pypirc",
    "*.sqlite", "*.sqlite3", ".netrc",
]
SENSITIVE_ALLOW = {".env.example", ".env.sample", ".env.template", ".env.dist"}
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_SPLIT_BYTES = 1024 * 1024  # files larger than this are committed whole

WORK_REF = "refs/gitnolo/work"


def is_sensitive(path: str) -> bool:
    base = os.path.basename(path)
    if base in SENSITIVE_ALLOW:
        return False
    return any(fnmatch.fnmatch(base, pat) for pat in SENSITIVE_PATTERNS)


@dataclass
class Op:
    i1: int
    i2: int
    j1: int
    j2: int
    pieces: int = 1

    @property
    def capacity(self) -> int:
        return max(1, self.i2 - self.i1, self.j2 - self.j1)

    def cut(self, p: int) -> Tuple[int, int]:
        """Old/new line offsets after applying `p` of `pieces`."""
        if p >= self.pieces:
            return self.i2, self.j2
        ci = self.i1 + (p * (self.i2 - self.i1)) // self.pieces
        cj = self.j1 + (p * (self.j2 - self.j1)) // self.pieces
        return ci, cj


@dataclass
class FilePlan:
    change: FileChange
    old: Optional[bytes]
    new: Optional[bytes]
    old_mode: Optional[str]
    new_mode: Optional[str]
    text: bool = False
    a: List[str] = field(default_factory=list)
    b: List[str] = field(default_factory=list)
    ops: List[Op] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.change.path

    @property
    def splittable(self) -> bool:
        return self.text and bool(self.ops)

    def unit_count(self) -> int:
        return len(self.ops) if self.splittable else 1

    def content_at(self, progress: Sequence[int]) -> Optional[bytes]:
        """File content after applying progress[k] pieces of each op."""
        if not self.splittable:
            return self.new if progress and progress[0] >= 1 else self.old
        out: List[str] = []
        pos = 0
        for op, p in zip(self.ops, progress):
            out.extend(self.a[pos : op.i1])
            if p <= 0:
                out.extend(self.a[op.i1 : op.i2])
            elif p >= op.pieces:
                out.extend(self.b[op.j1 : op.j2])
            else:
                ci, cj = op.cut(p)
                out.extend(self.b[op.j1 : cj])
                out.extend(self.a[ci : op.i2])
            pos = op.i2
        out.extend(self.a[pos:])
        if not out and self.change.kind == "delete":
            return None
        return "".join(out).encode("utf-8")


@dataclass
class Atom:
    file: int
    op: int  # -1 for whole-file atoms
    piece: int  # 1-based piece index


@dataclass
class PlannedCommit:
    subject: str
    atoms: List[Atom]
    paths: List[str]


@dataclass
class Plan:
    repo: Repo
    base: Optional[str]
    files: List[FilePlan]
    commits: List[PlannedCommit]
    skipped: List[Tuple[str, str]]
    max_possible: int

    @property
    def paths(self) -> List[str]:
        return [f.path for f in self.files]

    def stats(self) -> Tuple[int, int]:
        add = rem = 0
        for f in self.files:
            if f.splittable:
                for op in f.ops:
                    add += op.j2 - op.j1
                    rem += op.i2 - op.i1
            elif f.text:
                add += len(f.b)
                rem += len(f.a)
        return add, rem


@dataclass
class CommitResult:
    base: Optional[str]
    tip: str
    count: int
    subjects: List[str]
    paths: List[str]
    elapsed: float


def _decode(data: Optional[bytes]) -> Optional[List[str]]:
    if data is None:
        return []
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def collect(
    repo: Repo, paths: Optional[Sequence[str]] = None, base: Optional[str] = ""
) -> Tuple[Optional[str], List[FilePlan], List[Tuple[str, str]]]:
    """Snapshots `base` (default HEAD) and working-tree contents for all changed files."""
    if base == "":
        base = repo.head()
    changes = repo.changes(head=base)
    if paths is not None:
        wanted = set(paths)
        changes = [c for c in changes if c.path in wanted]
    skipped: List[Tuple[str, str]] = []
    old_blobs = repo.read_blobs([f"{base}:{c.path}" for c in changes]) if base else {}
    old_modes: Dict[str, str] = {c.path: c.old_mode for c in changes if c.old_mode}
    missing_modes = [c.path for c in changes if c.path not in old_modes and old_blobs.get(f"{base}:{c.path}") is not None]
    if base and missing_modes:
        out = repo.run("ls-tree", "-z", base, "--", *missing_modes, check=False)
        for rec in out.split("\0"):
            if "\t" in rec:
                meta, p = rec.split("\t", 1)
                old_modes[p] = meta.split()[0]

    plans: List[FilePlan] = []
    for c in changes:
        if is_sensitive(c.path) and c.kind != "delete":
            skipped.append((c.path, "looks like a secret; commit it manually if intended"))
            continue
        old = old_blobs.get(f"{base}:{c.path}") if base else None
        entry = repo.worktree_entry(c.path)
        if entry is None:
            if old is None:
                continue
            new_mode, new = None, None
            kind = "delete"
        else:
            new_mode, new = entry
            kind = "add" if old is None else "modify"
            if len(new) > MAX_FILE_BYTES:
                skipped.append((c.path, f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"))
                continue
        old_mode = old_modes.get(c.path)
        if old == new and old_mode == new_mode:
            continue
        fp = FilePlan(FileChange(c.path, kind), old, new, old_mode, new_mode)
        a = _decode(old)
        b = _decode(new)
        both_regular = (old_mode in (None, "100644", "100755")) and (new_mode in (None, "100644", "100755"))
        if a is not None and b is not None and both_regular and max(len(old or b""), len(new or b"")) <= MAX_SPLIT_BYTES:
            fp.text = True
            fp.a, fp.b = a, b
            sm = difflib.SequenceMatcher(None, a, b, autojunk=len(a) + len(b) > 20000)
            fp.ops = [Op(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
            if not fp.ops and old_mode == new_mode:
                continue
        plans.append(fp)

    plans.sort(key=_order_key)
    return base, plans, skipped


def _order_key(fp: FilePlan) -> Tuple[int, str]:
    ctype = messages.commit_type_for_path(fp.path)
    rank = {"build": 0, "chore": 1, None: 2, "style": 3, "test": 4, "ci": 5, "docs": 6}.get(ctype, 2)
    return rank, fp.path


def _distribute_pieces(files: List[FilePlan], extra: int) -> None:
    """Splits the largest hunks first until `extra` additional commits are allocated."""
    heap: List[Tuple[float, int, int]] = []
    for fi, f in enumerate(files):
        if not f.splittable:
            continue
        for oi, op in enumerate(f.ops):
            if op.capacity > op.pieces:
                heapq.heappush(heap, (-op.capacity / op.pieces, fi, oi))
    while extra > 0 and heap:
        _, fi, oi = heapq.heappop(heap)
        op = files[fi].ops[oi]
        op.pieces += 1
        extra -= 1
        if op.capacity > op.pieces:
            heapq.heappush(heap, (-op.capacity / op.pieces, fi, oi))


def _partition(sizes: List[int], target: int) -> List[List[Tuple[int, int, int]]]:
    """
    Partitions per-file atom counts into `target` contiguous groups.
    Returns groups as lists of (file_index, start_atom, end_atom).
    Prefers file boundaries; never splits more than needed.
    """
    nfiles = len(sizes)
    groups: List[List[Tuple[int, int, int]]] = []
    if nfiles >= target:
        # Pack whole files into `target` groups, balancing by atom size.
        start = 0
        for g in range(target):
            end = ((g + 1) * nfiles) // target
            groups.append([(fi, 0, sizes[fi]) for fi in range(start, end)])
            start = end
        return [g for g in groups if g]
    # Each file gets >= 1 commit; spread the rest proportionally (largest remainder).
    alloc = [1] * nfiles
    remaining = target - nfiles
    total = sum(sizes)
    if remaining > 0 and total > 0:
        quotas = [(s / total) * remaining for s in sizes]
        for fi, q in enumerate(quotas):
            add = min(int(q), sizes[fi] - alloc[fi])
            alloc[fi] += add
            remaining -= add
        order = sorted(range(nfiles), key=lambda k: quotas[k] - int(quotas[k]), reverse=True)
        while remaining > 0:
            progressed = False
            for fi in order:
                if remaining and alloc[fi] < sizes[fi]:
                    alloc[fi] += 1
                    remaining -= 1
                    progressed = True
            if not progressed:
                break
    for fi, (size, k) in enumerate(zip(sizes, alloc)):
        k = max(1, min(k, size))
        for g in range(k):
            s = (g * size) // k
            e = ((g + 1) * size) // k
            if e > s:
                groups.append([(fi, s, e)])
    return groups


def plan(
    repo: Repo,
    target: int,
    paths: Optional[Sequence[str]] = None,
    message_hook: Optional[Callable[[FilePlan, List[str], List[str]], Optional[str]]] = None,
    base: Optional[str] = "",
) -> Plan:
    """
    Builds a commit plan with (up to) `target` commits on top of `base` (default HEAD).
    target <= 0 means one commit per natural hunk.
    """
    base, files, skipped = collect(repo, paths, base)
    natural = sum(f.unit_count() for f in files)
    max_possible = sum(sum(op.capacity for op in f.ops) if f.splittable else 1 for f in files)
    if target <= 0:
        target = natural
    target = max(1, min(target, max_possible)) if files else 0

    if target > natural:
        _distribute_pieces(files, target - natural)

    atoms_per_file: List[List[Atom]] = []
    for fi, f in enumerate(files):
        if f.splittable:
            atoms_per_file.append([Atom(fi, oi, p) for oi, op in enumerate(f.ops) for p in range(1, op.pieces + 1)])
        else:
            atoms_per_file.append([Atom(fi, -1, 1)])

    commits: List[PlannedCommit] = []
    if files:
        groups = _partition([len(a) for a in atoms_per_file], target)
        for group in groups:
            atoms: List[Atom] = []
            for fi, s, e in group:
                atoms.extend(atoms_per_file[fi][s:e])
            paths_in = sorted({files[a.file].path for a in atoms}, key=lambda p: [f.path for f in files].index(p))
            commits.append(PlannedCommit("", atoms, paths_in))
        _label(files, commits, message_hook)

    return Plan(repo, base, files, commits, skipped, max_possible)


def _atom_lines(f: FilePlan, atom: Atom) -> Tuple[List[str], List[str], int, int]:
    if atom.op < 0:
        return (f.b if f.text else []), (f.a if f.text else []), 0, 0
    op = f.ops[atom.op]
    i0, j0 = op.cut(atom.piece - 1)
    i1, j1 = op.cut(atom.piece)
    return f.b[j0:j1], f.a[i0:i1], i0, j0


def _label(
    files: List[FilePlan],
    commits: List[PlannedCommit],
    message_hook: Optional[Callable[[FilePlan, List[str], List[str]], Optional[str]]],
) -> None:
    per_file_total: Dict[int, int] = {}
    for c in commits:
        if len(c.paths) == 1:
            fi = c.atoms[0].file
            per_file_total[fi] = per_file_total.get(fi, 0) + 1
    per_file_seen: Dict[int, int] = {}
    used: Dict[str, int] = {}

    for c in commits:
        if len(c.paths) > 1:
            subject = messages.summarize_multi(c.paths)
        else:
            fi = c.atoms[0].file
            f = files[fi]
            per_file_seen[fi] = per_file_seen.get(fi, 0) + 1
            added: List[str] = []
            removed: List[str] = []
            first_old = first_new = None
            for a in c.atoms:
                ad, rm, oi, nj = _atom_lines(f, a)
                added.extend(ad)
                removed.extend(rm)
                if first_old is None:
                    first_old, first_new = oi, nj
            subject = None
            if message_hook:
                subject = message_hook(f, added, removed)
            if not subject:
                if f.text and f.a:
                    ctx = messages.enclosing_symbol(f.a, first_old or 0)
                elif f.text and f.b and first_new:
                    ctx = messages.enclosing_symbol(f.b, first_new - 1)
                else:
                    ctx = None
                total = per_file_total.get(fi, 1)
                kind = f.change.kind
                subject = messages.build_subject(
                    f.path,
                    kind,
                    added,
                    removed,
                    context_symbol=ctx,
                    part=per_file_seen[fi] if total > 1 else None,
                    parts=total if total > 1 else None,
                )
        n = used.get(subject, 0)
        used[subject] = n + 1
        c.subject = subject if n == 0 else f"{subject} [{n + 1}]"


def _blob_id(data: bytes, algo: str) -> str:
    h = hashlib.new(algo)
    h.update(b"blob " + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def execute(
    p: Plan,
    ref: str = WORK_REF,
    body: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> CommitResult:
    """Writes the planned commits to `ref` with one fast-import stream and verifies the result."""
    if not p.commits:
        raise GitError("Nothing to commit")
    repo = p.repo
    started = time.time()
    name, email = repo.identity()
    when = f"{int(time.time())} {tz_offset()}".encode()
    ident = f"{name} <{email}> ".encode() + when

    progress: Dict[int, List[int]] = {fi: [0] * max(1, len(f.ops)) for fi, f in enumerate(p.files)}
    last_written: Dict[int, Tuple[Optional[str], Optional[bytes]]] = {}

    chunks: List[bytes] = [b"feature done\n", f"reset {ref}\n".encode()]
    if p.base:
        chunks.append(f"from {p.base}\n".encode())
    chunks.append(b"\n")
    total = len(p.commits)
    for n, c in enumerate(p.commits, 1):
        touched: List[int] = []
        for a in c.atoms:
            if a.op < 0:
                progress[a.file][0] = 1
            else:
                progress[a.file][a.op] = max(progress[a.file][a.op], a.piece)
            if a.file not in touched:
                touched.append(a.file)
        msg = c.subject + "\n"
        if body:
            msg += "\n" + body.strip() + "\n"
        chunks.append(f"commit {ref}\nmark :{n}\n".encode())
        chunks.append(b"author " + ident + b"\n")
        chunks.append(b"committer " + ident + b"\n")
        chunks.append(fi_data(msg.encode("utf-8")))
        for fi in touched:
            f = p.files[fi]
            done = all(
                pr >= (op.pieces if f.splittable else 1)
                for pr, op in zip(progress[fi], f.ops if f.splittable else [Op(0, 0, 0, 0)])
            )
            if done:
                content = f.new
                mode = f.new_mode
            else:
                content = f.content_at(progress[fi])
                mode = f.old_mode or f.new_mode
            if content is None or (done and f.change.kind == "delete"):
                chunks.append(b"D " + fi_path(f.path) + b"\n")
                last_written[fi] = (None, None)
            else:
                mode = mode or "100644"
                chunks.append(f"M {mode} inline ".encode() + fi_path(f.path) + b"\n")
                chunks.append(fi_data(content))
                last_written[fi] = (mode, content)
        chunks.append(b"\n")
        if on_progress:
            on_progress(n, total)
    chunks.append(b"done\n")
    marks = repo.fast_import(b"".join(chunks))
    tip = marks.get(f":{total}")
    if not tip:
        raise GitError("fast-import did not produce a commit")
    _verify(p, tip)
    return CommitResult(
        base=p.base,
        tip=tip,
        count=total,
        subjects=[c.subject for c in p.commits],
        paths=p.paths,
        elapsed=time.time() - started,
    )


def _verify(p: Plan, tip: str) -> None:
    """Ensures the final tree equals the snapshot: no line lost, none invented."""
    repo = p.repo
    algo = repo.object_format
    out = repo.run("ls-tree", "-r", "-z", tip, "--", *p.paths, check=False) if p.paths else ""
    tree: Dict[str, Tuple[str, str]] = {}
    for rec in out.split("\0"):
        if "\t" in rec:
            meta, path = rec.split("\t", 1)
            mode, _, oid = meta.split()
            tree[path] = (mode, oid)
    for f in p.files:
        if f.new is None:
            if f.path in tree:
                raise GitError(f"verification failed: {f.path} should be deleted")
            continue
        got = tree.get(f.path)
        if not got or got[1] != _blob_id(f.new, algo):
            raise GitError(f"verification failed: {f.path} content mismatch")
