"""
GitHub integration over the REST API (no `gh` dependency).

Token resolution order: explicit config -> GITHUB_TOKEN / GH_TOKEN ->
`gh auth token` (if gh exists) -> the git credential helper for github.com
(the same credential `git push` already uses).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

API = os.environ.get("GITNOLO_GITHUB_API", "https://api.github.com")
SLUG_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")

_TOKEN_CACHE: Dict[str, Optional[str]] = {}


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int = 0, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def parse_slug(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = SLUG_RE.search(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def resolve_token(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    if "auto" in _TOKEN_CACHE:
        return _TOKEN_CACHE["auto"]
    token: Optional[str] = None
    if shutil.which("gh"):
        try:
            proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0 and proc.stdout.strip():
                token = proc.stdout.strip()
        except Exception:
            pass
    if not token:
        try:
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GCM_INTERACTIVE"] = "never"
            proc = subprocess.run(
                ["git", "credential", "fill"],
                input="protocol=https\nhost=github.com\n\n",
                capture_output=True,
                text=True,
                timeout=8,
                env=env,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if line.startswith("password="):
                        token = line.split("=", 1)[1].strip() or None
        except Exception:
            pass
    _TOKEN_CACHE["auto"] = token
    return token


@dataclass
class PullRequest:
    number: int
    url: str
    title: str
    head: str
    base: str
    state: str = "open"
    merged: bool = False


class GitHub:
    def __init__(self, slug: str, token: Optional[str] = None):
        self.slug = slug
        self.token = token

    @classmethod
    def for_remote(cls, remote_url: Optional[str], token: Optional[str] = None) -> Optional["GitHub"]:
        slug = parse_slug(remote_url)
        if not slug:
            return None
        return cls(slug, resolve_token(token))

    # ------------------------------------------------------------- transport
    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 20) -> Any:
        url = path if path.startswith("http") else f"{API}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "gitnolo")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                payload = None
            msg = payload.get("message") if isinstance(payload, dict) else str(e)
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if errors:
                detail = "; ".join(str(x.get("message", x)) if isinstance(x, dict) else str(x) for x in errors)
                msg = f"{msg} ({detail})"
            raise GitHubError(f"GitHub {method} {path}: {e.code} {msg}", e.code, payload)
        except urllib.error.URLError as e:
            raise GitHubError(f"GitHub unreachable: {e.reason}")

    # ------------------------------------------------------------- repo info
    def repo_info(self) -> Dict[str, Any]:
        return self.request("GET", f"/repos/{self.slug}")

    def visibility(self) -> Tuple[bool, str]:
        """(is_private, source). Unknown repos are treated as private for safety."""
        try:
            info = self.repo_info()
            return bool(info.get("private", False)), "api"
        except GitHubError as e:
            if e.status in (401, 403, 404):
                return True, f"assumed private ({e.status})"
            return True, "assumed private (offline)"

    def viewer(self) -> Optional[str]:
        try:
            return self.request("GET", "/user").get("login")
        except GitHubError:
            return None

    # ------------------------------------------------------------- pull requests
    def find_open_pr(self, head_branch: str, base: Optional[str] = None) -> Optional[PullRequest]:
        owner = self.slug.split("/")[0]
        q = urllib.parse.urlencode({"head": f"{owner}:{head_branch}", "state": "open", **({"base": base} if base else {})})
        items = self.request("GET", f"/repos/{self.slug}/pulls?{q}")
        if items:
            it = items[0]
            return PullRequest(it["number"], it["html_url"], it["title"], head_branch, it["base"]["ref"])
        return None

    def create_pr(self, title: str, body: str, head: str, base: str, draft: bool = False) -> PullRequest:
        try:
            it = self.request(
                "POST",
                f"/repos/{self.slug}/pulls",
                {"title": title, "body": body, "head": head, "base": base, "draft": draft},
            )
        except GitHubError as e:
            if e.status == 422 and "already exists" in str(e):
                existing = self.find_open_pr(head, base)
                if existing:
                    return existing
            raise
        return PullRequest(it["number"], it["html_url"], it["title"], head, base)

    def ready_for_review(self, number: int) -> None:
        """Turns a draft PR into a regular one (GraphQL; best effort)."""
        try:
            node = self.request("GET", f"/repos/{self.slug}/pulls/{number}")
            if not node.get("draft"):
                return
            self.request("POST", "/graphql", {
                "query": "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){clientMutationId}}",
                "variables": {"id": node.get("node_id")},
            })
        except GitHubError:
            pass

    def list_prs(self, state: str = "open", limit: int = 30) -> List[Dict[str, Any]]:
        return self.request("GET", f"/repos/{self.slug}/pulls?state={state}&per_page={limit}")

    def merge_pr(self, number: int, method: str = "merge", title: Optional[str] = None, attempts: int = 6) -> Tuple[bool, str]:
        """Merges a PR, retrying while GitHub is still computing mergeability."""
        body: Dict[str, Any] = {"merge_method": method}
        if title:
            body["commit_title"] = title
        last = ""
        for i in range(attempts):
            try:
                res = self.request("PUT", f"/repos/{self.slug}/pulls/{number}/merge", body, timeout=30)
                return bool(res.get("merged", True)), res.get("sha", "")
            except GitHubError as e:
                last = str(e)
                transient = e.status in (405, 409) and any(
                    s in last.lower() for s in ("not mergeable", "base branch was modified", "try again", "mergeable")
                )
                if e.status in (502, 503) or transient:
                    time.sleep(1.0 + i)
                    continue
                return False, last
        return False, last

    def delete_branch(self, branch: str) -> bool:
        try:
            self.request("DELETE", f"/repos/{self.slug}/git/refs/heads/{urllib.parse.quote(branch)}")
            return True
        except GitHubError:
            return False

    # ------------------------------------------------------------- issues
    def list_issues(self, state: str = "open", limit: int = 100) -> List[Dict[str, Any]]:
        items = self.request("GET", f"/repos/{self.slug}/issues?state={state}&per_page={limit}")
        return [i for i in items if "pull_request" not in i]

    def create_issue(self, title: str, body: str, labels: Optional[List[str]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        try:
            return self.request("POST", f"/repos/{self.slug}/issues", payload)
        except GitHubError as e:
            if labels and e.status in (403, 422):
                return self.request("POST", f"/repos/{self.slug}/issues", {"title": title, "body": body})
            raise

    def close_issue(self, number: int, comment: Optional[str] = None) -> None:
        if comment:
            self.request("POST", f"/repos/{self.slug}/issues/{number}/comments", {"body": comment})
        self.request("PATCH", f"/repos/{self.slug}/issues/{number}", {"state": "closed"})
