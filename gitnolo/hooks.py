"""
Rapid response for Claude Code without a PTY wrapper: a PreToolUse hook that
approves tool calls the moment they are requested, so a session keeps going
while you are away.

  * Destructive shell commands and edits to sensitive paths are never approved:
    the hook answers "ask" and Claude Code shows its normal prompt.
  * Questions (AskUserQuestion) and plan approval (ExitPlanMode) are left alone.
  * scope "edits" approves only reading and editing tools; "all" also approves
    shell commands, web access and MCP tools.

Installed with `gitnolo hooks install` into ~/.claude/settings.json; the hook
process imports nothing heavy, so it adds a few tens of milliseconds per call.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, Optional, Tuple

MARK = "gitnolo.hooks"
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")

LEAVE_TO_USER = {"AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}
EDIT_TOOLS = {"Read", "Glob", "Grep", "LS", "Edit", "MultiEdit", "Write", "NotebookEdit", "NotebookRead", "TodoWrite"}
SENSITIVE_PATH = re.compile(
    r"(?:^|/)\.ssh/|(?:^|/)\.aws/|(?:^|/)\.gnupg/|^/etc/|^/System/|^/usr/|(?:^|/)\.git/(?!info/exclude)|"
    r"(?:^|/)\.env(?:\.(?!example|sample|template)[^/]*)?$|(?:^|/)id_(?:rsa|ed25519|ecdsa)|\.pem$|\.key$"
)


def decide(payload: Dict[str, Any], scope: str = "all") -> Optional[Tuple[str, str]]:
    """(permissionDecision, reason) for a PreToolUse payload, or None to leave Claude Code's default."""
    from .auto_accept import DANGER

    tool = str(payload.get("tool_name") or "")
    inp = payload.get("tool_input") or {}
    if tool in LEAVE_TO_USER:
        return None
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        if DANGER.search(cmd):
            return "ask", "gitnolo: destructive command, approval left to you"
    for key in ("file_path", "notebook_path", "path"):
        path = inp.get(key)
        if isinstance(path, str) and path and tool not in ("Read", "Glob", "Grep", "LS") and SENSITIVE_PATH.search(path):
            return "ask", f"gitnolo: {path} is sensitive, approval left to you"
    if scope == "edits" and tool not in EDIT_TOOLS:
        return None
    return "allow", "gitnolo rapid response"


def run_hook(stdin: Any = None, stdout: Any = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        payload = json.load(stdin)
    except ValueError:
        return 0
    scope = "all"
    try:
        home = os.environ.get("GITNOLO_HOME", os.path.expanduser("~/.gitnolo"))
        with open(os.path.join(home, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not cfg.get("rapid_response", True):
            return 0
        scope = cfg.get("rapid_hook_scope", "all")
    except (OSError, ValueError):
        pass
    verdict = decide(payload, scope)
    if verdict:
        decision, reason = verdict
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                          "permissionDecisionReason": reason}}, stdout)
    return 0


# ------------------------------------------------------------------ install
def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _save(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".gitnolo.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def command() -> str:
    return f"{sys.executable} -m {MARK}"


def installed(path: str = SETTINGS) -> bool:
    groups = _load(path).get("hooks", {}).get("PreToolUse", [])
    return any(MARK in str(h.get("command", "")) for g in groups for h in g.get("hooks", []))


def install(path: str = SETTINGS) -> bool:
    """Adds the hook (idempotent). Returns False when it was already there."""
    data = _load(path)
    if installed(path):
        return False
    groups = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    groups.append({"matcher": "*", "hooks": [{"type": "command", "command": command(), "timeout": 10}]})
    _save(path, data)
    return True


def uninstall(path: str = SETTINGS) -> bool:
    data = _load(path)
    groups = data.get("hooks", {}).get("PreToolUse", [])
    kept = []
    removed = False
    for g in groups:
        hooks = [h for h in g.get("hooks", []) if MARK not in str(h.get("command", ""))]
        removed = removed or len(hooks) != len(g.get("hooks", []))
        if hooks:
            kept.append({**g, "hooks": hooks})
    if not removed:
        return False
    if kept:
        data["hooks"]["PreToolUse"] = kept
    else:
        data["hooks"].pop("PreToolUse", None)
        if not data["hooks"]:
            data.pop("hooks")
    _save(path, data)
    return True


if __name__ == "__main__":
    sys.exit(run_hook())
