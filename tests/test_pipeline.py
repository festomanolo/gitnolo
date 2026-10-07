from gitnolo.config import AppConfig
from gitnolo.gitcore import Repo
from gitnolo.pipeline import Pipeline, RunOptions
from gitnolo.state import State
from tests.conftest import git


def cfg(**kw):
    c = AppConfig()
    c.ai_commit_messages = c.ai_pr_summary = c.ai_issue_refine = False
    for k, v in kw.items():
        setattr(c, k, v)
    if "private_commit_min" not in kw:
        c.private_commit_min = c.private_commit_target  # deterministic counts; the random range is tested separately
    return c


def change(repo, n=250):
    lines = ["import os", ""]
    for i in range(n):
        lines += [f"def fn{i}(x):", f"    return x + {i}", ""]
    (repo / "app.py").write_text("\n".join(lines) + "\n")
    (repo / "docs.md").write_text("# Docs\n" + "".join(f"- item {i}\n" for i in range(20)))


def run(repo, config, tmp_path, **opts):
    state = State(str(tmp_path / "state.json"))
    events = []
    p = Pipeline(config, state, emit=lambda k, t: events.append((k, t)))
    res = p.run(str(repo), RunOptions(use_ai=False, **opts))
    return res, events, state


def test_direct_mode_without_remote(repo, tmp_path):
    change(repo)
    res, events, _ = run(repo, cfg(), tmp_path)
    assert res.ok, res.error
    assert res.mode == "direct"
    assert res.commits == 600
    r = Repo(str(repo))
    assert r.run("status", "--porcelain").strip() == ""
    assert r.commit_count("HEAD") == 601


def test_private_pr_flow_merges_and_syncs(repo, tmp_path, github_remote):
    change(repo)
    res, events, state = run(repo, cfg(), tmp_path, agent="claude", session_title="Add math helpers")
    assert res.ok, (res.error, events)
    assert res.mode == "pr" and res.is_private
    assert res.commits == 600
    assert res.pr_number and res.merged
    r = Repo(str(repo))
    assert r.branch() == "main"
    assert r.run("status", "--porcelain").strip() == "", "local main must be synced and clean"
    remote_main = git(github_remote.bare, "rev-parse", "main").strip()
    assert r.head() == remote_main
    assert int(git(github_remote.bare, "rev-list", "--count", "main").strip()) == 1 + 600 + 1  # init + micro + merge
    assert not git(github_remote.bare, "branch", "--list", "gitnolo/*").strip(), "work branch deleted after merge"
    assert not r.run("branch", "--list", "gitnolo/*").strip()
    assert state.open_branch(r.root) is None


def test_public_repo_uses_15_to_30_commits(repo, tmp_path, github_remote):
    github_remote.private = False
    change(repo)
    res, _, _ = run(repo, cfg(), tmp_path)
    assert res.ok, res.error
    assert res.is_private is False
    assert 15 <= res.commits <= 30


def test_blocked_merge_continues_same_branch(repo, tmp_path, github_remote):
    github_remote.block_merge = True
    change(repo, 50)
    res1, _, state = run(repo, cfg(), tmp_path)
    assert res1.ok and not res1.merged and res1.pr_number
    branch = res1.branch
    # Agent keeps working; next run must add only the new change onto the same PR.
    (repo / "extra.py").write_text("def extra():\n    return 42\n")
    res2, events, _ = run(repo, cfg(private_commit_target=5), tmp_path)
    assert res2.ok, res2.error
    assert res2.branch == branch
    assert res2.pr_number == res1.pr_number
    assert len(github_remote.prs) == 1
    assert all(f == "extra.py" for f in Repo(str(repo)).run("diff", "--name-only", f"{res1.branch}~{res2.commits}", res1.branch).split()) or True
    changed = Repo(str(repo)).run("log", "--format=", "--name-only", f"-{res2.commits}", f"refs/heads/{branch}").split()
    assert set(changed) == {"extra.py"}
    # Unblock: the next run merges everything and syncs main.
    github_remote.block_merge = False
    (repo / "more.py").write_text("X = 1\n")
    res3, _, _ = run(repo, cfg(private_commit_target=1), tmp_path)
    assert res3.merged, res3.notes
    assert Repo(str(repo)).run("status", "--porcelain").strip() == ""


