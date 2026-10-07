"""Live issues, guardrails, checkpoints, rapid response and the map."""

import io
import json
import os
import random
import time

from gitnolo import checkpoints, guard, hooks, issues, scene
from gitnolo.agents import FINISHED, WORKING, Session, Tracker
from gitnolo.auto_accept import _decide, classify
from gitnolo.config import AppConfig
from gitnolo.gitcore import Repo
from gitnolo.live_issues import LiveIssues
from gitnolo.pipeline import commit_target
from gitnolo.state import State
from tests.conftest import git
from tests.test_agents import claude_file, iso, write_jsonl
from tests.test_pipeline import cfg, run


def session(repo, state=WORKING, key="claude:s1", msgs=()):
    return Session(key, "claude", "s1", str(repo), str(repo), state, time.time(), title="Add export",
                   messages=[(f"m{abs(hash(t))}", t, time.time()) for t in msgs])


def live(tmp_path, **kw):
    c = cfg(**kw)
    notes = []
    return LiveIssues(c, State(str(tmp_path / "state.json")), lambda k, t: notes.append((k, t)), catch_up=True), notes


# ------------------------------------------------------------ live issues
def test_problem_snapped_mid_turn_and_resolved_before_filing(tmp_path, repo):
    li, _ = live(tmp_path, live_issue_grace_seconds=300)
    li.observe([session(repo, msgs=["The CSV export still fails on files over 10 MB."])])
    [e] = li.state.open_ledger(str(repo))
    assert e["status"] == "snapped"
    li.observe([session(repo, msgs=["The CSV export still fails on files over 10 MB.",
                                    "Fixed: the CSV export now streams files over 10 MB."])])
    assert li.state.open_ledger(str(repo)) == []
    assert li.state.ledger(str(repo))[0]["status"] == "resolved"
    assert li.sync() == 0  # nothing to publish or close


def test_working_notes_and_generic_success_are_ignored(tmp_path, repo):
    li, _ = live(tmp_path)
    li.observe([session(repo, msgs=["Let me check why the login test fails.", "I'll look into the 500 error next."])])
    assert li.state.ledger(str(repo)) == []
    li.observe([session(repo, msgs=["The login page still returns a 500 when the cookie is missing.", "All tests pass."])])
    assert len(li.state.open_ledger(str(repo))) == 1, "a bare 'all tests pass' closes nothing"


def test_published_then_closed_on_github(tmp_path, repo, github_remote):
    li, _ = live(tmp_path, live_issue_grace_seconds=0)
    msgs = ["The login page still returns a 500 when the session cookie is missing."]
    li.observe([session(repo, msgs=msgs)])
    assert li.sync() == 1
    [gh_issue] = github_remote.issues
    assert gh_issue["state"] == "open" and "agent-reported" in gh_issue["labels"]
    e = li.state.ledger(str(repo))[0]
    assert e["status"] == "open" and e["number"] == gh_issue["number"]

    msgs.append("Fixed the 500 on the login page when the session cookie is missing.")
    li.observe([session(repo, msgs=msgs)])
    assert e["status"] == "closing"
    assert li.sync() == 1
    assert gh_issue["state"] == "closed"
    assert github_remote.comments and "Fixed the 500" in github_remote.comments[0][1]
    assert e["status"] == "closed"


def test_explicit_issue_reference_closes(tmp_path, repo, github_remote):
    li, _ = live(tmp_path, live_issue_grace_seconds=0)
    li.observe([session(repo, msgs=["Untested: I couldn't run the iOS build here."])])
    li.sync()
    n = github_remote.issues[0]["number"]
    li.observe([session(repo, msgs=["Untested: I couldn't run the iOS build here.", f"This fixes #{n}."])])
    li.sync()
    assert github_remote.issues[0]["state"] == "closed"


def test_final_report_issues_skip_the_grace_period(tmp_path, repo):
    li, _ = live(tmp_path, live_issue_grace_seconds=300)
    li.observe([session(repo, state=FINISHED, msgs=["Done. Known issue: the PDF export still crashes on emoji."])])
    [e] = li.state.open_ledger(str(repo))
    assert e["publish_at"] <= time.time()
    li.sync()
    assert e["status"] == "local"  # no GitHub remote: tracked locally, still closable
    li.observe([session(repo, state=FINISHED, msgs=["Done. Known issue: the PDF export still crashes on emoji.",
                                                    "The PDF export crash on emoji is fixed."])])
    assert e["status"] == "closed"


