"""
AI provider selection.

  * openrouter: OpenAI-compatible API. Uses free (":free") models by default and
    a client-side rate guard (per-minute and per-day counters persisted in
    ~/.gitnolo/ai_usage.json) that stops *before* the free-tier limit, so
    gitnolo silently falls back to its instant heuristics instead of erroring.
  * ollama: local model (time-boxed).
  * off: heuristics only.

Commit subjects are always heuristic by default (hundreds per run); the model
is reserved for the few calls where it adds value: PR summary, issue filtering
and conflict synthesis. That is ~2 calls per pipeline run.
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "json", "urllib.error", "urllib.request", "gitnolo.config", "gitnolo.ollama_client",
]

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .config import CONFIG_DIR, AppConfig
from .ollama_client import OllamaClient

OPENROUTER_API = "https://openrouter.ai/api/v1"
USAGE_FILE = os.path.join(CONFIG_DIR, "ai_usage.json")
PREFERRED_FREE = ("llama-3.3-70b", "gemma-3", "mistral-small", "gpt-oss", "glm", "deepseek", "qwen", "llama", "kimi")


class RateGuard:
    """Persistent sliding counters so separate gitnolo processes share one budget."""

    def __init__(self, per_minute: int, per_day: int, path: str = USAGE_FILE):
        self.per_minute = per_minute
        self.per_day = per_day
        self.path = path

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def status(self) -> Dict[str, Any]:
        data = self._load()
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        recent = [t for t in data.get("recent", []) if now - t < 60]
        used_today = data.get("count", 0) if data.get("day") == day else 0
        return {
            "minute": len(recent),
            "today": used_today,
            "cooldown_until": data.get("cooldown_until", 0),
            "per_minute": self.per_minute,
            "per_day": self.per_day,
        }

    def allow(self) -> bool:
        s = self.status()
        if time.time() < s["cooldown_until"]:
            return False
        return s["minute"] < self.per_minute and s["today"] < self.per_day

    def record(self) -> None:
        data = self._load()
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if data.get("day") != day:
            data["day"], data["count"] = day, 0
        data["count"] = data.get("count", 0) + 1
        data["recent"] = [t for t in data.get("recent", []) if now - t < 60] + [now]
        self._save(data)

    def cooldown(self, until: float) -> None:
        data = self._load()
        data["cooldown_until"] = until
        self._save(data)


class OpenRouterClient(OllamaClient):
    """Same task API as OllamaClient, served by OpenRouter."""

    def __init__(self, api_key: str, model: Optional[str], guard: RateGuard):
        super().__init__(OPENROUTER_API, model)
        self.api_key = api_key
        self.guard = guard

    def _request(self, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 30) -> Any:
        req = urllib.request.Request(
            f"{OPENROUTER_API}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method="POST" if body is not None else "GET",
        )
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("HTTP-Referer", "https://github.com/festomanolo/gitnolo")
        req.add_header("X-Title", "gitnolo")
        last: Exception = RuntimeError("no attempt")
        for attempt in range(2):  # free endpoints drop idle connections; retry once
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError:
                raise
            except Exception as e:
                last = e
                time.sleep(1.0)
        raise last

    def check_health(self, timeout: float = 5.0) -> Tuple[bool, List[str]]:
        try:
            data = self._request("/models", timeout=timeout)
            return True, [m.get("id", "") for m in data.get("data", [])]
        except Exception:
            return False, []

    def ensure_model(self, preferred: Optional[List[str]] = None) -> Optional[str]:
        if self.model_name:
            return self.model_name
        cache = os.path.join(CONFIG_DIR, "openrouter_model.json")
        try:
            with open(cache, "r", encoding="utf-8") as f:
                c = json.load(f)
            if time.time() - c.get("t", 0) < 86400 and c.get("model"):
                self.model_name = c["model"]
                return self.model_name
        except Exception:
            pass
        ok, models = self.check_health()
        free = [m for m in models if m.endswith(":free")]
        if not free:
            return None
        ranked = sorted(free, key=lambda m: next((i for i, p in enumerate(PREFERRED_FREE) if p in m.lower()), 99))
        self.model_name = ranked[0]
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump({"model": self.model_name, "t": time.time()}, f)
        except OSError:
            pass
        return self.model_name

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        json_mode: bool = False,
        timeout: float = 60.0,
        max_tokens: int = 400,
    ) -> str:
        timeout = min(timeout, self.remaining())
        if timeout <= 1:
            raise RuntimeError("AI time budget exhausted")
        if not self.guard.allow():
            raise RuntimeError("OpenRouter free-tier guard: limit reached, using heuristics")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "reasoning": {"effort": "low", "exclude": True},  # reasoning models otherwise spend max_tokens thinking
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        self.guard.record()
        try:
            data = self._request("/chat/completions", body, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                reset = e.headers.get("X-RateLimit-Reset")
                until = float(reset) / 1000 if reset and reset.isdigit() else time.time() + 600
                self.guard.cooldown(until)
            raise RuntimeError(f"OpenRouter error {e.code}")
        except Exception as e:
            raise RuntimeError(f"OpenRouter unreachable: {e}")
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter returned no choices")
        return str((choices[0].get("message") or {}).get("content") or "").strip()


def make_client(config: AppConfig) -> Optional[OllamaClient]:
    """Returns the configured AI client, or None when AI is off/unavailable."""
    provider = (config.ai_provider or "auto").lower()
    key = config.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")
    if provider == "off":
        return None
    if provider in ("openrouter", "auto") and key:
        client = OpenRouterClient(key, config.openrouter_model, RateGuard(config.ai_per_minute, config.ai_per_day))
        if client.ensure_model():
            return client
        if provider == "openrouter":
            return None
    if provider in ("ollama", "auto"):
        client = OllamaClient(config.ollama_url, config.model_name)
        if client.ensure_model(config.preferred_models):
            return client
    return None
