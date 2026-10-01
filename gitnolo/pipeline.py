"""
The commit pipeline: plan -> micro-commits -> push -> PR -> merge -> local sync,
plus issues extracted from the agent's report.

Branch strategy
  * On the default branch: commits go to a fresh `gitnolo/<stamp>-<slug>` branch
    (built with fast-import, never checked out), pushed, opened as a PR and
    merged. The local default branch is then fast-forwarded without touching
    the files an agent may still be editing.
  * On a feature branch: commits go straight onto it, it is pushed, and a PR to
    the default branch is opened (and merged when auto_merge is on).
  * No GitHub remote / no token: commits go onto the current branch and are
    pushed if a remote exists.
  * If a PR is left open (merge disabled or blocked), the next run continues
    the same branch, so the same change is never committed twice.
"""

from __future__ import annotations

import fcntl
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import issues as issue_mod
from . import microcommit as mc
from . import edition
from .ai import make_client
from .config import AppConfig
from .github import GitHub, GitHubError
from .gitcore import GitError, Repo
from .ollama_client import OllamaClient
from .state import State

Emit = Callable[[str, str], None]


@dataclass
class RunOptions:
    target: Optional[int] = None
    push: Optional[bool] = None
    pr: Optional[bool] = None
    merge: Optional[bool] = None
    issues: Optional[bool] = None
    use_ai: bool = True
    dry_run: bool = False
    agent: str = ""
    session_title: str = ""
    final_message: str = ""
    partial: bool = False          # the agent stopped before finishing: PR as draft, never auto-merge
    stop_reason: str = ""
    confirm: Optional[Callable[["mc.Plan", Dict[str, Any]], bool]] = None


@dataclass
class RunResult:
    ok: bool
    repo: str
    commits: int = 0
    subjects: List[str] = field(default_factory=list)
    files: int = 0
    added: int = 0
    removed: int = 0
    branch: str = ""
    pr_url: str = ""
    pr_number: Optional[int] = None
    merged: bool = False
    pushed: bool = False
    issues: List[Tuple[int, str, str]] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0
    is_private: Optional[bool] = None
    mode: str = ""


class RepoLock:
    def __init__(self, repo: Repo):
        self.path = os.path.join(repo.git_dir, "gitnolo.lock")
        self.fd: Optional[int] = None

    def __enter__(self) -> bool:
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)


def _slugify(text: str, limit: int = 32) -> str:
    text = re.sub(r"^[a-z]+(\([^)]*\))?!?:\s*", "", text.strip().lower())
    s = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return s[:limit].rstrip("-") or "update"


def commit_target(config: AppConfig, is_private: bool, natural: int) -> int:
    if edition.is_community():
        return max(1, min(edition.COMMUNITY_MAX_COMMITS, int(config.public_commit_max)))
    if is_private:
        return max(1, int(config.private_commit_target))
    lo, hi = int(config.public_commit_min), int(config.public_commit_max)
    return max(lo, min(hi, natural))