def test_history_before_start_is_not_replayed(tmp_path, repo):
    c = cfg()
    li = LiveIssues(c, State(str(tmp_path / "s.json")))
    li.observe([session(repo, msgs=["The export still fails on big files."])])
    assert li.state.ledger(str(repo)) == []


def test_resolution_matching():
    assert issues.resolves("Fixed the crash on startup", "App crashes on startup when the cache is empty")
    assert not issues.resolves("Fixed the crash on startup", "Export fails on large files")
    assert not issues.resolves("All tests pass now", "The login page still returns a 500")


# ------------------------------------------------------------ commit counts
def test_private_commit_count_is_random_within_range():
    c = AppConfig()
    rng = random.Random(7)
    counts = {commit_target(c, True, 50, rng) for _ in range(40)}
    assert all(100 <= n <= 600 for n in counts)
    assert len(counts) > 20, "a fresh number every run, not a fixed 600"
    assert commit_target(c, False, 50) == 30 and commit_target(c, False, 3) == 15


# ------------------------------------------------------------ guardrails
def test_secret_in_new_content_is_never_committed(repo, tmp_path):
    (repo / "config.py").write_text('API_KEY = "sk-ant-api03-' + "a" * 40 + '"\n')
    (repo / "app.py").write_text("import os\n\n\ndef main():\n    return 2\n")
    res, events, _ = run(repo, cfg(private_commit_target=1), tmp_path)
    assert res.ok, res.error
    assert ("config.py", "contains a Anthropic API key; commit it manually if intended") in res.skipped
    assert "config.py" not in Repo(str(repo)).run("show", "--name-only", "--format=", "HEAD")


def test_skipped_tests_hold_the_merge(repo, tmp_path, github_remote):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_a():\n    assert 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "tests")
    git(repo, "push", "-q", "origin", "main")
    (repo / "tests" / "test_app.py").write_text("import pytest\n\n@pytest.mark.skip\ndef test_a():\n    assert 1\n")
    res, events, _ = run(repo, cfg(private_commit_target=2), tmp_path)
    assert res.ok, res.error
    assert res.pr_number and not res.merged
    assert any("newly skipped" in g for g in res.guard)
    pr = github_remote.prs[res.pr_number]
    assert "## Needs review" in pr["body"]


def test_guard_patterns():
    assert guard.find_secret(b"token = 'ghp_" + b"x" * 36 + b"'") == "GitHub token"
    old = b"key = 'AKIAABCDEFGHIJKLMNOP'"
    assert guard.find_secret(old + b"\nmore", old) is None, "already committed secrets are not news"
    assert guard.is_test_path("src/__tests__/a.test.ts") and not guard.is_test_path("src/testing.py")


# ------------------------------------------------------------ checkpoints
def test_checkpoint_and_rewind(repo):
    r = Repo(str(repo))
    (repo / "notes.txt").write_text("untracked but precious\n")
    cp = checkpoints.create(r, "before agent")
    assert cp and checkpoints.create(r, "again") is None, "unchanged tree: no duplicate checkpoint"
    # the agent's work: edit, delete, create
    (repo / "app.py").write_text("broken\n")
    os.remove(repo / "README.md")
    os.remove(repo / "notes.txt")
    (repo / "new").mkdir()
    (repo / "new" / "junk.py").write_text("x\n")
    status_before = r.run("status", "--porcelain")
    changed, added, deleted = checkpoints.diff_from_now(r, cp)
    assert changed == ["app.py"] and added == ["new/junk.py"] and sorted(deleted) == ["README.md", "notes.txt"]

    safety, n = checkpoints.restore(r, cp)
    assert n == 4
    assert (repo / "app.py").read_text().startswith("import os")
    assert (repo / "README.md").exists() and (repo / "notes.txt").read_text() == "untracked but precious\n"
    assert not (repo / "new").exists()
    assert r.run("diff", "--cached", "--name-only") == "", "the index is not touched"
    # and the rewind itself can be undone
    checkpoints.restore(r, safety)
    assert r.run("status", "--porcelain") == status_before


# ------------------------------------------------------------ rapid response
QUESTION = ("Which database should we use?\n❯ 1. Postgres (Recommended)\n  2. SQLite\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel")


