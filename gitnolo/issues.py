"""
Issue extraction from an agent's final message.

Heuristics find lines that describe unresolved problems (failing tests, TODOs,
known limitations, errors the agent could not fix). The local model, when
available, may only *filter and retitle* those candidates, never invent new
ones, which keeps hallucinated issues out of the tracker.
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "hashlib", "re",
]

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

STRONG_RE = re.compile(
    r"\b(untested|not (?:been )?tested|couldn'?t|could not|unable to|can'?t (?:get around|fix|reproduce|resolve)|"
    r"not (?:yet )?implemented|unimplemented|todo|fixme|known (?:issue|limitation|problem|bug)s?|"
    r"still (?:fails?|failing|broken|missing|needs?|returns|loads|uses|depends|references|contains|has|requires|crash\w*)|"
    r"(?:is|are) (?:still )?failing|fails (?:when|on|with|to|if)|keeps? failing|"
    r"returns? (?:a |an |the )?(?:404|500|5\d\d|error|wrong|blank|empty|your main)|"
    r"second problem|another (?:problem|issue|bug)|bug remains|regression|security (?:risk|issue|hole)|"
    r"vulnerab\w*|insecure|memory leak|race condition|deprecated|stubbed|hard-?coded (?:key|secret|token|password))\b",
    re.I,
)
WEAK_RE = re.compile(
    r"\b(errors?|warnings?|missing|limitations?|follow[- ]?ups?|needs? (?:manual|further|more)|requires? manual|"
    r"worth (?:checking|testing|verifying)|manual (?:confirmation|verification|step|testing)|placeholder|bug|broken|fail\w*|crash\w*|flaky|timeouts?)\b",
    re.I,
)
RESOLVED_RE = re.compile(
    r"\b(is gone|no longer|fixed|resolved|addressed|passes|passed|all (?:\d+ )?tests? pass|successfully|"
    r"works correctly|works now|now works|with no [\w\s]{0,30}errors?|no (?:console )?(?:errors?|warnings?|issues?|failures?)|"
    r"zero (?:errors|warnings)|without (?:errors|issues)|nothing else|0 (?:errors|failures))\b",
    re.I,
)
NOW_RE = re.compile(r"\bnow\b", re.I)
STILL_RE = re.compile(r"\bstill\b", re.I)
PAST_PREFIX_RE = re.compile(
    r"^(?:cause|root cause|why[^:]{0,40}|what (?:it|i) did|fix|fixed|before|after|change[sd]?|added|updated|removed|result|done)\s*:",
    re.I,
)
META_RE = re.compile(
    r"\b(bash|edit calls?|tool calls?|terminal|anthropic|claude code|feedback|draft(?:ed)? (?:a )?note|"
    r"my (?:tools|environment|session)|permission prompts?|the harness|this session)\b",
    re.I,
)
HEADING_RE = re.compile(
    r"^\s*(?:#+\s*|\*\*)?(known issues?|issues?|limitations?|caveats?|todo|to-?do|next steps?|follow[- ]ups?|"
    r"remaining (?:work|issues|items)|open (?:questions|issues)|not (?:yet )?(?:done|implemented)|blockers?|warnings?|"
    r"untested|still to do)\b",
    re.I,
)
BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
CODE_FENCE_RE = re.compile(r"```.*?```", re.S)

BUG_WORDS = re.compile(r"\b(fail\w*|broken|bug|error|exception|crash\w*|regression|flaky|leak\w*|timeout|vulnerab\w*)\b", re.I)


@dataclass
class IssueDraft:
    title: str
    body: str
    labels: List[str] = field(default_factory=list)
    source_line: str = ""

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.title)


def _clean(line: str) -> str:
    line = BULLET_RE.sub("", line)
    line = re.sub(r"\*\*|__|`", "", line)
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
    line = re.sub(r"\s+", " ", line).strip(" :-–—")
    return line


def _title(line: str) -> str:
    t = _clean(line)
    t = re.sub(r"^(?:note|warning|caveat|todo|fixme|issue|known issue|limitation)s?\s*[:\-]\s*", "", t, flags=re.I)
    t = t[:1].upper() + t[1:]
    if len(t) > 80:
        cut = t[:80]
        t = cut[: cut.rfind(" ")] if " " in cut[40:] else cut
        t = t.rstrip(",;:") + "…"
    return t.rstrip(".")


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def fingerprint(title: str) -> str:
    return hashlib.sha1(" ".join(sorted(_words(title))).encode()).hexdigest()[:16]


def similar(a: str, b: str, threshold: float = 0.6) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= threshold


def candidates(message: str) -> List[str]:
    """Problem-describing lines, in order, de-duplicated."""
    text = CODE_FENCE_RE.sub("", message or "")
    out: List[str] = []
    under_heading = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if HEADING_RE.match(line) and len(_clean(line)) < 40:
            under_heading = True
            continue
        if re.match(r"^\s*#+\s", line) or (line.strip().startswith("**") and line.strip().endswith("**")):
            under_heading = False
            continue
        cleaned = _clean(line)
        if len(cleaned) < 12 or len(cleaned) > 400:
            continue
        if score(cleaned, under_heading and bool(BULLET_RE.match(line))) >= 2 and cleaned not in out:
            out.append(cleaned)
    return out


def score(line: str, under_problem_heading: bool = False) -> int:
    """Precision-first score: >= 2 means the line describes an unresolved problem."""
    if META_RE.search(line) or PAST_PREFIX_RE.match(line):
        return -10
    s = 2 if under_problem_heading else 0
    if STRONG_RE.search(line):
        s += 2
    if WEAK_RE.search(line):
        s += 1
    if RESOLVED_RE.search(line):
        s -= 3
    if NOW_RE.search(line) and not STILL_RE.search(line):
        s -= 2
    return s


def extract(
    message: str,
    repo_name: str = "",
    agent: str = "",
    session_title: str = "",
    llm: Optional[Any] = None,
    limit: int = 5,
) -> List[IssueDraft]:
    cands = candidates(message)
    if not cands:
        return []
    chosen: List[Dict[str, Any]] = [{"index": i, "title": _title(c)} for i, c in enumerate(cands)]
    if llm is not None:
        try:
            refined = llm.refine_issues(message, cands)
            if refined is not None:
                chosen = [
                    {"index": r["index"], "title": str(r.get("title") or _title(cands[r["index"]]))[:90], "type": r.get("type")}
                    for r in refined
                    if 0 <= r["index"] < len(cands)
                ]
        except Exception:
            pass

    drafts: List[IssueDraft] = []
    for item in chosen:
        line = cands[item["index"]]
        title = item["title"].strip() or _title(line)
        if any(similar(title, d.title) for d in drafts):
            continue
        kind = item.get("type")
        labels = ["agent-reported"]
        if kind == "bug" or (kind is None and BUG_WORDS.search(line)):
            labels.append("bug")
        elif kind == "enhancement":
            labels.append("enhancement")
        context = _context(message, line)
        body = (
            f"Reported by **{agent or 'coding agent'}** after finishing a task"
            + (f" (\"{session_title}\")" if session_title else "")
            + (f" in `{repo_name}`" if repo_name else "")
            + ".\n\n"
            f"> {line}\n\n"
            + (f"<details><summary>Context from the agent's summary</summary>\n\n{context}\n\n</details>\n\n" if context else "")
            + "_Opened automatically by gitnolo._"
        )
        drafts.append(IssueDraft(title=title, body=body, labels=labels, source_line=line))
        if len(drafts) >= limit:
            break
    return drafts


def _context(message: str, line: str, radius: int = 3) -> str:
    lines = message.splitlines()
    for i, l in enumerate(lines):
        if line[:40] in _clean(l):
            return "\n".join(lines[max(0, i - radius) : i + radius + 1]).strip()
    return ""


# ---------------------------------------------------------------- live mode
# Messages streamed mid-turn are full of working notes ("let me check why the
# test fails"). Those describe what the agent is about to do, not a problem it
# is reporting, so live extraction drops them.
TRANSIENT_RE = re.compile(
    r"\b(let me|let's|i'll|i will|i'm going to|i am going to|going to (?:check|look|try|fix|investigate)|"
    r"next,? i|now i|first,? i|i need to (?:check|look|see|find|investigate|read)|i should (?:check|look)|"
    r"checking|looking (?:at|into)|investigating)\b",
    re.I,
)
ISSUE_REF_RE = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)\b", re.I)
STOP_WORDS = {
    "the", "and", "for", "with", "when", "that", "this", "from", "into", "now", "still", "but", "are", "was", "were",
    "has", "have", "had", "not", "its", "it's", "our", "your", "their", "there", "then", "than", "also", "just",
    "all", "any", "some", "both", "each", "more", "less", "after", "before", "because", "which", "while", "only",
    "fixed", "fix", "fixes", "fixing", "resolved", "resolve", "addressed", "works", "work", "working", "passes",
    "pass", "passed", "passing", "fails", "fail", "failed", "failing", "failure", "error", "errors", "issue", "issues",
    "bug", "bugs", "problem", "problems", "broken", "gone", "longer", "correctly", "properly", "successfully",
    "again", "should", "would", "could", "can", "cannot", "couldn", "does", "doesn", "did", "didn", "done",
    "returns", "return", "returned", "todo", "known", "remains", "remaining", "untested", "tested", "test", "tests",
    "missing", "added", "add", "updated", "update", "handle", "handles", "handled", "case", "cases", "now",
}


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s", "ly"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    return w[:7]


def keywords(text: str) -> set:
    """Topic words of a line: no stop words, no problem/resolution verbs, crudely stemmed."""
    words = re.findall(r"[a-z0-9][a-z0-9_.-]*[a-z0-9]|[a-z0-9]{2,}", (text or "").lower())
    return {_stem(w) for w in words if len(w) > 2 and w not in STOP_WORDS}


def live_candidates(message: str) -> List[str]:
    """Problem lines from any agent message, including mid-turn ones."""
    out = []
    for c in candidates(message):
        if TRANSIENT_RE.search(c) or c.rstrip().endswith("?"):
            continue
        out.append(c)
    return out


def resolution_lines(message: str) -> List[str]:
    """Lines where the agent states that something is now fixed or working."""
    text = CODE_FENCE_RE.sub("", message or "")
    out = []
    for raw in text.splitlines():
        line = _clean(raw)
        if len(line) < 8 or len(line) > 400 or META_RE.search(line) or TRANSIENT_RE.search(line):
            continue
        fixed = RESOLVED_RE.search(line) or ISSUE_REF_RE.search(line) or re.match(r"^(?:fix(?:ed)?|resolved)\s*:", line, re.I)
        if fixed and score(line) < 2:
            out.append(line)
    return out


def referenced_numbers(line: str) -> List[int]:
    return [int(n) for n in ISSUE_REF_RE.findall(line or "")]


def resolves(line: str, issue_title: str, issue_source: str = "") -> bool:
    """True when a resolution line is about the same topic as an open issue."""
    a = keywords(line)
    b = keywords(issue_title) | keywords(issue_source)
    if not a or not b:
        return False
    shared = a & b
    return len(shared) >= 2 and len(shared) / min(len(a), len(b)) >= 0.5


def draft_for(line: str, repo_name: str = "", agent: str = "", session_title: str = "", live: bool = False) -> IssueDraft:
    labels = ["agent-reported"] + (["bug"] if BUG_WORDS.search(line) else [])
    when = "while working" if live else "after finishing a task"
    body = (
        f"Reported by **{agent or 'coding agent'}** {when}"
        + (f" (\"{session_title}\")" if session_title else "")
        + (f" in `{repo_name}`" if repo_name else "")
        + ".\n\n"
        f"> {line}\n\n"
        "_Opened automatically by gitnolo; it will be closed automatically when the agent reports it fixed._"
    )
    return IssueDraft(title=_title(line), body=body, labels=labels, source_line=line)
