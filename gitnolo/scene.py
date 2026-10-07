"""
Animated map for the live dashboard.

  * Agent lanes: every agent is a little brick car on its own road. While the
    agent works, the road scrolls, the wheels turn and the car creeps toward
    the finish flag (it slows down the longer the turn runs, so it never
    arrives early). It parks at the flag when the turn finishes, stops with
    blinking hazards while waiting for you, and stops with a red sign on errors.
  * Pipeline map: the commit pipeline as stations on a line, with a pulse
    travelling from the station that is running to the next one.

Pure functions of (state, time): the dashboard just re-renders each frame.
"""

from __future__ import annotations

# Deferred until first use on Python 3.15+ (PEP 810); ignored by older interpreters.
__lazy_modules__ = [
    "math", "rich.text",
]

import math
import time
from typing import Dict, List, Optional, Sequence

from rich.text import Text

from . import ui

AGENT_COLORS = {
    "claude": ("#d97757", "#a9583d"),
    "kiro": ("#9b7bff", "#7259cc"),
    "kiro-cli": ("#9b7bff", "#7259cc"),
    "agy": ("#4f8ef7", "#3567c0"),
    "antigravity": ("#4f8ef7", "#3567c0"),
    "codex": ("#7fb069", "#5c8a4b"),
    "gemini": ("#6cb6d9", "#4c8aa8"),
    "cursor-agent": ("#e5e5e5", "#a0a0a0"),
}
DEFAULT_COLORS = ("#e0b354", "#b08a3a")

# Two-row brick car, facing right. Row 1: cabin and brick body; row 2 sits on the road.
CAR_TOP = " ▗▟█▓█▙▖"
CAR_BOT = "▝{w}▀▀▀{w}▘"
CAR_W = 8
WHEEL_SPIN = "◐◓◑◒"
LABEL_W = 22

STATIONS = ["analyze", "issues", "commit", "push", "pr", "merge", "sync"]
STATION_LABEL = {"pr": "PR"}

BUSY = ("working", "running")
STOPPED_BAD = ("error", "closed", "limited", "stopped")


class Lanes:
    """Remembers when each session's turn began, so the car's position means time on task."""

    def __init__(self) -> None:
        self.turn_start: Dict[str, float] = {}
        self.frozen: Dict[str, float] = {}  # elapsed time at which a stopped car came to rest

    def elapsed(self, key: str, state: str, now: float) -> float:
        if state in BUSY or state == "waiting":
            if key in self.frozen:  # a new turn after a stop or a finish: a new lap
                del self.frozen[key]
                self.turn_start[key] = now
            return now - self.turn_start.setdefault(key, now)
        if key not in self.frozen:
            start = self.turn_start.pop(key, None)
            self.frozen[key] = now - start if start else 0.0
        return self.frozen[key]

    def render(self, sessions: Sequence, width: int, now: Optional[float] = None, limit: int = 6) -> List[Text]:
        now = now or time.time()
        road_w = max(24, min(72, width - LABEL_W - 4))
        out: List[Text] = []
        for s in list(sessions)[:limit]:
            out.extend(lane(s, road_w, now, self.elapsed(s.key, s.state, now)))
        return out


def _colors(agent: str):
    return AGENT_COLORS.get(agent, DEFAULT_COLORS)


def car_position(state: str, road_w: int, elapsed: float) -> int:
    """Column of the car's left edge. Busy cars approach the flag asymptotically; finished cars park at it."""
    span = road_w - CAR_W - 2
    if state == "finished":
        return span
    progress = 1 - math.exp(-max(0.0, elapsed) / 420.0)  # ~63% of the road after 7 minutes
    return int(span * min(0.92, progress))


