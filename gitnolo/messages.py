"""
Instant, deterministic Conventional Commit messages.

Micro-commit runs can produce hundreds of commits; calling an LLM per commit
would take minutes. These heuristics read the actual added/removed lines and
name the symbols touched, so each message is specific without any model call.
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "re",
]

import os
import re
from typing import Iterable, List, Optional, Sequence

MAX_SUBJECT = 72

SYMBOL_PATTERNS = [
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*class\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait|impl)\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:public|private|protected|internal|open|static|override|\s)*\s*(?:fun|func)\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:public|private|protected|internal|final|abstract|sealed|data|\s)*(?:class|struct|protocol|interface|object)\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?!return\b|new\b|await\b|throw\b|else\b)(?:public|private|protected|static|final|async|\s)+[\w<>\[\],\s]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?\s*$"),
    re.compile(r"^\s*([A-Za-z_][\w-]*)\s*:\s*$"),  # yaml-ish top-level keys
]

IMPORT_RE = re.compile(r"^\s*(?:import\s|from\s+\S+\s+import\s|#include\s|use\s+\w|require\(|const\s+\w+\s*=\s*require\()")
COMMENT_RE = re.compile(r"^\s*(?:#|//|/\*|\*|<!--|--|;)")
FIX_RE = re.compile(r"\b(?:fix|bug|error|exception|except|catch|raise|throw|guard|null|undefined|none\b|retry|fallback|validate|sanitize)", re.I)
TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$")

DOC_EXT = {".md", ".mdx", ".rst", ".txt", ".adoc"}
STYLE_EXT = {".css", ".scss", ".sass", ".less", ".styl"}
BUILD_FILES = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "pipfile", "pipfile.lock",
    "cargo.toml", "cargo.lock", "go.mod", "go.sum", "gemfile", "gemfile.lock",
    "makefile", "dockerfile", "docker-compose.yml", "docker-compose.yaml", "build.gradle",
    "build.gradle.kts", "pom.xml", "podfile", "podfile.lock", "package.swift", "cmakelists.txt",
}
CONFIG_EXT = {".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".plist", ".xml", ".env"}


def commit_type_for_path(path: str) -> Optional[str]:
    low = path.lower()
    base = os.path.basename(low)
    ext = os.path.splitext(base)[1]
    if low.startswith(".github/workflows/") or base in {".gitlab-ci.yml", ".travis.yml", "jenkinsfile"}:
        return "ci"
    if TEST_PATH_RE.search(low):
        return "test"
    if base in BUILD_FILES or base.startswith("requirements") and ext == ".txt":
        return "build"
    if ext in DOC_EXT or low.startswith("docs/") or base in {"license", "changelog", "authors"}:
        return "docs"
    if ext in STYLE_EXT:
        return "style"
    if ext in CONFIG_EXT or base.startswith("."):
        return "chore"
    return None


def scope_for_path(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    stem = os.path.splitext(parts[-1])[0].lstrip(".")
    if stem.lower() in {"index", "main", "__init__", "mod", "lib", "app", "init"} and len(parts) > 1:
        stem = parts[-2]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-").lower()
    return stem[:24] or "repo"


def find_symbols(lines: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for line in lines:
        for pat in SYMBOL_PATTERNS:
            m = pat.match(line)
            if m:
                name = m.group(1)
                if name and name not in seen and name not in {"if", "for", "while", "switch", "return", "else"}:
                    seen.append(name)
                break
    return seen


def enclosing_symbol(lines: Sequence[str], index: int) -> Optional[str]:
    """Nearest definition at or above `index` (cheap scope detection)."""
    for k in range(min(index, len(lines) - 1), max(-1, index - 400), -1):
        syms = find_symbols([lines[k]])
        if syms:
            return syms[0]
    return None


def _fmt_names(names: Sequence[str], limit: int = 2) -> str:
    shown = [n if n[:1].isupper() else f"{n}()" for n in names[:limit]]
    text = ", ".join(shown)
    if len(names) > limit:
        text += f" and {len(names) - limit} more"
    return text


def _significant(lines: Iterable[str]) -> List[str]:
    return [l for l in lines if l.strip()]


def classify_lines(added: Sequence[str], removed: Sequence[str]) -> str:
    sig = _significant(added) + _significant(removed)
    if not sig:
        return "whitespace"
    if all(IMPORT_RE.match(l) for l in sig):
        return "imports"
    if all(COMMENT_RE.match(l) for l in sig):
        return "comments"
    return "code"


def build_subject(
    path: str,
    kind: str,
    added: Sequence[str],
    removed: Sequence[str],
    context_symbol: Optional[str] = None,
    part: Optional[int] = None,
    parts: Optional[int] = None,
) -> str:
    """Builds one conventional commit subject for a change to a single file."""
    ctype = commit_type_for_path(path)
    scope = scope_for_path(path)
    base = os.path.basename(path)
    added_syms = find_symbols(added)
    removed_syms = [s for s in find_symbols(removed) if s not in added_syms]
    nature = classify_lines(added, removed)
    suffix = f" ({part}/{parts})" if part and parts and parts > 1 else ""

    if kind == "delete" and (parts is None or parts <= 1 or part == parts):
        desc = f"remove {base}"
        ctype = ctype or "refactor"
    elif kind == "add" and not removed:
        if added_syms:
            desc = f"add {_fmt_names(added_syms)}"
        elif context_symbol and part and part > 1:
            desc = f"complete {_fmt_names([context_symbol], 1)}"
        else:
            desc = f"add {base}"
        ctype = ctype or "feat"
    elif nature == "imports":
        desc = "update imports"
        ctype = ctype or "refactor"
    elif nature == "comments":
        desc = f"update comments in {base}" if not context_symbol else f"document {_fmt_names([context_symbol], 1)}"
        ctype = ctype if ctype in ("test", "docs") else "docs"
    elif nature == "whitespace":
        desc = f"tidy formatting in {base}"
        ctype = "style"
    elif added_syms:
        desc = f"add {_fmt_names(added_syms)}"
        ctype = ctype or "feat"
    elif removed_syms and not _significant(added):
        desc = f"remove {_fmt_names(removed_syms)}"
        ctype = ctype or "refactor"
    elif removed_syms:
        desc = f"rework {_fmt_names(removed_syms)}"
        ctype = ctype or "refactor"
    else:
        target = _fmt_names([context_symbol], 1) if context_symbol else base
        if not _significant(added):
            desc = f"trim {target}"
            ctype = ctype or "refactor"
        elif any(FIX_RE.search(l) for l in added):
            desc = f"harden {target}"
            ctype = ctype or "fix"
        else:
            desc = f"update {target}"
            ctype = ctype or "refactor"

    head = f"{ctype}({scope}): " if scope else f"{ctype}: "
    room = MAX_SUBJECT - len(head) - len(suffix)
    if len(desc) > room:
        desc = desc[: max(8, room - 1)].rstrip() + "…"
    return head + desc + suffix


def summarize_multi(paths: Sequence[str]) -> str:
    """Subject for a commit that spans several files."""
    types = [commit_type_for_path(p) or "feat" for p in paths]
    ctype = max(set(types), key=types.count)
    dirs = {os.path.dirname(p) for p in paths}
    only = next(iter(dirs)) if len(dirs) == 1 else ""
    scope = re.sub(r"[^A-Za-z0-9_-]+", "-", os.path.basename(only)).strip("-").lower()[:24] if only else ""
    names = [os.path.basename(p) for p in paths]
    desc = f"update {', '.join(names[:2])}"
    if len(names) > 2:
        desc += f" and {len(names) - 2} more"
    head = f"{ctype}({scope}): " if scope else f"{ctype}: "
    subject = head + desc
    return subject if len(subject) <= MAX_SUBJECT else subject[: MAX_SUBJECT - 1] + "…"
