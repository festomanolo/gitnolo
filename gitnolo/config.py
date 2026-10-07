"""
Persistent configuration (~/.gitnolo/config.json).
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "json",
]

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

CONFIG_DIR = os.environ.get("GITNOLO_HOME", os.path.expanduser("~/.gitnolo"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_PREFERRED_MODELS = [
    "qwen2.5-coder:14b",
    "qwen2.5-coder:7b",
    "qwen2.5:7b",
    "qwen2.5:3b",
    "qwen2.5:1.5b",
]

DEFAULT_MONITORED_AGENTS = [
    "claude",
    "agy",
    "kiro-cli",
    "kiro",
    "codex",
    "gemini",
    "aider",
    "cursor-agent",
    "opencode",
    "amp",
    "goose",
]


@dataclass
class AppConfig:
    # AI
    ai_provider: str = "auto"           # auto | openrouter | ollama | off
    openrouter_api_key: Optional[str] = None
    openrouter_model: Optional[str] = None  # None = best available ":free" model
    ai_per_minute: int = 15              # client-side guard, below OpenRouter's free limit (20/min)
    ai_per_day: int = 45                 # below the free daily cap (50/day without credits)
    ollama_url: str = DEFAULT_OLLAMA_URL
    model_name: Optional[str] = None
    preferred_models: List[str] = field(default_factory=lambda: list(DEFAULT_PREFERRED_MODELS))
    ai_pr_summary: bool = True          # use the local model for PR title/summary
    ai_commit_messages: bool = False    # per-commit AI subjects (public repos only); heuristics are instant
    ai_issue_refine: bool = True        # let the model filter/clean extracted issues
    ai_budget_seconds: float = 25.0     # total model time allowed per pipeline run

    # Agents
    monitored_agents: List[str] = field(default_factory=lambda: list(DEFAULT_MONITORED_AGENTS))
    settle_seconds: float = 3.0          # quiet period after a finished turn before committing
    fallback_idle_seconds: float = 90.0  # agents without transcripts: commit after this much quiet
    transcript_horizon_hours: float = 6.0
    catch_up: bool = False               # on start, also process turns that finished before gitnolo ran

    # Commits
    private_commit_min: int = 100        # private repos: a fresh random count in [min, target] per change
    private_commit_target: int = 600     # upper bound for private repos
    public_commit_min: int = 15
    public_commit_max: int = 15
    repo_commit_targets: Dict[str, int] = field(default_factory=dict)  # repo root -> commits, overrides policy

    # GitHub
    auto_push: bool = True
    auto_pr: bool = True
    auto_merge: bool = True
    merge_method: str = "merge"          # merge keeps every micro-commit in history
    delete_branch_after_merge: bool = True
    auto_issues: bool = True
    max_issues_per_turn: int = 5
    github_token: Optional[str] = None
    gitlab_token: Optional[str] = None

    # Live issues: snapped from agent messages as they stream, closed when the agent resolves them
    live_issues: bool = True
    live_issue_grace_seconds: float = 30.0   # resolved within this window -> never published
    auto_close_issues: bool = True

    # Rapid response (gitnolo supervise / Claude Code hook)
    rapid_response: bool = True
    rapid_away_seconds: float = 45.0     # no keystrokes for this long = you are away
    rapid_choice_delay: float = 15.0     # questions get option 1 after this long, only while away
    rapid_hook_scope: str = "all"        # all | edits: what the Claude Code hook may approve

    # Safety nets
    checkpoints: bool = True             # snapshot the tree when an agent starts a turn (gitnolo rewind)
    checkpoint_keep: int = 60
    guard_tests: bool = True             # deleted/skipped tests or dropped assertions block auto-merge
    guard_secrets: bool = True           # files containing API keys / private keys are never committed

    # Experience
    show_map: bool = True                # animated lanes and pipeline map in `watch`
    interactive: bool = True
    notify: bool = True
    animations_enabled: bool = True
    max_diff_chars: int = 12000

    # Back-compat with older config files
    debounce_seconds: float = 4.0

    @classmethod
    def load(cls) -> "AppConfig":
        config = cls()
        if os.path.isfile(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                valid = {f.name for f in fields(cls)}
                for key, val in data.items():
                    if key in valid:
                        setattr(config, key, val)
            except Exception:
                pass
        return config

    def save(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for secret in ("github_token", "gitlab_token", "openrouter_api_key"):
            if d.get(secret):
                d[secret] = "********"
        return d

    def set_value(self, key: str, raw: str) -> Any:
        """Sets a config value from a string, coercing to the field's type."""
        field_map = {f.name: f for f in fields(self)}
        if key not in field_map:
            raise KeyError(key)
        current = getattr(self, key)
        if isinstance(current, bool):
            val: Any = raw.strip().lower() in ("1", "true", "yes", "on", "y")
        elif isinstance(current, int) and not isinstance(current, bool):
            val = int(raw)
        elif isinstance(current, float):
            val = float(raw)
        elif isinstance(current, list):
            val = [x.strip() for x in raw.split(",") if x.strip()]
        elif raw.strip().lower() in ("", "none", "null"):
            val = None
        else:
            val = raw
        setattr(self, key, val)
        return val

    def resolve_model(self, available_models: List[str]) -> Optional[str]:
        if not available_models:
            return self.model_name
        if self.model_name and self.model_name in available_models:
            return self.model_name
        for pref in self.preferred_models:
            if pref in available_models:
                return pref
        for avail in available_models:
            if "qwen" in avail.lower() or "coder" in avail.lower():
                return avail
        return available_models[0]
