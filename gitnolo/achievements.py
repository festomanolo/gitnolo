"""GitHub profile achievements: progress for the ones normal gitnolo use earns."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .github import GitHub, GitHubError, resolve_token

# name -> (tiers, how gitnolo helps)
TIERS: Dict[str, Tuple[Tuple[int, ...], str]] = {
    "Pull Shark": ((2, 16, 128, 1024), "every merged pipeline PR counts; `gitnolo watch -y` opens and merges one per finished agent turn"),
    "YOLO": ((1,), "merge a PR with no review; the pipeline's auto-merge does this on your own repos"),
    "Starstruck": ((16, 128, 512, 4096), "needs other people to star a repo; no tool can honestly earn it"),
}


def _search(gh: GitHub, query: str) -> int:
    from urllib.parse import quote
    return int(gh.request("GET", f"/search/issues?q={quote(query)}&per_page=1").get("total_count", 0))


def collect(token: Optional[str] = None) -> Tuple[Optional[str], List[Tuple[str, int, str]]]:
    gh = GitHub("-/-", token or resolve_token())
    login = gh.viewer()
    if not login:
        return None, []
    counts: Dict[str, int] = {}
    try:
        counts["Pull Shark"] = _search(gh, f"author:{login} is:pr is:merged")
        counts["YOLO"] = _search(gh, f"author:{login} is:pr is:merged review:none")
        repos: List[Any] = gh.request("GET", "/user/repos?per_page=100&affiliation=owner&visibility=public")
        counts["Starstruck"] = max([r.get("stargazers_count", 0) for r in repos] or [0])
    except GitHubError:
        pass
    return login, [(name, counts.get(name, 0), hint) for name, (_, hint) in TIERS.items()]


def tier_of(name: str, n: int) -> Tuple[str, Optional[int]]:
    """(current tier label, next threshold)."""
    tiers = TIERS[name][0]
    got = sum(1 for t in tiers if n >= t)
    nxt = tiers[got] if got < len(tiers) else None
    if got == 0:
        return "locked", nxt
    if len(tiers) == 1:
        return "earned", nxt
    return ("default", "bronze", "silver", "gold")[got - 1], nxt