class Pipeline:
    def __init__(self, config: AppConfig, state: Optional[State] = None, emit: Optional[Emit] = None):
        self.config = config
        self.state = state or State()
        self.emit: Emit = emit or (lambda kind, text: None)
        self._llm: Optional[OllamaClient] = None
        self._llm_checked = False

    # ------------------------------------------------------------ helpers
    def llm(self) -> Optional[OllamaClient]:
        if not self._llm_checked:
            self._llm_checked = True
            self._llm = make_client(self.config)
        return self._llm

    def _visibility(self, gh: Optional[GitHub]) -> Tuple[bool, str]:
        if not gh:
            return True, "no GitHub remote (private rules)"
        cached = self.state.visibility(gh.slug)
        if cached is not None:
            return cached, "cached"
        private, source = gh.visibility()
        if source == "api":
            self.state.set_visibility(gh.slug, private)
        return private, source

    # ------------------------------------------------------------ main
    def run(self, path: str, opts: Optional[RunOptions] = None) -> RunResult:
        opts = opts or RunOptions()
        started = time.time()
        try:
            repo = Repo(path)
        except GitError as e:
            return RunResult(False, path, error=str(e))
        result = RunResult(False, repo.root)
        with RepoLock(repo) as acquired:
            if not acquired:
                result.error = "another gitnolo run holds this repository; skipped"
                return result
            try:
                self._run(repo, opts, result)
            except (GitError, GitHubError) as e:
                result.error = str(e)
                self.emit("error", str(e))
            finally:
                result.elapsed = time.time() - started
                if result.commits and not opts.dry_run:
                    self.state.record(
                        {
                            "repo": repo.root,
                            "commits": result.commits,
                            "pr": result.pr_url,
                            "merged": result.merged,
                            "issues": [n for n, _, _ in result.issues],
                            "branch": result.branch,
                            "agent": opts.agent,
                            "elapsed": round(result.elapsed, 2),
                        }
                    )
                try:
                    self.state.save()
                except OSError:
                    pass
        return result

    def _run(self, repo: Repo, opts: RunOptions, result: RunResult) -> None:
        cfg = self.config
        emit = self.emit
        op = repo.operation_in_progress()
        if op:
            raise GitError(f"a {op} is in progress in {repo.name}; resolve it first (gitnolo conflict)")
        if repo.conflicted_files():
            raise GitError(f"unresolved conflicts in {repo.name}; run gitnolo conflict")

        remote = repo.remote_url()
        gh = GitHub.for_remote(remote, cfg.github_token) if remote else None
        is_private, vis_source = self._visibility(gh)
        result.is_private = is_private
        if edition.is_community() and (is_private or not gh):
            raise GitError(
                f"{repo.name} is private or not on GitHub ({vis_source}); "
                "gitnolo community works on public GitHub repositories only"
            )
        want_push = cfg.auto_push if opts.push is None else opts.push
        want_pr = (cfg.auto_pr if opts.pr is None else opts.pr) and want_push
        want_merge = (cfg.auto_merge if opts.merge is None else opts.merge) and not opts.partial
        want_issues = cfg.auto_issues if opts.issues is None else opts.issues
        can_api = bool(gh and gh.token)

        info: Dict[str, Any] = {}
        if can_api and (want_pr or want_issues):
            try:
                info = gh.repo_info()  # type: ignore[union-attr]
            except GitHubError as e:
                emit("warn", f"GitHub API: {e}")
                can_api = False
        default = info.get("default_branch") or repo.default_branch()
        current = repo.branch()

        # Reconcile a PR branch left open by an earlier run.
        cont = self._reconcile_open_branch(repo, gh if can_api else None, default, current)
        base = cont["tip"] if cont else repo.head()

        emit("step", f"Analyzing {repo.name}")
        llm = self.llm() if (opts.use_ai and (cfg.ai_commit_messages or cfg.ai_pr_summary or cfg.ai_issue_refine)) else None
        if llm:
            llm.set_budget(cfg.ai_budget_seconds)

        probe = mc.plan(repo, 0, base=base)
        if not probe.commits:
            for p, why in probe.skipped:
                emit("warn", f"skipped {p}: {why}")
            result.skipped = probe.skipped
            result.ok = True
            result.notes.append("nothing to commit")
            emit("info", "Working tree clean, nothing to commit")
            return
        natural = len(probe.commits)
        target = opts.target if opts.target is not None else commit_target(cfg, is_private, natural)
        if edition.is_community():
            target = min(target, edition.COMMUNITY_MAX_COMMITS)

        hook = None
        if llm and cfg.ai_commit_messages and not is_private and target <= 40:
            hook = self._ai_subject_hook(llm)
        plan = mc.plan(repo, target, base=base, message_hook=hook)
        add, rem = plan.stats()
        result.files, result.added, result.removed = len(plan.files), add, rem
        result.skipped = plan.skipped
        policy = "private" if is_private else "public"
        emit(
            "info",
            f"{len(plan.files)} files  +{add} -{rem}  ->  {len(plan.commits)} commits "
            f"({policy} policy, target {target}, max possible {plan.max_possible})",
        )
        for p, why in plan.skipped:
            emit("warn", f"skipped {p}: {why}")

        if current and current != default:
            mode = "feature-branch"
        elif can_api and want_pr:
            mode = "pr"
        else:
            mode = "direct"
        result.mode = mode

        if opts.confirm and not opts.confirm(
            plan, {"is_private": is_private, "mode": mode, "default": default, "current": current, "target": target}
        ):
            result.notes.append("cancelled")
            emit("info", "Cancelled")
            return
        if opts.dry_run:
            result.ok = True
            result.commits = len(plan.commits)
            result.subjects = [c.subject for c in plan.commits]
            emit("info", "Dry run: no commits written")
            return

        # Issues first so the PR can reference them.
        if want_issues and can_api and opts.final_message and not opts.partial:
            result.issues = self._file_issues(repo, gh, opts, llm)  # type: ignore[arg-type]

        emit("step", f"Writing {len(plan.commits)} commits")
        body = None
        if opts.agent:
            body = f"Agent: {opts.agent}" + (f"\nTask: {opts.session_title}" if opts.session_title and is_private else "")
        res = mc.execute(plan, body=body)
        result.commits = res.count
        result.subjects = res.subjects
        emit("ok", f"{res.count} commits written and verified in {res.elapsed:.2f}s")

        if mode == "pr":
            self._pr_flow(repo, gh, plan, res, opts, result, default, current, cont, want_merge, llm)  # type: ignore[arg-type]
        else:
            target_ref = f"refs/heads/{current}" if current else "HEAD"
            repo.update_ref(target_ref, res.tip, res.base, "gitnolo: micro-commits")
            repo.sync_index(res.paths, "HEAD")
            result.branch = current or "HEAD"
            repo.delete_ref(mc.WORK_REF)
            if want_push and remote and current:
                emit("step", f"Pushing {current}")
                try:
                    repo.push(current, current, set_upstream=True)
                    result.pushed = True
                    emit("ok", f"Pushed {current}")
                except GitError as e:
                    emit("error", f"push failed: {e}")
                    result.notes.append("push failed")
            if mode == "feature-branch" and result.pushed and can_api and want_pr:
                self._open_and_merge(repo, gh, plan, res, opts, result, current, default, want_merge, llm)  # type: ignore[arg-type]
        result.ok = not result.error

    # ------------------------------------------------------------ PR flow
    def _pr_flow(
        self,
        repo: Repo,
        gh: GitHub,
        plan: "mc.Plan",
        res: "mc.CommitResult",
        opts: RunOptions,
        result: RunResult,
        default: str,
        current: Optional[str],
        cont: Optional[Dict[str, Any]],
        want_merge: bool,
        llm: Optional[OllamaClient],
    ) -> None:
        emit = self.emit
        if cont:
            branch = cont["branch"]
            old = cont["tip"]
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            topic = opts.session_title or (res.subjects[0] if res.subjects else "update")
            branch = f"gitnolo/{stamp}-{_slugify(topic)}"
            old = None
        repo.update_ref(f"refs/heads/{branch}", res.tip, old, "gitnolo: work branch")
        repo.delete_ref(mc.WORK_REF)
        result.branch = branch
        emit("step", f"Pushing {branch}")
        repo.push(branch, branch)
        result.pushed = True
        emit("ok", f"Pushed {len(res.subjects)} commits to {branch}")
        self.state.set_open_branch(repo.root, {"branch": branch, "tip": res.tip, "base": default, "pr": cont.get("pr") if cont else None})
        self._open_and_merge(repo, gh, plan, res, opts, result, branch, default, want_merge, llm)
        if result.merged:
            self.state.set_open_branch(repo.root, None)
            repo.delete_ref(f"refs/heads/{branch}")
            if current == default:
                self._sync_default(repo, default, res)
        else:
            self.state.set_open_branch(
                repo.root, {"branch": branch, "tip": res.tip, "base": default, "pr": result.pr_number}
            )

    def _open_and_merge(
        self,
        repo: Repo,
        gh: GitHub,
        plan: "mc.Plan",
        res: "mc.CommitResult",
        opts: RunOptions,
        result: RunResult,
        head: str,
        base: str,
        want_merge: bool,
        llm: Optional[OllamaClient],
    ) -> None:
        emit = self.emit
        pr = gh.find_open_pr(head, base)
        if pr:
            emit("info", f"Updated PR #{pr.number}")
            if not opts.partial and want_merge:
                gh.ready_for_review(pr.number)
        else:
            title, body = self._pr_text(repo, plan, res, opts, result, llm)
            if opts.partial:
                title = f"WIP: {title}"
                body = (f"> Agent stopped before finishing ({opts.stop_reason or 'interrupted'}). "
                        "This draft keeps the partial work safe; gitnolo continues and merges it when the agent completes.\n\n" + body)
            emit("step", "Opening pull request" + (" (draft)" if opts.partial else ""))
            pr = gh.create_pr(title, body, head, base, draft=opts.partial)
            emit("ok", f"PR #{pr.number} {pr.url}")
        result.pr_url, result.pr_number = pr.url, pr.number
        if not want_merge:
            return
        emit("step", f"Merging PR #{pr.number} ({self.config.merge_method})")
        ok, detail = gh.merge_pr(pr.number, self.config.merge_method)
        if ok:
            result.merged = True
            emit("ok", f"Merged PR #{pr.number}")
            if self.config.delete_branch_after_merge and head.startswith("gitnolo/"):
                gh.delete_branch(head)
        else:
            result.notes.append(f"merge blocked: {detail}")
            emit("warn", f"PR left open: {detail}")

    def _sync_default(self, repo: Repo, default: str, res: "mc.CommitResult") -> None:
        """Fast-forwards the local default branch to the merged remote, leaving agent edits intact."""
        emit = self.emit
        if not repo.fetch("origin", default):
            emit("warn", "fetch failed; local branch not fast-forwarded")
            return
        remote_ref = f"refs/remotes/origin/{default}"
        head = repo.head()
        if not head:
            return
        # Every path the merged branch changed relative to local HEAD (spans earlier runs too).
        paths = [p for p in repo.run("diff", "--name-only", "-z", head, res.tip, check=False).split("\0") if p]
        repo.sync_index(paths, res.tip)
        code, _, err = repo.run_bytes("merge", "--ff-only", "--quiet", remote_ref)
        if code == 0:
            emit("ok", f"Local {default} fast-forwarded")
        else:
            repo.sync_index(paths, "HEAD")
            emit("warn", f"local {default} not fast-forwarded ({err.decode().strip()[:120]}); run git pull when idle")

    def _reconcile_open_branch(
        self, repo: Repo, gh: Optional[GitHub], default: str, current: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        info = self.state.open_branch(repo.root)
        if not info:
            return None
        branch, tip = info.get("branch"), info.get("tip")
        local = repo.rev(f"refs/heads/{branch}") if branch else None
        head = repo.head()
        if not branch or local != tip or not head:
            self.state.set_open_branch(repo.root, None)
            return None
        # Merged elsewhere (e.g. by hand on GitHub)? Then sync and start fresh.
        if repo.fetch("origin", default) and repo.is_ancestor(tip, f"refs/remotes/origin/{default}"):
            self.state.set_open_branch(repo.root, None)
            repo.delete_ref(f"refs/heads/{branch}")
            if current == default:
                paths = [p for p in repo.run("diff", "--name-only", "-z", head, tip).split("\0") if p]
                fake = mc.CommitResult(head, tip, 0, [], paths, 0.0)
                self._sync_default(repo, default, fake)
            return None
        if gh and info.get("pr"):
            try:
                pr = gh.request("GET", f"/repos/{gh.slug}/pulls/{info['pr']}")
                if pr.get("state") != "open":
                    self.state.set_open_branch(repo.root, None)
                    return None
            except GitHubError:
                pass
        if not repo.is_ancestor(head, tip):
            self.state.set_open_branch(repo.root, None)
            return None
        self.emit("info", f"Continuing open branch {branch}")
        return info

    # ------------------------------------------------------------ text
    def _ai_subject_hook(self, llm: OllamaClient) -> Callable[[Any, List[str], List[str]], Optional[str]]:
        def hook(fp: Any, added: List[str], removed: List[str]) -> Optional[str]:
            if llm.remaining() < 10:
                return None
            snippet = "".join("-" + l for l in removed[:40]) + "".join("+" + l for l in added[:60])
            try:
                return llm.commit_subject(fp.path, snippet, timeout=min(10, llm.remaining() - 8))
            except Exception:
                return None

        return hook

    def _pr_text(
        self,
        repo: Repo,
        plan: "mc.Plan",
        res: "mc.CommitResult",
        opts: RunOptions,
        result: RunResult,
        llm: Optional[OllamaClient],
    ) -> Tuple[str, str]:
        is_private = bool(result.is_private)
        types = Counter(s.split("(")[0].split(":")[0] for s in res.subjects)
        main_type = types.most_common(1)[0][0] if types else "chore"
        topic = opts.session_title.strip() if opts.session_title else ""
        title = f"{main_type}: {topic}" if topic else (
            res.subjects[0] if len(res.subjects) == 1 else f"{main_type}: update {len(plan.files)} files"
        )
        summary = ""
        bullets: List[str] = []
        if llm and self.config.ai_pr_summary and llm.remaining() > 5:
            try:
                diff = repo.run("diff", "--stat", "--patch", "--no-color", f"{res.base}..{res.tip}", check=False) if res.base else ""
                data = llm.pr_summary(diff, res.subjects, is_private, opts.final_message if is_private else "",
                                      timeout=min(30, llm.remaining()))
                if data:
                    title = str(data.get("title") or title)[:72]
                    summary = str(data.get("summary") or "")
                    bullets = [str(b) for b in (data.get("changes") or []) if str(b).strip()][:8]
            except Exception:
                pass
        stats = plan.stats()
        lines: List[str] = ["## Summary", "", summary or (
            f"{len(res.subjects)} focused commits across {len(plan.files)} files"
            + (f" from {opts.agent}" if opts.agent else "") + "."
        ), ""]
        if bullets:
            lines += ["## Changes", ""] + [f"- {b}" for b in bullets] + [""]
        lines += ["## Files", ""]
        nums = {}
        for f in plan.files:
            a = r = 0
            for op in f.ops:
                a += op.j2 - op.j1
                r += op.i2 - op.i1
            nums[f.path] = (a, r)
        for f in plan.files[:40]:
            a, r = nums[f.path]
            lines.append(f"- `{f.path}` ({f.change.kind}, +{a} -{r})")
        if len(plan.files) > 40:
            lines.append(f"- and {len(plan.files) - 40} more")
        lines += ["", f"**{len(res.subjects)} commits**, +{stats[0]} -{stats[1]}", ""]
        if result.issues:
            lines += ["## Related issues", ""] + [f"- #{n} {t}" for n, _, t in result.issues] + [""]
        if is_private and opts.final_message:
            report = opts.final_message.strip()
            if len(report) > 3000:
                report = report[:3000] + "\n..."
            lines += ["<details><summary>Agent report</summary>", "", report, "", "</details>", ""]
        lines += ["---", "_Automated by gitnolo_"]
        return title, "\n".join(lines)

    def _file_issues(
        self, repo: Repo, gh: GitHub, opts: RunOptions, llm: Optional[OllamaClient]
    ) -> List[Tuple[int, str, str]]:
        emit = self.emit
        drafts = issue_mod.extract(
            opts.final_message,
            repo_name=repo.name,
            agent=opts.agent,
            session_title=opts.session_title,
            llm=llm if (llm and self.config.ai_issue_refine and llm.remaining() > 8) else None,
            limit=self.config.max_issues_per_turn,
        )
        if not drafts:
            return []
        try:
            existing = gh.list_issues("open")
        except GitHubError as e:
            emit("warn", f"issues unavailable: {e}")
            return []
        created: List[Tuple[int, str, str]] = []
        for d in drafts:
            if self.state.issue_known(repo.root, d.fingerprint):
                continue
            dup = next((i for i in existing if issue_mod.similar(d.title, i.get("title", ""))), None)
            if dup:
                self.state.add_issue(repo.root, d.fingerprint, dup["number"])
                emit("info", f"issue already tracked as #{dup['number']}: {dup['title'][:60]}")
                continue
            try:
                it = gh.create_issue(d.title, d.body, d.labels)
            except GitHubError as e:
                emit("warn", f"could not open issue: {e}")
                break
            self.state.add_issue(repo.root, d.fingerprint, it["number"])
            created.append((it["number"], it.get("html_url", ""), d.title))
            emit("ok", f"Issue #{it['number']} {d.title[:70]}")
        return created
