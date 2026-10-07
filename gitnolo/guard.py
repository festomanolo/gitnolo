"""
Guardrails for agent-written changes.

  * Secrets: a file whose new content introduces an API key, token or private
    key is never auto-committed (it would be pushed within seconds).
  * Test tampering: agents under pressure delete or skip failing tests, or drop
    assertions, to make a suite pass. Such changes are still committed and
    pushed, but the PR is left open for a human instead of being auto-merged.
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "re",
]

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from .microcommit import Plan

SECRET_PATTERNS = [
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("Anthropic API key", re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{30,}")),
    ("OpenAI-style API key", re.compile(rb"\bsk-(?:proj-|or-v1-)?[A-Za-z0-9_-]{32,}")),
    ("Slack token", re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{20,}")),
    ("Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe secret key", re.compile(rb"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----")),
]

TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|specs)/|(?:^|/)test_[^/]+\.py$|_test\.(?:py|go|rb|exs?)$|"
    r"\.(?:test|spec)\.[cm]?[jt]sx?$|Tests?\.(?:java|kt|cs|swift)$|_spec\.rb$",
    re.I,
)
SKIP_RE = re.compile(
    r"\b(?:it|test|describe|context)\.(?:skip|todo)\s*\(|\b(?:xit|xtest|xdescribe)\s*\(|@pytest\.mark\.(?:skip|xfail)\b|"
    r"\bpytest\.skip\s*\(|@unittest\.skip|\bself\.skipTest\s*\(|\bt\.Skip(?:Now|f)?\s*\(|#\[ignore\]|@Disabled\b|@Ignore\b|"
    r"\bskip\s*\(\s*['\"]"
)
ASSERT_RE = re.compile(r"\b(?:assert\w*|expect|should|t\.(?:Error|Fatal)f?|XCTAssert\w*|require\.\w+)\b")


@dataclass
class Finding:
    path: str
    kind: str     # "secret" | "deleted-test" | "skipped-test" | "dropped-assertions"
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: {self.detail}"


def find_secret(new: Optional[bytes], old: Optional[bytes] = None) -> Optional[str]:
    """Name of a secret that `new` introduces (one already in `old` is not news)."""
    if not new:
        return None
    for name, rx in SECRET_PATTERNS:
        for m in rx.finditer(new):
            if not old or m.group(0) not in old:
                return name
    return None


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


def review_tests(plan: "Plan") -> List[Finding]:
    """Test files the change deletes, skips, or strips of assertions."""
    out: List[Finding] = []
    for f in plan.files:
        if not is_test_path(f.path):
            continue
        if f.change.kind == "delete":
            out.append(Finding(f.path, "deleted-test", "test file deleted"))
            continue
        if not f.splittable:
            continue
        added: List[str] = []
        removed: List[str] = []
        for op in f.ops:
            added.extend(f.b[op.j1 : op.j2])
            removed.extend(f.a[op.i1 : op.i2])
        new_skips = sum(1 for l in added if SKIP_RE.search(l)) - sum(1 for l in removed if SKIP_RE.search(l))
        if new_skips > 0:
            out.append(Finding(f.path, "skipped-test", f"{new_skips} test(s) newly skipped"))
        dropped = sum(1 for l in removed if ASSERT_RE.search(l)) - sum(1 for l in added if ASSERT_RE.search(l))
        if dropped >= 3:
            out.append(Finding(f.path, "dropped-assertions", f"{dropped} assertions removed"))
    return out
