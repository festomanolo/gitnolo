"""
Agent intelligence: knows which agent is working in which repository, and
exactly when a task (turn) finishes, stalls, waits for approval, or stops.

Sources, strongest first:
  * Claude Code transcripts  ~/.claude/projects/*/*.jsonl
      turn end = `system/turn_duration` (or a trailing assistant `end_turn`)
  * Kiro sessions            ~/.kiro/sessions/*/sess_*/messages.jsonl
      turn end = `turn_end`; waiting = unresolved `pending_interaction`
  * Antigravity transcripts  ~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl
      turn end = final MODEL PLANNER_RESPONSE with content and no tool calls
  * Process table (any CLI agent, incl. codex/gemini/aider): start, cwd, exit.

Transcript files are only re-read when their size/mtime changes, and only the
tail is parsed, so a scan costs a few stat() calls in the steady state.
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "glob", "json", "re", "gitnolo.gitcore",
]

import glob
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .gitcore import find_root

WORKING = "working"
WAITING = "waiting"
FINISHED = "finished"
INTERRUPTED = "stopped"
STALLED = "stalled"
EXITED = "exited"
IDLE = "idle"
LIMITED = "limited"   # usage / rate limit hit; resumes later
ERRORED = "error"     # network, server, auth or tool failure ended the turn
CLOSED = "closed"     # agent process vanished mid-task
RUNNING = "running"  # process-only knowledge

# Why a turn ended (Session.stop_reason)
R_COMPLETED = "completed"
R_USER = "user interrupt"
R_LIMIT = "usage limit"
R_NETWORK = "network"
R_SERVER = "server error"
R_AUTH = "auth expired"
R_STUCK = "stuck in a loop"
R_CLOSED = "closed mid-task"
R_CANCELLED = "cancelled"
R_ERROR = "error"
PARTIAL_REASONS = {R_USER, R_LIMIT, R_NETWORK, R_SERVER, R_AUTH, R_CLOSED, R_CANCELLED, R_ERROR}

NETWORK_RE = re.compile(
    r"ENOTFOUND|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENETUNREACH|EHOSTUNREACH|socket hang up|"
    r"unable to connect|connection (?:closed|dropped|refused|reset|error)|network|timed? ?out|TLS handshake|"
    r"certificate|no such host|read tcp|dial tcp|offline",
    re.I,
)
LIMIT_RE = re.compile(r"usage limit|session limit|rate.?limit|quota|RESOURCE_EXHAUSTED|too many requests|\b429\b|limit reached|credits", re.I)
AUTH_RE = re.compile(r"/login|not logged in|login expired|unauthori[sz]ed|\b401\b|invalid api key|authentication|token expired", re.I)
SERVER_RE = re.compile(r"overloaded|\b5\d\d\b|UNAVAILABLE|internal server error|service (?:is )?unavailable|api error", re.I)
RESET_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)


def classify_error(text: str, kind: str = "") -> str:
    """Maps an error message (and optional typed kind) to a stop reason."""
    k = (kind or "").lower()
    if k == "rate_limit" or LIMIT_RE.search(text):
        return R_LIMIT
    if k in ("authentication_failed", "auth") or AUTH_RE.search(text):
        return R_AUTH
    if NETWORK_RE.search(text):
        return R_NETWORK
    if k == "server_error" or SERVER_RE.search(text):
        return R_SERVER
    return R_ERROR


def parse_reset(text: str, now: Optional[float] = None) -> Optional[float]:
    """'resets 10:30am' -> next local timestamp of that wall-clock time."""
    m = RESET_RE.search(text or "")
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    now = now or time.time()
    lt = time.localtime(now)
    ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
    if ts <= now - 60:
        ts += 86400
    return ts

TAIL_BYTES = 768 * 1024
MAX_MESSAGES = 40
LOOP_REPEATS = 4        # the same tool call this many times in the last LOOP_WINDOW calls = a loop
LOOP_WINDOW = 10
LOOP_ERRORS = 3         # this many failed tool calls in a row = a loop
USER_GATED_TOOLS = {"AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}


@dataclass
class Session:
    key: str
    agent: str
    session_id: str
    cwd: str
    repo: Optional[str]
    state: str
    updated: float
    title: str = ""
    turn_id: str = ""
    final_message: str = ""
    detail: str = ""
    pid: Optional[int] = None
    source: str = ""
    started: float = 0.0
    stop_reason: str = ""
    stop_detail: str = ""
    resume_at: Optional[float] = None
    messages: List[Tuple[str, str, float]] = field(default_factory=list)  # (id, text, ts) of recent agent messages

    @property
    def partial(self) -> bool:
        """True when the turn ended without the agent completing its work."""
        return self.stop_reason in PARTIAL_REASONS

    @property
    def repo_name(self) -> str:
        return os.path.basename(self.repo) if self.repo else os.path.basename(self.cwd)

    @property
    def idle(self) -> float:
        return max(0.0, time.time() - self.updated)


@dataclass
class AgentEvent:
    kind: str  # "turn_finished" | "turn_started" | "waiting" | "stopped" | "stalled" | "looping" | "exited" | "started" | ...
    session: Session
    at: float = field(default_factory=time.time)


# ---------------------------------------------------------------- helpers
_root_cache: Dict[str, Optional[str]] = {}
_canon: Dict[Tuple[int, int], str] = {}


def repo_for(path: Optional[str]) -> Optional[str]:
    """Git root for a path, canonicalised by inode so case variants on macOS collapse."""
    if not path:
        return None
    if path in _root_cache:
        return _root_cache[path]
    root = None
    if os.path.isdir(path):
        home = os.path.expanduser("~")
        r = find_root(path)
        if r and os.path.abspath(r) not in ("/", home):
            try:
                st = os.stat(r)
                root = _canon.setdefault((st.st_dev, st.st_ino), r)
            except OSError:
                root = r
    _root_cache[path] = root
    return root


def read_tail_lines(path: str, max_bytes: int = TAIL_BYTES) -> List[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    if start > 0:
        nl = data.find(b"\n")
        data = data[nl + 1 :] if nl >= 0 else b""
    return data.decode("utf-8", "replace").splitlines()


def _json_lines(lines: Iterable[str]) -> List[Dict[str, Any]]:
    out = []
    for l in lines:
        l = l.strip()
        if not l:
            continue
        try:
            d = json.loads(l)
            if isinstance(d, dict):
                out.append(d)
        except ValueError:
            continue
    return out


def _parse_ts(ts: Any) -> Optional[float]:
    if not isinstance(ts, str):
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class _FileCache:
    def __init__(self) -> None:
        self.sig: Dict[str, Tuple[float, int]] = {}
        self.sessions: Dict[str, Session] = {}

    def fresh(self, path: str, st: os.stat_result) -> bool:
        return self.sig.get(path) == (st.st_mtime, st.st_size)

    def put(self, path: str, st: os.stat_result, session: Optional[Session]) -> None:
        self.sig[path] = (st.st_mtime, st.st_size)
        if session:
            self.sessions[path] = session
        else:
            self.sessions.pop(path, None)


# ---------------------------------------------------------------- adapters
class ClaudeCodeAdapter:
    agent = "claude"

    def __init__(self, home: Optional[str] = None):
        self.root = os.path.join(home or os.path.expanduser("~"), ".claude", "projects")
        self.cache = _FileCache()
        self.titles: Dict[str, str] = {}

    def files(self) -> List[str]:
        return glob.glob(os.path.join(glob.escape(self.root), "*", "*.jsonl"))

    def scan(self, horizon: float) -> List[Session]:
        now = time.time()
        out = []
        for path in self.files():
            try:
                st = os.stat(path)
            except OSError:
                continue
            if now - st.st_mtime > horizon:
                continue
            if not self.cache.fresh(path, st):
                self.cache.put(path, st, self.parse(path, st.st_mtime))
            s = self.cache.sessions.get(path)
            if s:
                out.append(self.refresh(s))
        return out

    @staticmethod
    def refresh(s: Session) -> Session:
        """Time-dependent state transitions without re-reading the file."""
        idle = time.time() - s.updated
        if s.state in (WORKING, WAITING) and idle > 900:
            s.state = STALLED
        elif s.state == WORKING and s.detail.startswith("tool:") and idle > 30:
            s.state = WAITING
        return s

    def parse(self, path: str, mtime: float) -> Optional[Session]:
        entries = _json_lines(read_tail_lines(path))
        if not entries:
            return None
        sid = os.path.splitext(os.path.basename(path))[0]
        cwd = ""
        title = self.titles.get(path, "")
        for e in reversed(entries):
            if not cwd and isinstance(e.get("cwd"), str):
                cwd = e["cwd"]
            if not title and e.get("type") == "ai-title" and e.get("aiTitle"):
                title = str(e["aiTitle"])
            if not title and e.get("type") == "summary" and e.get("summary"):
                title = str(e["summary"])
            if cwd and title:
                break
        if title:
            self.titles[path] = title
        if not cwd:
            return None

        relevant = [
            e for e in entries
            if e.get("type") in ("assistant", "user", "system") and not e.get("isSidechain")
            and not (e.get("type") == "system" and e.get("subtype") not in ("turn_duration",))
            and not e.get("isMeta")
        ]
        if not relevant:
            return None
        last = relevant[-1]
        state = WORKING
        turn_id = ""
        detail = ""
        final = ""
        msg = last.get("message") or {}
        ltype = last.get("type")

        stop_reason = stop_detail = ""
        resume_at = None
        api_err = self._turn_api_error(relevant)
        if ltype == "system" and last.get("subtype") == "turn_duration":
            state = FINISHED
            stop_reason = R_COMPLETED
            turn_id = f"claude:{sid}:{last.get('uuid') or last.get('timestamp')}"
            final = self._final_text(relevant[:-1])
        elif ltype == "assistant":
            stop = msg.get("stop_reason")
            blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
            tools = [b.get("name", "") for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
            if stop == "end_turn" and not tools or last.get("isApiErrorMessage"):
                state = FINISHED
                stop_reason = R_COMPLETED
                turn_id = f"claude:{sid}:{last.get('uuid') or last.get('timestamp')}"
                final = self._final_text(relevant)
            elif tools:
                detail = f"tool:{tools[-1]}"
                if tools[-1] in USER_GATED_TOOLS:
                    state = WAITING
                    detail = f"needs input:{tools[-1]}"
        elif ltype == "user":
            content = msg.get("content")
            text = content if isinstance(content, str) else " ".join(
                str(b.get("text", "")) for b in (content or []) if isinstance(b, dict)
            )
            if "[Request interrupted by user" in text:
                state = INTERRUPTED
                stop_reason = R_USER
                turn_id = f"claude:{sid}:{last.get('uuid')}:interrupted"
                final = self._final_text(relevant)
            else:
                detail = "thinking"

        if state == WORKING:
            loop = self._loop(relevant)
            if loop:
                detail = f"loop:{loop}"
        if api_err and state == FINISHED:
            err_text, err_kind, err_uuid = api_err
            stop_reason = classify_error(err_text, err_kind)
            stop_detail = err_text[:120]
            state = LIMITED if stop_reason == R_LIMIT else ERRORED
            resume_at = parse_reset(err_text) if stop_reason == R_LIMIT else None
            turn_id = f"claude:{sid}:{err_uuid}:{stop_reason}"
            final = ""
        updated = _parse_ts(last.get("timestamp")) or mtime
        return Session(
            stop_reason=stop_reason,
            stop_detail=stop_detail,
            resume_at=resume_at,
            key=f"claude:{sid}",
            agent="claude",
            session_id=sid,
            cwd=cwd,
            repo=repo_for(cwd),
            state=state,
            updated=updated,
            title=title,
            turn_id=turn_id,
            final_message=final,
            detail=detail,
            source=path,
            messages=self._messages(relevant),
        )

    @staticmethod
    def _messages(entries: List[Dict[str, Any]]) -> List[Tuple[str, str, float]]:
        out: List[Tuple[str, str, float]] = []
        for e in entries:
            if e.get("type") != "assistant" or e.get("isApiErrorMessage"):
                continue
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list):
                text = "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
            else:
                text = str(content or "")
            if text.strip():
                out.append((str(e.get("uuid") or e.get("timestamp")), text.strip(), _parse_ts(e.get("timestamp")) or 0.0))
        return out[-MAX_MESSAGES:]

    @staticmethod
    def _loop(entries: List[Dict[str, Any]]) -> str:
        """Describes a loop in the current turn: repeated identical tool calls or a run of failures."""
        calls: List[str] = []
        names: List[str] = []
        errors = 0
        streak = True  # still counting the trailing run of failed tool results
        for e in reversed(entries):
            content = (e.get("message") or {}).get("content")
            if e.get("type") == "user":
                if isinstance(content, str) or not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content or []):
                    break  # the prompt that started this turn
                for b in reversed(content):
                    if streak and isinstance(b, dict) and b.get("type") == "tool_result":
                        if b.get("is_error"):
                            errors += 1
                        else:
                            streak = False
            elif e.get("type") == "assistant" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        names.append(str(b.get("name", "")))
                        calls.append(names[-1] + json.dumps(b.get("input"), sort_keys=True, default=str))
            if len(calls) >= LOOP_WINDOW:
                break
        if errors >= LOOP_ERRORS:
            return f"{errors} failed tool calls in a row"
        if calls:
            sig = max(set(calls), key=calls.count)
            n = calls.count(sig)
            if n >= LOOP_REPEATS:
                return f"same {names[calls.index(sig)]} call {n}x"
        return ""

    @staticmethod
    def _turn_api_error(entries: List[Dict[str, Any]]) -> Optional[Tuple[str, str, str]]:
        """(text, kind, uuid) if the current turn's last assistant message is an API error."""
        for e in reversed(entries):
            t = e.get("type")
            if t == "assistant":
                if not e.get("isApiErrorMessage"):
                    return None
                content = (e.get("message") or {}).get("content")
                text = " ".join(
                    str(b.get("text", "")) for b in content if isinstance(b, dict)
                ) if isinstance(content, list) else str(content or "")
                return text.strip(), str(e.get("error") or ""), str(e.get("uuid") or e.get("timestamp"))
            if t == "user":
                content = (e.get("message") or {}).get("content")
                if isinstance(content, str) or not any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in (content or [])
                ):
                    return None  # reached the prompt that started this turn
        return None

    @staticmethod
    def _final_text(entries: List[Dict[str, Any]]) -> str:
        """Text of the last assistant message(s) in the turn."""
        texts: List[str] = []
        for e in reversed(entries):
            if e.get("type") == "user":
                msg = e.get("message") or {}
                content = msg.get("content")
                is_tool_result = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                )
                if not is_tool_result:
                    break
                if texts:
                    break
                continue
            if e.get("type") != "assistant":
                continue
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list):
                chunk = "\n".join(
                    str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
                if chunk:
                    texts.append(chunk)
            elif isinstance(content, str) and content.strip():
                texts.append(content.strip())
        return "\n\n".join(reversed(texts))[:20000]


class KiroAdapter:
    agent = "kiro"

    def __init__(self, home: Optional[str] = None):
        self.root = os.path.join(home or os.path.expanduser("~"), ".kiro", "sessions")
        self.cache = _FileCache()

    def scan(self, horizon: float) -> List[Session]:
        now = time.time()
        out = []
        for path in glob.glob(os.path.join(glob.escape(self.root), "*", "sess_*", "messages.jsonl")):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if now - st.st_mtime > horizon:
                continue
            if not self.cache.fresh(path, st):
                self.cache.put(path, st, self.parse(path, st.st_mtime))
            s = self.cache.sessions.get(path)
            if s:
                if s.state == WORKING and time.time() - s.updated > 900:
                    s.state = STALLED
                out.append(s)
        return out

    def parse(self, path: str, mtime: float) -> Optional[Session]:
        meta_path = os.path.join(os.path.dirname(path), "session.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
        paths = meta.get("workspacePaths") or meta.get("rootPaths") or []
        cwd = paths[0] if paths else ""
        if not cwd:
            return None
        sid = meta.get("id") or os.path.basename(os.path.dirname(path))
        entries = _json_lines(read_tail_lines(path))
        payloads: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for e in entries:
            p = e.get("payload")
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except ValueError:
                    p = None
            if isinstance(p, dict):
                payloads.append((e, p))
        ignore = {"session_start", "session_metadata", "usage_summary", "session_event"}
        relevant = [(e, p) for e, p in payloads if p.get("type") not in ignore]
        state, turn_id, final, detail = WORKING, "", "", ""
        stop_reason = stop_detail = ""
        if not any(q.get("type") in ("user", "turn_start") for _, q in relevant):
            state = IDLE
        elif relevant:
            e, p = relevant[-1]
            t = p.get("type")
            if t == "turn_end":
                state = FINISHED
                reason = str(p.get("stopReason") or "end_turn")
                turn_id = f"kiro:{sid}:{e.get('id') or p.get('executionId')}"
                usage = next((q for _, q in reversed(payloads) if q.get("type") == "usage_summary"), {})
                if reason == "cancelled" or usage.get("status") == "aborted":
                    state, stop_reason = INTERRUPTED, R_USER
                elif reason == "error" or usage.get("status") == "failed":
                    err_text = " ".join(str(q.get("content") or q.get("message") or "") for _, q in relevant[-4:])
                    stop_reason = classify_error(err_text)
                    state = LIMITED if stop_reason == R_LIMIT else ERRORED
                    stop_detail = err_text.strip()[:120]
                else:
                    stop_reason = R_COMPLETED
                says = []
                for _, q in reversed(relevant[:-1]):
                    if q.get("type") == "assistant" and q.get("content"):
                        says.append(str(q["content"]))
                        if len(says) >= 3:
                            break
                    elif q.get("type") in ("user", "turn_start"):
                        break
                final = "\n\n".join(reversed(says))
            else:
                resolved = {q.get("toolCallId") for _, q in relevant if q.get("type") == "interaction_resolved"}
                pending = [
                    q for _, q in relevant if q.get("type") == "pending_interaction" and q.get("toolCallId") not in resolved
                ]
                if pending:
                    state = WAITING
                    q = pending[-1]
                    detail = f"approval:{str(q.get('question', ''))[:60]}"
                    if str(q.get("toolCallId", "")).startswith("failure-intervention"):
                        stop_reason = R_STUCK
                        detail = f"stuck:{str(q.get('question', ''))[:60]}"
                elif t == "tool_call":
                    detail = f"tool:{p.get('name') or p.get('toolName') or ''}"
        ts = _parse_ts(relevant[-1][0].get("timestamp")) if relevant else None
        said = [
            (f"{e.get('id') or i}", str(q["content"]).strip(), _parse_ts(e.get("timestamp")) or 0.0)
            for i, (e, q) in enumerate(relevant) if q.get("type") == "assistant" and str(q.get("content") or "").strip()
        ]
        return Session(
            messages=said[-MAX_MESSAGES:],
            stop_reason=stop_reason,
            stop_detail=stop_detail,
            key=f"kiro:{sid}",
            agent="kiro",
            session_id=sid,
            cwd=cwd,
            repo=repo_for(cwd),
            state=state,
            updated=ts or mtime,
            title=str(meta.get("title", ""))[:80],
            turn_id=turn_id,
            final_message=final,
            detail=detail,
            source=path,
        )


class AgyLog:
    """Recent error lines from Antigravity's glog-style logs (errors are not in transcripts)."""

    LINE_RE = re.compile(r"^E(\d{2})(\d{2}) (\d{2}):(\d{2}):(\d{2})\.\d+\s+\d+\s+\S+\]\s*(.*)$")
    NOISE = re.compile(r"play\.googleapis\.com/log|ListExperiments|GeminiDir|create patch|Language server shutdown|"
                       r"input detection model call|g3syslog", re.I)

    def __init__(self, root: str):
        self.root = root
        self._sig: Tuple[str, float] = ("", 0.0)
        self._errors: List[Tuple[float, str]] = []

    def errors_between(self, start: float, end: float) -> List[Tuple[float, str]]:
        return [(t, m) for t, m in self.errors_since(start) if t <= end]

    def errors_since(self, since: float) -> List[Tuple[float, str]]:
        files = glob.glob(os.path.join(glob.escape(self.root), "cli.log")) + glob.glob(
            os.path.join(glob.escape(self.root), "log", "cli-*.log"))
        if not files:
            return []
        newest = max(files, key=lambda f: os.path.getmtime(f))
        mtime = os.path.getmtime(newest)
        if (newest, mtime) != self._sig:
            self._sig = (newest, mtime)
            year = time.localtime().tm_year
            errs = []
            for line in read_tail_lines(newest, 256 * 1024):
                m = self.LINE_RE.match(line)
                if not m or self.NOISE.search(line):
                    continue
                mo, d, hh, mm, ss, msg = m.groups()
                ts = time.mktime((year, int(mo), int(d), int(hh), int(mm), int(ss), 0, 0, -1))
                errs.append((ts, msg.split("] ", 1)[-1]))
            self._errors = errs
        return [(t, m) for t, m in self._errors if t >= since]


class AntigravityAdapter:
    agent = "agy"
    CWD_RE = re.compile(r'Cwd\\?"?\s*[:=]\s*\\?"?\\?"?(/[^"\\\n]+)')

    def __init__(self, home: Optional[str] = None):
        base = os.path.join(home or os.path.expanduser("~"), ".gemini", "antigravity-cli")
        self.root = os.path.join(base, "brain")
        self.cache = _FileCache()
        self.cwds: Dict[str, str] = {}
        self.log = AgyLog(base)

    def scan(self, horizon: float) -> List[Session]:
        now = time.time()
        out = []
        pattern = os.path.join(glob.escape(self.root), "*", ".system_generated", "logs", "transcript.jsonl")
        for path in glob.glob(pattern):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if now - st.st_mtime > horizon:
                continue
            if not self.cache.fresh(path, st):
                self.cache.put(path, st, self.parse(path, st.st_mtime))
            s = self.cache.sessions.get(path)
            if s:
                idle = time.time() - s.updated
                if s.state in (WORKING, WAITING, STALLED) and 20 < idle < 6 * 3600:
                    errs = self.log.errors_between(s.updated - 5, s.updated + 120)
                    if errs:
                        _, msg = errs[-1]
                        s.stop_reason = classify_error(msg)
                        s.stop_detail = msg[:120]
                        s.state = LIMITED if s.stop_reason == R_LIMIT else ERRORED
                        s.turn_id = s.turn_id or f"agy:{s.session_id}:err:{int(s.updated)}"
                if s.state in (WORKING, WAITING) and idle > 900:
                    s.state = STALLED
                elif s.state == WORKING and s.detail.startswith("tool:") and idle > 45:
                    s.state = WAITING
                out.append(s)
        return out

    def _cwd(self, path: str, lines: List[str]) -> str:
        for line in reversed(lines):
            m = self.CWD_RE.search(line)
            if m:
                self.cwds[path] = m.group(1)
                return m.group(1)
        if path not in self.cwds:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = self.CWD_RE.search(line)
                        if m:
                            self.cwds[path] = m.group(1)
            except OSError:
                pass
        return self.cwds.get(path, "")

    def parse(self, path: str, mtime: float) -> Optional[Session]:
        lines = read_tail_lines(path)
        entries = _json_lines(lines)
        if not entries:
            return None
        cwd = self._cwd(path, lines)
        if not cwd:
            return None
        sid = path.split(os.sep + "brain" + os.sep, 1)[-1].split(os.sep, 1)[0]
        last = entries[-1]
        state, turn_id, final, detail = WORKING, "", "", ""
        stop_reason = ""
        running = any(e.get("status") == "RUNNING" for e in entries[-5:])
        if (
            last.get("source") == "MODEL"
            and last.get("type") == "PLANNER_RESPONSE"
            and last.get("status") == "DONE"
            and last.get("content")
            and not last.get("tool_calls")
            and not running
        ):
            state = FINISHED
            turn_id = f"agy:{sid}:{last.get('step_index')}"
            stop_reason = R_COMPLETED
            final = str(last.get("content"))[:20000]
        elif last.get("tool_calls"):
            calls = last.get("tool_calls") or []
            name = calls[0].get("name", "") if isinstance(calls, list) and calls and isinstance(calls[0], dict) else ""
            detail = f"tool:{name}"
        title = ""
        for e in entries:
            if e.get("type") == "USER_INPUT" and e.get("content"):
                text = re.sub(r"<ADDITIONAL_METADATA>.*", "", str(e["content"]), flags=re.S)
                text = re.sub(r"</?[A-Z_]+>", "", text).strip()
                if text:
                    title = text.splitlines()[0][:80]
        said = [
            (f"{e.get('step_index')}", str(e["content"]).strip(), _parse_ts(e.get("created_at")) or 0.0)
            for e in entries
            if e.get("source") == "MODEL" and e.get("type") == "PLANNER_RESPONSE" and str(e.get("content") or "").strip()
        ]
        return Session(
            messages=said[-MAX_MESSAGES:],
            stop_reason=stop_reason,
            key=f"agy:{sid}",
            agent="agy",
            session_id=sid,
            cwd=cwd,
            repo=repo_for(cwd),
            state=state,
            updated=_parse_ts(last.get("created_at")) or mtime,
            title=title,
            turn_id=turn_id,
            final_message=final,
            detail=detail,
            source=path,
        )


class ProcessScanner:
    """Finds CLI agent processes and their working directories."""

    INTERPRETERS = {"node", "bun", "deno", "python", "python3", "ruby"}

    def __init__(self, names: Iterable[str]):
        self.names = {n.lower() for n in names}
        self._last: Dict[int, Session] = {}
        self._last_scan = 0.0

    def _match(self, name: str, cmdline: List[str], exe: str) -> Optional[str]:
        if ".app/Contents/" in exe and "/Resources/" not in exe:
            return None  # desktop GUI apps, not CLI agents
        low = name.lower()
        if low in self.names:
            return low
        if cmdline:
            argv0 = os.path.basename(cmdline[0]).lower()
            if argv0 in self.names:
                return argv0
        if exe:
            # e.g. ~/.local/share/claude/versions/2.1.286 (binary named after its version)
            parts = exe.lower().split("/")
            for agent in self.names:
                if agent in parts[-3:-1]:
                    return agent
        if low in self.INTERPRETERS or low.startswith("python"):
            for arg in cmdline[1:3]:
                base = os.path.basename(arg).lower()
                for suffix in (".js", ".mjs", ".cjs", ".py"):
                    if base.endswith(suffix):
                        base = base[: -len(suffix)]
                if base in self.names:
                    return base
        return None

    def scan(self, min_interval: float = 4.0) -> List[Session]:
        now = time.time()
        if now - self._last_scan < min_interval:
            return list(self._last.values())
        self._last_scan = now
        try:
            import psutil
        except ImportError:
            return []
        found: Dict[int, Session] = {}
        parents: Dict[int, int] = {}
        for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time", "exe"]):
            try:
                info = proc.info
                agent = self._match(info.get("name") or "", info.get("cmdline") or [], info.get("exe") or "")
                if not agent:
                    continue
                try:
                    cwd = proc.cwd()
                except Exception:
                    continue
                repo = repo_for(cwd)
                if not repo:
                    continue  # IDE helpers / agents outside any repository
                prev = self._last.get(info["pid"])
                found[info["pid"]] = Session(
                    key=f"proc:{info['pid']}",
                    agent=agent,
                    session_id=str(info["pid"]),
                    cwd=cwd,
                    repo=repo,
                    state=RUNNING,
                    updated=prev.updated if prev else now,
                    pid=info["pid"],
                    started=info.get("create_time") or now,
                )
                parents[info["pid"]] = info.get("ppid") or 0
            except Exception:
                continue
        # drop helper children whose parent is the same agent
        for pid in list(found):
            if parents.get(pid) in found and found[parents[pid]].agent == found[pid].agent:
                del found[pid]
        self._last = found
        return list(found.values())


# ---------------------------------------------------------------- tracker
class Tracker:
    """Fuses transcripts and processes into sessions and emits lifecycle events."""

    def __init__(
        self,
        monitored: Iterable[str],
        horizon_hours: float = 6.0,
        home: Optional[str] = None,
        processes: bool = True,
    ):
        self.adapters = [ClaudeCodeAdapter(home), KiroAdapter(home), AntigravityAdapter(home)]
        self.procs = ProcessScanner(monitored) if processes else None
        self.horizon = horizon_hours * 3600
        self.sessions: Dict[str, Session] = {}
        self._prev_state: Dict[str, str] = {}
        self._proc_seen: Dict[int, Session] = {}
        self._pid_of: Dict[str, int] = {}
        self._closed: Dict[str, float] = {}
        self._reset_announced: set = set()
        self._loop_announced: set = set()
        self._initialized = False

    def poll(self) -> Tuple[List[Session], List[AgentEvent]]:
        events: List[AgentEvent] = []
        transcript_sessions: List[Session] = []
        for ad in self.adapters:
            try:
                transcript_sessions.extend(ad.scan(self.horizon))
            except Exception:
                continue
        procs = self.procs.scan() if self.procs else []

        for s in transcript_sessions:
            s.pid = None  # re-attached below from the live process table
        # attach pids to transcript sessions of the same agent+repo
        family = {"claude": "claude", "kiro-cli": "kiro", "kiro": "kiro", "agy": "agy", "antigravity": "agy"}
        covered: set = set()
        for p in procs:
            fam = family.get(p.agent)
            if not fam or not p.repo:
                continue
            cands = [s for s in transcript_sessions if s.agent == fam and s.repo == p.repo]
            if cands:
                best = max(cands, key=lambda s: s.updated)
                best.pid = p.pid
                best.started = p.started
                covered.add(p.pid)

        # A transcript whose agent process vanished while it was mid-task.
        live_pids = {p.pid for p in procs}
        for s in transcript_sessions:
            prev_pid = self._pid_of.get(s.key)
            if s.pid:
                self._pid_of[s.key] = s.pid
                self._closed.pop(s.key, None)
            elif prev_pid and prev_pid not in live_pids and s.state in (WORKING, WAITING):
                self._closed.setdefault(s.key, time.time())
            if s.key in self._closed and not s.pid and s.state in (WORKING, WAITING, STALLED):
                s.state = CLOSED
                s.stop_reason = R_CLOSED
                s.stop_detail = f"process {self._pid_of.get(s.key)} ended before the task finished"
                s.turn_id = f"{s.key}:closed:{int(self._closed[s.key])}"

        current: Dict[str, Session] = {s.key: s for s in transcript_sessions}
        for p in procs:
            if p.pid not in covered:
                current[p.key] = p

        if self._initialized:
            for key, s in current.items():
                prev = self._prev_state.get(key)
                if s.state != prev:
                    if s.state == FINISHED:
                        events.append(AgentEvent("turn_finished", s))
                    elif s.state == WAITING:
                        events.append(AgentEvent("waiting", s))
                    elif s.state == INTERRUPTED:
                        events.append(AgentEvent("stopped", s))
                    elif s.state == STALLED:
                        events.append(AgentEvent("stalled", s))
                    elif s.state == LIMITED:
                        events.append(AgentEvent("limited", s))
                    elif s.state == ERRORED:
                        events.append(AgentEvent("errored", s))
                    elif s.state == CLOSED:
                        events.append(AgentEvent("closed", s))
                    elif prev is None and s.state in (WORKING, RUNNING) and time.time() - s.updated < 60:
                        events.append(AgentEvent("started", s))
                elif s.state == FINISHED and s.turn_id and s.turn_id != self.sessions.get(key, s).turn_id:
                    events.append(AgentEvent("turn_finished", s))
            for pid, old in self._proc_seen.items():
                if f"proc:{pid}" not in current and not any(s.pid == pid for s in current.values()):
                    gone = Session(**{**old.__dict__})
                    gone.state = EXITED
                    gone.updated = time.time()
                    gone.turn_id = f"exit:{old.agent}:{pid}:{int(old.started)}"
                    events.append(AgentEvent("exited", gone))
            for key, s in current.items():
                prev = self._prev_state.get(key)
                if s.state in (WORKING, WAITING) and prev in (FINISHED, INTERRUPTED, IDLE, LIMITED, ERRORED, CLOSED, STALLED):
                    events.append(AgentEvent("turn_started", s))
                if s.detail.startswith("loop:") and (key, s.detail) not in self._loop_announced:
                    self._loop_announced.add((key, s.detail))
                    events.append(AgentEvent("looping", s))
            for s in current.values():
                if s.state == LIMITED and s.resume_at and time.time() >= s.resume_at and s.turn_id not in self._reset_announced:
                    self._reset_announced.add(s.turn_id)
                    events.append(AgentEvent("limit_reset", s))
        self._proc_seen = {p.pid: p for p in procs}
        self._prev_state = {k: s.state for k, s in current.items()}
        self.sessions = current
        self._initialized = True
        return list(current.values()), events

    def visible(self, window: float = 3 * 3600) -> List[Session]:
        """Sessions worth showing: live processes or recent activity."""
        now = time.time()
        rows = [s for s in self.sessions.values() if s.pid or now - s.updated < window]
        order = {WAITING: 0, LIMITED: 1, ERRORED: 1, CLOSED: 1, WORKING: 2, RUNNING: 3, STALLED: 4, INTERRUPTED: 5,
                 FINISHED: 6, IDLE: 7, EXITED: 8}
        rows.sort(key=lambda s: (order.get(s.state, 9), -s.updated))
        return rows

    def busy_in_repo(self, repo: str, exclude_key: Optional[str] = None, recent: float = 120) -> List[Session]:
        now = time.time()
        return [
            s for s in self.sessions.values()
            if s.repo == repo and s.key != exclude_key and s.state in (WORKING, WAITING) and now - s.updated < recent
        ]