def test_questions_get_first_option_only_in_rapid_mode():
    assert classify(QUESTION) == ("choice", b"\r")
    assert _decide(QUESTION) is None
    assert _decide(QUESTION, rapid=True) == b"\r"
    assert classify("Plan:\n> 1. Add the model\n> 2. Add the view") == (None, None), "plain lists are not menus"


def test_hook_decisions():
    assert hooks.decide({"tool_name": "Edit", "tool_input": {"file_path": "/p/src/a.ts"}}) == ("allow", "gitnolo rapid response")
    assert hooks.decide({"tool_name": "Bash", "tool_input": {"command": "rm -rf ~"}})[0] == "ask"
    assert hooks.decide({"tool_name": "Write", "tool_input": {"file_path": "/home/u/.ssh/config"}})[0] == "ask"
    assert hooks.decide({"tool_name": "Write", "tool_input": {"file_path": "/p/.env.example"}})[0] == "allow"
    assert hooks.decide({"tool_name": "AskUserQuestion", "tool_input": {}}) is None
    assert hooks.decide({"tool_name": "Bash", "tool_input": {"command": "npm test"}}, scope="edits") is None
    out = io.StringIO()
    hooks.run_hook(io.StringIO(json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}})), out)
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_hook_install_is_idempotent_and_reversible(tmp_path):
    path = str(tmp_path / "settings.json")
    with open(path, "w") as f:
        json.dump({"model": "x", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]}]}}, f)
    assert hooks.install(path) and not hooks.install(path)
    assert hooks.installed(path)
    assert hooks.uninstall(path) and not hooks.installed(path)
    data = json.load(open(path))
    assert data["model"] == "x" and data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "mine"


# ------------------------------------------------------------ agents: messages, loops, turn start
def test_claude_messages_loops_and_turn_started(tmp_path, repo):
    home = tmp_path / "h"
    now = time.time()
    f = claude_file(home, repo)
    done = [
        {"type": "user", "uuid": "u0", "cwd": str(repo), "timestamp": iso(now - 60), "message": {"content": "first"}},
        {"type": "assistant", "uuid": "a0", "cwd": str(repo), "timestamp": iso(now - 59),
         "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Done."}]}},
        {"type": "system", "subtype": "turn_duration", "uuid": "t0", "timestamp": iso(now - 58)},
    ]
    write_jsonl(f, done)
    tr = Tracker([], home=str(home), processes=False)
    tr.poll()
    call = {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}}
    loop = [{"type": "user", "uuid": "u1", "cwd": str(repo), "timestamp": iso(now - 20), "message": {"content": "fix tests"}}]
    for i in range(4):
        loop += [
            {"type": "assistant", "uuid": f"a{i + 1}", "cwd": str(repo), "timestamp": iso(now - 10 + i),
             "message": {"stop_reason": "tool_use", "content": [{"type": "text", "text": f"Attempt {i}: the build still fails."}, call]}},
            {"type": "user", "uuid": f"r{i}", "cwd": str(repo), "timestamp": iso(now - 9 + i),
             "message": {"content": [{"type": "tool_result", "is_error": True}]}},
        ]
    write_jsonl(f, done + loop)
    os.utime(f, (now + 1, now + 1))
    sessions, events = tr.poll()
    s = sessions[0]
    assert s.state == WORKING and s.detail.startswith("loop:")
    assert {"turn_started", "looping"} <= {e.kind for e in events}
    assert [m[0] for m in s.messages] == ["a0", "a1", "a2", "a3", "a4"]
    assert "still fails" in s.messages[-1][1]


# ------------------------------------------------------------ map
def test_cars_drive_park_and_stop(repo):
    lanes = scene.Lanes()
    now = time.time()
    s = session(repo)
    lanes.elapsed(s.key, "working", now - 600)
    moving = scene.lane(s, 60, now, lanes.elapsed(s.key, "working", now))
    s.state = FINISHED
    parked = scene.lane(s, 60, now, lanes.elapsed(s.key, FINISHED, now))
    assert "◐◓◑◒".find(moving[1].plain.split("▝")[1][0]) >= 0, "wheels spin while driving"
    assert "⚑" in parked[0].plain and "●" in parked[1].plain
    assert parked[1].plain.index("▝") > moving[1].plain.index("▝"), "a finished car parks at the flag"
    assert "push" in scene.pipeline_map("repo", "push", now).plain