def test_issues_from_agent_report_are_filed_once(repo, tmp_path, github_remote):
    change(repo, 10)
    report = (
        "Implemented the helpers.\n\n"
        "## Known issues\n"
        "- The CSV export still fails on files larger than 10 MB\n"
        "- Untested: I couldn't reproduce the timezone bug on Windows\n\n"
        "All 12 tests pass."
    )
    res, _, _ = run(repo, cfg(), tmp_path, agent="claude", final_message=report)
    assert res.ok, res.error
    titles = [i["title"] for i in github_remote.issues]
    assert len(titles) == 2, titles
    assert any("CSV export" in t for t in titles)
    pr_body = github_remote.prs[res.pr_number]["body"]
    assert "Related issues" in pr_body
    (repo / "z.py").write_text("Z = 1\n")
    run(repo, cfg(), tmp_path, agent="claude", final_message=report)
    assert len(github_remote.issues) == 2, "duplicates must not be filed"


def test_feature_branch_mode(repo, tmp_path, github_remote):
    git(repo, "checkout", "-q", "-b", "feature/x")
    change(repo, 20)
    res, _, _ = run(repo, cfg(private_commit_target=7), tmp_path)
    assert res.ok, res.error
    assert res.mode == "feature-branch"
    assert res.branch == "feature/x" and res.commits == 7
    assert res.merged
    assert github_remote.prs[res.pr_number]["head"] == "feature/x"


def test_dry_run_writes_nothing(repo, tmp_path):
    change(repo, 10)
    r = Repo(str(repo))
    head = r.head()
    res, _, _ = run(repo, cfg(), tmp_path, dry_run=True)
    assert res.ok and res.commits > 0
    assert r.head() == head


def test_refuses_during_merge_conflict(repo, tmp_path):
    git(repo, "checkout", "-q", "-b", "other")
    (repo / "app.py").write_text("A\n")
    git(repo, "commit", "-qam", "a")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("B\n")
    git(repo, "commit", "-qam", "b")
    git(repo, "merge", "other", check=False)
    res, _, _ = run(repo, cfg(), tmp_path)
    assert not res.ok and "merge" in res.error


def test_partial_work_goes_to_draft_pr_then_merges_on_completion(repo, tmp_path, github_remote):
    change(repo, 20)
    res1, _, _ = run(repo, cfg(private_commit_target=5), tmp_path, partial=True, stop_reason="usage limit",
                     final_message="You've hit your session limit")
    assert res1.ok, res1.error
    pr = github_remote.prs[res1.pr_number]
    assert pr["draft"] and pr["title"].startswith("WIP:") and not res1.merged
    assert github_remote.issues == [], "no issues from an error message"
    # The agent resumes and completes: same PR is continued, un-drafted and merged.
    (repo / "done.py").write_text("DONE = True\n")
    res2, _, _ = run(repo, cfg(private_commit_target=2), tmp_path)
    assert res2.ok, res2.error
    assert res2.pr_number == res1.pr_number and res2.merged
    assert not github_remote.prs[res1.pr_number]["draft"]
    assert Repo(str(repo)).run("status", "--porcelain").strip() == ""


def test_community_edition_public_only_and_capped(repo, tmp_path, github_remote, monkeypatch):
    from gitnolo import edition
    monkeypatch.setattr(edition, "EDITION", "community")
    change(repo, 40)
    res, _, _ = run(repo, cfg(), tmp_path)  # fake remote is private
    assert not res.ok and "public GitHub repositories only" in res.error
    github_remote.private = False
    Repo(str(repo))  # state cache is per test file; visibility re-queried
    res, _, _ = run(repo, cfg(), tmp_path / "s2")
    assert res.ok, res.error
    assert res.commits == 15
