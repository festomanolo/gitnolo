"""
Shared fixtures: throwaway repositories and a fake GitHub API backed by a
bare repository that performs real merges, so the full pipeline runs offline.
"""

import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from gitnolo.gitcore import GIT


def git(cwd, *args, check=True):
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com"})
    p = subprocess.run([GIT, *args], cwd=cwd, capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p.stdout


@pytest.fixture(autouse=True)
def personal_edition(monkeypatch):
    """Core tests run against the unrestricted engine in every build."""
    from gitnolo import edition
    monkeypatch.setattr(edition, "EDITION", "personal")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "gitnolo-home"
    home.mkdir()
    monkeypatch.setenv("GITNOLO_HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    import gitnolo.state as st
    monkeypatch.setattr(st, "STATE_FILE", str(home / "state.json"))
    return home


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "commit.gpgsign", "false")
    (path / "app.py").write_text("import os\n\n\ndef main():\n    return 1\n")
    (path / "README.md").write_text("# Demo\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "init")
    return path


class FakeGitHub:
    def __init__(self, bare, private=True):
        self.bare = str(bare)
        self.private = private
        self.prs = {}
        self.issues = []
        self.comments = []
        self.calls = []
        self.block_merge = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def _merge(self, pr):
        head = git(self.bare, "rev-parse", f"refs/heads/{pr['head']}").strip()
        base = git(self.bare, "rev-parse", f"refs/heads/{pr['base']}").strip()
        tree = git(self.bare, "merge-tree", "--write-tree", base, head).split()[0]
        commit = git(self.bare, "commit-tree", tree, "-p", base, "-p", head, "-m", f"Merge pull request #{pr['number']}").strip()
        git(self.bare, "update-ref", f"refs/heads/{pr['base']}", commit, base)
        return commit

    def _handler(fake):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                u = urlparse(self.path)
                fake.calls.append(("GET", u.path))
                if u.path == "/repos/o/r":
                    return self._send(200, {"private": fake.private, "default_branch": "main"})
                if u.path == "/repos/o/r/pulls":
                    q = parse_qs(u.query)
                    head = q.get("head", [""])[0].split(":", 1)[-1]
                    items = [
                        {"number": n, "html_url": f"https://github.com/o/r/pull/{n}", "title": p["title"], "base": {"ref": p["base"]}}
                        for n, p in fake.prs.items() if p["state"] == "open" and (not head or p["head"] == head)
                    ]
                    return self._send(200, items)
                m = re.match(r"^/repos/o/r/pulls/(\d+)$", u.path)
                if m:
                    p = fake.prs[int(m.group(1))]
                    return self._send(200, {"state": p["state"], "merged": p["state"] == "closed", "draft": p["draft"], "node_id": f"PR_{p['number']}"})
                if u.path == "/repos/o/r/issues":
                    return self._send(200, [i for i in fake.issues if i["state"] == "open"])
                if u.path == "/user":
                    return self._send(200, {"login": "o"})
                self._send(404, {"message": "Not Found"})

            def do_POST(self):
                u = urlparse(self.path)
                body = self._body()
                fake.calls.append(("POST", u.path))
                if u.path == "/repos/o/r/pulls":
                    n = len(fake.prs) + len(fake.issues) + 1
                    fake.prs[n] = {"number": n, "title": body["title"], "body": body["body"], "head": body["head"], "base": body["base"],
                                  "state": "open", "draft": bool(body.get("draft"))}
                    return self._send(201, {"number": n, "html_url": f"https://github.com/o/r/pull/{n}", "title": body["title"]})
                if u.path == "/graphql":
                    n = int(body["variables"]["id"].split("_")[1])
                    fake.prs[n]["draft"] = False
                    return self._send(200, {"data": {}})
                m = re.match(r"^/repos/o/r/issues/(\d+)/comments$", u.path)
                if m:
                    fake.comments.append((int(m.group(1)), body["body"]))
                    return self._send(201, {"id": len(fake.comments)})
                if u.path == "/repos/o/r/issues":
                    n = len(fake.prs) + len(fake.issues) + 1
                    it = {"number": n, "title": body["title"], "body": body["body"], "labels": body.get("labels", []), "state": "open",
                          "html_url": f"https://github.com/o/r/issues/{n}"}
                    fake.issues.append(it)
                    return self._send(201, it)
                self._send(404, {"message": "Not Found"})

            def do_PUT(self):
                u = urlparse(self.path)
                self._body()
                fake.calls.append(("PUT", u.path))
                m = re.match(r"^/repos/o/r/pulls/(\d+)/merge$", u.path)
                if m:
                    if fake.block_merge:
                        return self._send(405, {"message": "At least 1 approving review is required"})
                    pr = fake.prs[int(m.group(1))]
                    if pr["draft"]:
                        return self._send(405, {"message": "Pull Request is still a draft"})
                    sha = fake._merge(pr)
                    pr["state"] = "closed"
                    return self._send(200, {"merged": True, "sha": sha})
                self._send(404, {"message": "Not Found"})

            def do_PATCH(self):
                u = urlparse(self.path)
                body = self._body()
                fake.calls.append(("PATCH", u.path))
                m = re.match(r"^/repos/o/r/issues/(\d+)$", u.path)
                if m:
                    it = next((i for i in fake.issues if i["number"] == int(m.group(1))), None)
                    if it is None:
                        return self._send(404, {"message": "Not Found"})
                    it.update({k: v for k, v in body.items() if k in ("state", "title", "body")})
                    return self._send(200, it)
                self._send(404, {"message": "Not Found"})

            def do_DELETE(self):
                u = urlparse(self.path)
                fake.calls.append(("DELETE", u.path))
                m = re.match(r"^/repos/o/r/git/refs/heads/(.+)$", u.path)
                if m:
                    git(fake.bare, "update-ref", "-d", f"refs/heads/{m.group(1)}")
                    self.send_response(204)
                    self.end_headers()
                    return
                self._send(404, {"message": "Not Found"})

        return H


@pytest.fixture
def github_remote(repo, tmp_path, monkeypatch):
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(repo, "remote", "add", "origin", "https://github.com/o/r.git")
    git(repo, "config", f"url.{bare}.insteadOf", "https://github.com/o/r.git")
    git(repo, "push", "-q", "-u", "origin", "main")
    fake = FakeGitHub(bare)
    import gitnolo.github as ghmod
    monkeypatch.setattr(ghmod, "API", fake.url)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    yield fake
    fake.close()