def lane(s, road_w: int, now: float, elapsed: float) -> List[Text]:
    body, brick = _colors(s.agent)
    state = s.state
    moving = state in BUSY
    x = car_position(state, road_w, elapsed)
    frame = int(now * 8)

    # ---- labels
    name = Text.assemble((f"  {s.agent[:9]:<10}", f"bold {body}"), (f"{s.repo_name[:11]:<12}", "path"))
    glyph, style = ui.STATE_STYLE.get(state, ("·", "muted"))
    if moving:
        glyph = ui.spinner_frame()
    sub = _activity(s)
    status = Text.assemble((f"  {glyph} ", style), (f"{state:<9}", style), (f"{sub[:9]:<10}", "faint"))

    # ---- row 1: open air with the car body (and a sign when stopped)
    top = Text(" " * road_w)
    hazard = state == "waiting" and frame % 8 < 4
    body_style = "#e0b354" if hazard else body
    top_text = Text()
    for ch in CAR_TOP:
        top_text.append(ch, style=brick if ch == "▓" else body_style)
    top = _overlay(top, top_text, x)
    if state == "waiting":
        top = _overlay(top, Text(" ◆ your turn", style="warn"), min(road_w - 12, x + CAR_W))
    elif state in STOPPED_BAD:
        top = _overlay(top, Text(f" × {(s.stop_reason or state)[:14]}", style="err"), min(road_w - 17, x + CAR_W))
    elif state == "stalled":
        top = _overlay(top, Text(" … quiet", style="faint"), min(road_w - 9, x + CAR_W))
    elif state == "finished":
        top = _overlay(top, Text("⚑", style="ok"), road_w - 1)

    # ---- row 2: the road, scrolling under a moving car
    offset = frame if moving else 0
    road = Text()
    for c in range(road_w):
        road.append("━" if (c + offset) % 4 < 2 else " ", style="#3a3a3a")
    road = _overlay(road, Text("▕", style="ok" if state == "finished" else "faint"), road_w - 1)
    wheel = WHEEL_SPIN[frame % 4] if moving else "●"
    chassis = CAR_BOT.format(w=wheel)
    car_bot = Text()
    for ch in chassis:
        car_bot.append(ch, style="#d0d0d0" if ch in WHEEL_SPIN + "●" else body_style)
    road = _overlay(road, car_bot, x)
    if moving and x > 2:  # exhaust puffs behind the car
        puff = "∘·"[frame % 2]
        road = _overlay(road, Text(puff, style="faint"), x - 2)

    return [name + top, status + road]


def _activity(s) -> str:
    d = (s.detail or "").split(":", 1)
    if d[0] == "loop":
        return "looping"
    if len(d) == 2 and d[1]:
        return d[1].strip()
    return ui.ago(s.updated)


def _overlay(base: Text, piece: Text, x: int) -> Text:
    x = max(0, min(x, len(base.plain) - 1))
    end = min(len(base.plain), x + len(piece.plain))
    piece = piece[: end - x]
    return base[:x] + piece + base[end:]


# ------------------------------------------------------------------ pipeline map
def pipeline_map(repo_name: str, stage: str, now: Optional[float] = None, seg: int = 4) -> Text:
    """One line: stations with the running one pulsing and a dot travelling to the next."""
    now = now or time.time()
    out = Text.assemble(("  ", ""), (ui.spinner_frame() + " ", "accent"), (f"{repo_name[:16]:<17}", "bold"))
    cur = STATIONS.index(stage) if stage in STATIONS else -1
    for i, st in enumerate(STATIONS):
        label = STATION_LABEL.get(st, st)
        if i == cur:
            out.append("◉ " if int(now * 4) % 2 else "○ ", style="accent.bold")
            out.append(label, style="accent.bold")
        else:
            style = "ok" if i < cur else "faint"
            out.append("● " if i < cur else "○ ", style=style)
            out.append(label, style=style)
        if i < len(STATIONS) - 1:
            out.append(" ")
            if i == cur:
                pos = int(now * 6) % seg
                out.append("".join("•" if k == pos else "─" for k in range(seg)), style="accent")
            else:
                out.append("━" * seg if i < cur else "─" * seg, style="ok" if i < cur else "#3a3a3a")
            out.append(" ")
    return out
