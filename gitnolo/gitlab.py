"""
GitLab integration (gitlab.com or self-hosted) with the same interface as `GitHub`,
so the pipeline, issues and PR commands work unchanged. Merge requests are exposed
as PRs (`number` = iid, `html_url` = web_url).
Token: config gitlab_token -> GITLAB_TOKEN -> `glab auth token` -> git credential helper.
Self-hosted hosts: set GITNOLO_GITLAB_HOSTS=git.example.com,other.host
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
from typing import Any, Dict, List, Optional, Tuple

from .github import GitHub, GitHubError, PullRequest

_TOKENS: Dict[str, Optional[str]] = {}


def _hosts() -> List[str]:
    return ["gitlab.com"] + [h.strip() for h in os.environ.get("GITNOLO_GITLAB_HOSTS", "").split(",") if h.strip()]


def parse_remote(url: Optional[str]) -> Optional[Tuple[str, str]]:
    """(host, namespace/project) for a GitLab remote, subgroups included."""
    if not url:
        return None
    for host in _hosts():
        m = re.search(re.escape(host) + r"(?::\d+)?[:/]+(.+?)(?:\.git)?/?$", url.strip())
        if m and "/" in m.group(1):
            return host, m.group(1)
    return None


def resolve_token(host: str, explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit
    if os.environ.get("GITLAB_TOKEN"):
        return os.environ["GITLAB_TOKEN"]
    if host in _TOKENS:
        return _TOKENS[host]
    token = None
    cmds = ([["glab", "auth", "token", "--hostname", host]] if shutil.which("glab") else []) + [["git", "credential", "fill"]]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    for cmd in cmds:
        try:
            p = subprocess.run(cmd, input=f"protocol=https\nhost={host}\n\n", capture_output=True, text=True,
                               timeout=8, env=env)
        except Exception:
            continue
        if p.returncode == 0:
            out = p.stdout.strip()
            if cmd[0] == "git":
                out = next((ln.split("=", 1)[1] for ln in out.splitlines() if ln.startswith("password=")), "")
            if out:
                token = out
                break
    _TOKENS[host] = token
    return token


def _mr(it: Dict[str, Any]) -> Dict[str, Any]:
    return {**it, "number": it["iid"], "html_url": it.get("web_url", ""), "head": {"ref": it.get("source_branch", "")},
            "base": {"ref": it.get("target_branch", "")}}


class GitLab(GitHub):
    forge = "GitLab"

    def __init__(self, slug: str, token: Optional[str] = None, host: str = "gitlab.com"):
        super().__init__(slug, token)
        self.host = host
        self.api = os.environ.get("GITNOLO_GITLAB_API", f"https://{host}/api/v4")
        self.pid = f"/projects/{urllib.parse.quote(slug, safe='')}"

    @classmethod
    def for_remote(cls, remote_url: Optional[str], token: Optional[str] = None) -> Optional["GitLab"]:
        r = parse_remote(remote_url)
        return cls(r[1], resolve_token(r[0], token), r[0]) if r else None

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 20) -> Any:
        url = path if path.startswith("http") else f"{self.api}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", "gitnolo")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode()) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode())
            except Exception:
                payload = None
            msg = (payload.get("message") or payload.get("error")) if isinstance(payload, dict) else str(e)
            raise GitHubError(f"GitLab {method} {path}: {e.code} {msg}", e.code, payload)
        except urllib.error.URLError as e:
            raise GitHubError(f"GitLab unreachable: {e.reason}")

    def repo_info(self) -> Dict[str, Any]:
        info = self.request("GET", self.pid)
        return {**info, "private": info.get("visibility") != "public"}

    def viewer(self) -> Optional[str]:
        try:
            return self.request("GET", "/user").get("username")
        except GitHubError:
            return None

    # ------------------------------------------------------------- merge requests
    def find_open_pr(self, head_branch: str, base: Optional[str] = None) -> Optional[PullRequest]:
        q = urllib.parse.urlencode({"state": "opened", "source_branch": head_branch,
                                    **({"target_branch": base} if base else {})})
        items = self.request("GET", f"{self.pid}/merge_requests?{q}")
        if items:
            it = items[0]
            return PullRequest(it["iid"], it["web_url"], it["title"], head_branch, it["target_branch"])
        return None

    def create_pr(self, title: str, body: str, head: str, base: str, draft: bool = False) -> PullRequest:
        try:
            it = self.request("POST", f"{self.pid}/merge_requests", {
                "title": ("Draft: " if draft else "") + title, "description": body, "source_branch": head,
                "target_branch": base, "remove_source_branch": False})
        except GitHubError as e:
            if e.status == 409:
                existing = self.find_open_pr(head, base)
                if existing:
                    return existing
            raise
        return PullRequest(it["iid"], it["web_url"], it["title"], head, base)

    def ready_for_review(self, number: int) -> None:
        try:
            it = self.request("GET", f"{self.pid}/merge_requests/{number}")
            if it.get("draft") or it.get("work_in_progress"):
                title = re.sub(r"^(Draft:|\[Draft\]|WIP:)\s*", "", it["title"], flags=re.I)
                self.request("PUT", f"{self.pid}/merge_requests/{number}", {"title": title})
        except GitHubError:
            pass

    def pr_state(self, number: int) -> str:
        st = self.request("GET", f"{self.pid}/merge_requests/{number}").get("state", "")
        return "open" if st == "opened" else st

    def list_prs(self, state: str = "open", limit: int = 30) -> List[Dict[str, Any]]:
        st = {"open": "opened"}.get(state, state)
        return [_mr(i) for i in self.request("GET", f"{self.pid}/merge_requests?state={st}&per_page={limit}")]

    def merge_pr(self, number: int, method: str = "merge", title: Optional[str] = None, attempts: int = 6) -> Tuple[bool, str]:
        body: Dict[str, Any] = {"squash": method == "squash"}
        if title:
            body["merge_commit_message"] = title
        last = ""
        for i in range(attempts):
            try:
                res = self.request("PUT", f"{self.pid}/merge_requests/{number}/merge", body, timeout=30)
                return res.get("state") == "merged", res.get("merge_commit_sha") or res.get("sha", "")
            except GitHubError as e:
                last = str(e)
                if e.status in (405, 406, 422, 502, 503):  # mergeability still being computed
                    time.sleep(1.0 + i)
                    continue
                return False, last
        return False, last

    def delete_branch(self, branch: str) -> bool:
        try:
            self.request("DELETE", f"{self.pid}/repository/branches/{urllib.parse.quote(branch, safe='')}")
            return True
        except GitHubError:
            return False

    # ------------------------------------------------------------- issues
    def list_issues(self, state: str = "open", limit: int = 100) -> List[Dict[str, Any]]:
        st = {"open": "opened"}.get(state, state)
        return [{**i, "number": i["iid"], "html_url": i.get("web_url", "")}
                for i in self.request("GET", f"{self.pid}/issues?state={st}&per_page={limit}")]

    def create_issue(self, title: str, body: str, labels: Optional[List[str]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"title": title, "description": body, **({"labels": ",".join(labels)} if labels else {})}
        it = self.request("POST", f"{self.pid}/issues", payload)
        return {**it, "number": it["iid"], "html_url": it.get("web_url", "")}

    def close_issue(self, number: int, comment: Optional[str] = None) -> None:
        if comment:
            self.request("POST", f"{self.pid}/issues/{number}/notes", {"body": comment})
        self.request("PUT", f"{self.pid}/issues/{number}", {"state_event": "close"})


def for_remote(remote_url: Optional[str], config: Any) -> Optional[GitHub]:
    """The right forge client for a remote: GitHub or GitLab (None if neither)."""
    return (GitHub.for_remote(remote_url, getattr(config, "github_token", None))
            or GitLab.for_remote(remote_url, getattr(config, "gitlab_token", None)))
