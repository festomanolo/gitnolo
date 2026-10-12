"""`gitnolo release`: build (Vite/npm, Python, Cargo, or existing dist) and publish a GitHub release with the artifacts."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from typing import List, Optional, Tuple

from . import ui
from .config import AppConfig
from .gitcore import GitError, Repo
from .onboard import Rail

BUNDLE_EXT = (".dmg", ".pkg", ".exe", ".msi", ".deb", ".rpm", ".AppImage", ".whl", ".tar.gz", ".zip", ".apk", ".vsix")


def _pm(root: str) -> str:
    for lock, pm in (("bun.lockb", "bun"), ("bun.lock", "bun"), ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn")):
        if os.path.exists(os.path.join(root, lock)):
            return pm
    return "npm"


def detect(root: str) -> Tuple[str, Optional[List[str]]]:
    """(kind, build command)"""
    pj = os.path.join(root, "package.json")
    if os.path.isfile(pj):
        try:
            pkg = json.load(open(pj))
        except Exception:
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        pm = _pm(root)
        if "build" in pkg.get("scripts", {}):
            kind = "vite" if "vite" in deps else "node"
            return kind, [pm, "run", "build"]
    if os.path.isfile(os.path.join(root, "pyproject.toml")):
        return "python", ["python3", "-m", "build"]
    if os.path.isfile(os.path.join(root, "Cargo.toml")):
        return "rust", ["cargo", "build", "--release"]
    return "prebuilt", None


def collect(root: str, kind: str, name: str, tag: str) -> List[str]:
    found: List[str] = []
    if kind in ("vite", "node"):
        for d in ("dist", "build"):
            full = os.path.join(root, d)
            if os.path.isdir(full):
                out = os.path.join(tempfile.mkdtemp(), f"{name}-{tag}-{d}.zip")
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                    for dp, _, fs in os.walk(full):
                        for f in fs:
                            p = os.path.join(dp, f)
                            z.write(p, os.path.relpath(p, full))
                found.append(out)
                break
    if kind == "rust":
        for p in glob.glob(os.path.join(root, "target", "release", "*")):
            if os.path.isfile(p) and os.access(p, os.X_OK) and "." not in os.path.basename(p):
                found.append(p)
    # agent-built bundles / installers anywhere common
    for d in ("dist", "release", "releases", "out", "bundle", "src-tauri/target/release/bundle", "target/release/bundle"):
        for dp, _, fs in os.walk(os.path.join(root, d)):
            for f in fs:
                if f.endswith(BUNDLE_EXT) and os.path.join(dp, f) not in found:
                    found.append(os.path.join(dp, f))
    return found


def project_info(root: str) -> Tuple[str, Optional[str]]:
    """(name, version) from the manifest, so tag and artifact names match what the project says."""
    name, ver = os.path.basename(root), None

    def grab(path: str, pat: str) -> Optional[str]:
        try:
            m = re.search(pat, open(path).read(), re.M)
            return m.group(1) if m else None
        except OSError:
            return None

    pj = os.path.join(root, "package.json")
    if os.path.isfile(pj):
        try:
            d = json.load(open(pj))
            name, ver = (d.get("name") or name).split("/")[-1], d.get("version")
        except Exception:
            pass
    for f, in (("pyproject.toml",), ("Cargo.toml",), ("src-tauri/Cargo.toml",)):
        if ver:
            break
        path = os.path.join(root, f)
        ver = grab(path, r'^version\s*=\s*"([^"]+)"')
        name = grab(path, r'^name\s*=\s*"([^"]+)"') or name if ver else name
    return name, ver


_VER = re.compile(r"[-_.]?v?\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.]+)?")


def sync_names(files: List[str], name: str, ver: str) -> List[str]:
    """Copies artifacts to `<name>-<ver>[-platform].<ext>` so binaries match the tag."""
    out_dir, out = tempfile.mkdtemp(), []
    for f in files:
        base = os.path.basename(f)
        ext = next((e for e in (".tar.gz", ".AppImage") if base.endswith(e)), os.path.splitext(base)[1])
        rest = _VER.sub("", base[: len(base) - len(ext)])
        rest = re.sub(re.escape(name), "", rest, flags=re.I).strip("-_. ")
        new = f"{name}-{ver}" + (f"-{rest}" if rest else "") + ext
        dst = os.path.join(out_dir, new)
        shutil.copy2(f, dst)
        out.append(dst)
    return out


def next_tag(repo: Repo) -> str:
    last = repo.run("describe", "--tags", "--abbrev=0", check=False).strip()
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", last or "")
    return f"v{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}" if m else "v0.1.0"


def run(config: AppConfig, tag: Optional[str] = None, no_build: bool = False, yes: bool = False) -> int:
    from .gitlab import GitLab
    from .github import GitHub, resolve_token

    repo = Repo(".")
    root = repo.run("rev-parse", "--show-toplevel").strip()
    r = Rail("gitnolo release")
    kind, cmd = detect(root)
    name, ver = project_info(root)
    existing = set(repo.run("tag", "--list", check=False).split())
    if not tag:
        tag = f"v{ver}" if ver else next_tag(repo)
        if tag in existing:
            tag = next_tag(repo)
            r.note(f"manifest version {ver} is already released · using {tag} (bump the manifest to keep them in sync)", "warn")
    elif tag in existing:
        r.end(f"Tag {tag} already exists")
        return 1
    ver = tag.lstrip("v")
    r.dot(f"Project: {kind} · {os.path.basename(root)} · {tag}")
    r.bar()

    if cmd and not no_build:
        r.dot(f"Build  {' '.join(cmd)}")
        if not shutil.which(cmd[0]):
            r.note(f"{cmd[0]} not found", "err")
            r.end("Install it, or use --no-build")
            return 1
        if kind in ("vite", "node") and not os.path.isdir(os.path.join(root, "node_modules")):
            r.note("installing dependencies…")
            subprocess.run([cmd[0], "install"], cwd=root, capture_output=True)
        with ui.console.status("", spinner="dots"):
            p = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
        if p.returncode:
            for line in (p.stdout + p.stderr).strip().splitlines()[-8:]:
                r.note(line, "err")
            r.end("Build failed · nothing published")
            return 1
        r.note("build ok", "ok")
        r.bar()

    files = collect(root, kind, name, tag)
    if not files:
        r.dot("No artifacts found", "warn", "▲")
        r.end("Build something first (dist/, bundles) or ask your agent to")
        return 1
    files = sync_names(files, name, ver)
    r.dot("Artifacts (named to match the tag)")
    for f in files:
        r.note(f"{os.path.basename(f)}  ({os.path.getsize(f) / 1e6:.1f} MB)", "path")
    r.bar()

    url = repo.remote_url()
    forge = GitLab.for_remote(url, config.gitlab_token) if url else None
    if not forge or type(forge) is not GitHub or not forge.token:
        r.end("Releases need a GitHub remote and token (see `gitnolo doctor`)")
        return 1
    r.dot(f"Publish {tag} to github.com/{forge.slug}", "info", "◆")
    if not yes and r.ask("Are these the right binaries?", "Y/n", "n") not in ("y", ""):
        r.end("Cancelled · nothing published")
        return 0
    if not yes and r.ask("Create release now?", "Y/n", "n") not in ("y", ""):
        r.end("Cancelled")
        return 0
    try:
        repo.run("tag", tag, check=False)
        repo.run("push", "origin", tag, check=False)
        rel = forge.create_release(tag, tag)
        for f in files:
            r.note(f"uploading {os.path.basename(f)}…")
            forge.upload_asset(rel, f)
    except Exception as e:
        r.end(f"Failed: {e}")
        return 1
    r.end(rel.get("html_url", "Released"))
    return 0
