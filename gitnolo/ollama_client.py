"""
Local Ollama client. Every call is time-boxed so a slow or missing model can
never stall the pipeline; callers always have a deterministic fallback.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return m.group(1).strip() if m else text


def _loads(text: str) -> Any:
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if m:
            return json.loads(m.group(1))
        raise


class OllamaClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11434", model_name: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.deadline: Optional[float] = None  # absolute time budget shared across calls

    def set_budget(self, seconds: float) -> None:
        self.deadline = time.time() + seconds

    def remaining(self) -> float:
        return 1e9 if self.deadline is None else self.deadline - time.time()

    def check_health(self, timeout: float = 2.0) -> Tuple[bool, List[str]]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return True, [m.get("name", "") for m in data.get("models", [])]
        except Exception:
            return False, []

    def ensure_model(self, preferred: Optional[List[str]] = None) -> Optional[str]:
        healthy, models = self.check_health()
        if not healthy or not models:
            return None
        if self.model_name in models:
            return self.model_name
        for pref in preferred or []:
            if pref in models:
                self.model_name = pref
                return pref
        for m in models:
            if "qwen" in m or "coder" in m:
                self.model_name = m
                return m
        self.model_name = models[0]
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
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "keep_alive": "15m",
            "options": {"temperature": 0.2, "num_predict": max_tokens},
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"
        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8")).get("response", "").strip()
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama unreachable at {self.base_url}: {e}")
        except Exception as e:
            raise RuntimeError(f"Ollama generation failed: {e}")

    # ------------------------------------------------------------- tasks
    def commit_subject(self, path: str, diff_snippet: str, timeout: float = 30.0) -> Optional[str]:
        prompt = (
            "Write ONE Conventional Commit subject line (max 72 chars) for this change.\n"
            "Format: type(scope): imperative summary. Types: feat, fix, refactor, perf, docs, test, build, ci, chore, style.\n"
            "Output only the line.\n\n"
            f"File: {path}\n{diff_snippet[:2500]}"
        )
        out = self.generate(prompt, system="You output a single git commit subject line.", timeout=timeout, max_tokens=40)
        line = out.replace("`", "").strip().strip('"').splitlines()[0].strip() if out.strip() else ""
        if re.match(r"^(feat|fix|refactor|perf|docs|test|build|ci|chore|style|revert)(\([^)]{1,30}\))?!?: .{3,}", line):
            return line[:72]
        return None

    def pr_summary(self, diff_text: str, subjects: List[str], is_private: bool, agent_note: str = "", timeout: float = 30.0) -> Optional[Dict[str, Any]]:
        prompt = (
            "You are preparing a GitHub Pull Request.\n"
            f"Repository visibility: {'private' if is_private else 'public'}.\n"
            "Return JSON: {\"title\": str (<= 70 chars, conventional style), \"summary\": str (2-4 sentences), "
            "\"changes\": [str, ...] (3-8 concise bullets)}.\n"
            + ("Do not mention internal agent tooling.\n" if not is_private else "")
            + (f"\nAgent's own summary:\n{agent_note[:1500]}\n" if agent_note else "")
            + "\nCommit subjects (sample):\n" + "\n".join(subjects[:40])
            + f"\n\nDiff:\n{diff_text[:4000]}"
        )
        out = self.generate(prompt, system="You return strictly valid JSON.", json_mode=True, timeout=timeout, max_tokens=500)
        data = _loads(out)
        if isinstance(data, dict) and data.get("title"):
            return data
        return None

    def refine_issues(self, message: str, candidates: List[str], timeout: float = 20.0) -> Optional[List[Dict[str, Any]]]:
        listing = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates))
        prompt = (
            "An AI coding agent finished a task and wrote the message below. Candidate problem lines were extracted.\n"
            "Keep ONLY candidates that describe an UNRESOLVED problem, bug, missing piece or required follow-up.\n"
            "Drop anything already fixed, passing, or merely descriptive.\n"
            "Return JSON: {\"issues\": [{\"index\": int, \"title\": str (<= 80 chars, imperative), "
            "\"type\": \"bug\"|\"enhancement\"|\"task\"}]}\n\n"
            f"Agent message:\n{message[:5000]}\n\nCandidates:\n{listing}"
        )
        out = self.generate(prompt, system="You return strictly valid JSON.", json_mode=True, timeout=timeout, max_tokens=500)
        data = _loads(out)
        if isinstance(data, dict) and isinstance(data.get("issues"), list):
            return [i for i in data["issues"] if isinstance(i, dict) and isinstance(i.get("index"), int)]
        return None

    def resolve_conflict_ai(
        self,
        file_path: str,
        ours_code: str,
        theirs_code: str,
        context_before: str = "",
        context_after: str = "",
        base_code: Optional[str] = None,
    ) -> Dict[str, str]:
        """Synthesizes a merged hunk that keeps the intent of both sides."""
        prompt = f"Resolve a git merge conflict in `{file_path}`.\n\n"
        if context_before:
            prompt += f"--- Code before the conflict ---\n{context_before}\n\n"
        prompt += f"--- CURRENT (ours) ---\n{ours_code}\n\n--- INCOMING (theirs) ---\n{theirs_code}\n\n"
        if base_code:
            prompt += f"--- COMMON ANCESTOR ---\n{base_code}\n\n"
        if context_after:
            prompt += f"--- Code after the conflict ---\n{context_after}\n\n"
        prompt += (
            "Produce one syntactically valid replacement for the conflicted region that preserves the intent of both sides.\n"
            'Return JSON: {"merged_code": str, "explanation": str (1-2 sentences)}'
        )
        try:
            out = self.generate(prompt, system="You return strictly valid JSON.", json_mode=True, timeout=90, max_tokens=1500)
            data = _loads(out)
            merged = data.get("merged_code", "")
            if merged and "<<<<<<<" not in merged and ">>>>>>>" not in merged:
                return {"merged_code": merged, "explanation": data.get("explanation", "Merged both sides.")}
        except Exception as e:
            return {"merged_code": ours_code + theirs_code, "explanation": f"Model unavailable ({e}); concatenated both sides."}
        return {"merged_code": ours_code + theirs_code, "explanation": "Model output unusable; concatenated both sides."}
