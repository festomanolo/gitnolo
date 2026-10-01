import os
import stat

import pytest

from gitnolo import microcommit as mc
from gitnolo.gitcore import Repo
from tests.conftest import git


def apply(repo_path, res):
    r = Repo(str(repo_path))
    r.update_ref("refs/heads/main", res.tip, res.base)
    r.sync_index(res.paths)
    return r


def big_change(path, n=120):
    lines = ["import os", "import sys", ""]
    for i in range(n):
        lines += [f"def fn{i}(x):", f"    return x * {i}", ""]
    (path / "app.py").write_text("\n".join(lines) + "\n")


def test_final_tree_matches_worktree_for_mixed_changes(repo):
    big_change(repo, 30)
    (repo / "README.md").unlink()
    (repo / "dir with space").mkdir()
    (repo / "dir with space" / "ünïcode file.txt").write_text("hello\nworld")  # no trailing newline
    (repo / "bin.dat").write_bytes(bytes(range(256)) * 4)
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    r = Repo(str(repo))
    before = r.run("status", "--porcelain")
    p = mc.plan(r, 50)
    res = mc.execute(p)  # verification is built in; raises on mismatch
    assert res.count == len(p.commits) == 50
    assert r.run("status", "--porcelain") == before, "execute must not touch worktree or index"
    apply(repo, res)
    assert r.run("status", "--porcelain", "--untracked-files=all").strip() == ""
    assert r.run("ls-tree", "HEAD", "run.sh").split()[0] == "100755"


def test_hits_exact_private_target_when_enough_lines(repo):
    big_change(repo, 250)
    r = Repo(str(repo))
    p = mc.plan(r, 600)
    assert p.max_possible >= 600
    res = mc.execute(p)
    assert res.count == 600
    apply(repo, res)
    assert r.commit_count("HEAD") == 601
    # no empty commits: every commit changes something
    empties = r.run("log", "--format=%H", "HEAD~600..HEAD").split()
    for sha in empties[:50]:
        assert r.run("diff-tree", "--no-commit-id", "--name-only", "-r", sha).strip()


def test_caps_at_max_possible_for_small_change(repo):
    (repo / "app.py").write_text("import os\n\n\ndef main():\n    return 2\n")
    r = Repo(str(repo))
    p = mc.plan(r, 600)
    assert len(p.commits) == p.max_possible == 1


def test_groups_many_files_into_small_target(repo):
    for i in range(12):
        (repo / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    r = Repo(str(repo))
    p = mc.plan(r, 3)
    assert len(p.commits) == 3
    res = mc.execute(p)
    apply(repo, res)
    assert r.run("status", "--porcelain").strip() == ""


def test_public_range_splits_single_file(repo):
    big_change(repo, 40)
    r = Repo(str(repo))
    p = mc.plan(r, 20)
    assert len(p.commits) == 20
    subjects = [c.subject for c in p.commits]
    assert len(set(subjects)) == len(subjects), "subjects should be unique"
    assert all(s.split("(")[0] in {"feat", "refactor", "fix", "chore", "docs", "style", "test", "build", "ci"} for s in subjects)


def test_secrets_are_never_committed(repo):
    (repo / ".env").write_text("API_KEY=abc\n")
    (repo / "server.pem").write_text("-----BEGIN-----\n")
    (repo / ".env.example").write_text("API_KEY=\n")
    r = Repo(str(repo))
    p = mc.plan(r, 10)
    paths = {f.path for f in p.files}
    assert ".env" not in paths and "server.pem" not in paths
    assert ".env.example" in paths
    assert {s for s, _ in p.skipped} == {".env", "server.pem"}


def test_initial_commit_in_empty_repo(tmp_path):
    path = tmp_path / "empty"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "T")
    git(path, "config", "user.email", "t@e.com")
    (path / "a.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n")
    r = Repo(str(path))
    p = mc.plan(r, 4)
    res = mc.execute(p)
    r.update_ref("refs/heads/main", res.tip, None)
    r.sync_index(res.paths)
    assert r.commit_count("HEAD") == 4
    assert r.run("status", "--porcelain").strip() == ""


def test_intermediate_states_are_prefixes_of_final(repo):
    """Each step only moves lines toward the final content (no invented text)."""
    (repo / "notes.txt").write_text("".join(f"line {i}\n" for i in range(30)))
    r = Repo(str(repo))
    p = mc.plan(r, 10)
    res = mc.execute(p)
    final = (repo / "notes.txt").read_text().splitlines()
    shas = r.run("rev-list", "--reverse", f"{res.base}..{res.tip}").split()
    prev = 0
    for sha in shas:
        content = r.run("show", f"{sha}:notes.txt").splitlines()
        assert content == final[: len(content)]
        assert len(content) > prev
        prev = len(content)
