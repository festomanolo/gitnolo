"""
Terminal design system. Claude Code inspired: quiet chrome, one warm accent,
status dots and tree connectors instead of emojis, dim secondary text.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .logo import CRIMSON

ACCENT = os.environ.get("GITNOLO_ACCENT", CRIMSON)

THEME = Theme(
    {
        "accent": ACCENT,
        "accent.bold": f"bold {ACCENT}",
        "muted": "#8a8a8a",
        "faint": "#5c5c5c",
        "ok": "#7fb069",
        "warn": "#e0b354",
        "err": "#e06c75",
        "info": "#6cb6d9",
        "add": "#7fb069",
        "del": "#e06c75",
        "hash": "#c792ea",
        "branch": "#6cb6d9",
        "path": "#e5e5e5",
    }
)

console = Console(theme=THEME, highlight=False)

# Glyphs (no emojis)
DOT = "⏺"
ELBOW = "⎿"
STAR = "✻"
BULLET = "·"

STATE_STYLE = {
    "working": ("●", "accent"),
    "running": ("●", "info"),
    "waiting": ("◆", "warn"),
    "finished": ("✓", "ok"),
    "stopped": ("■", "err"),
    "stalled": ("◌", "muted"),
    "exited": ("○", "faint"),
    "idle": ("○", "muted"),
    "limited": ("◔", "warn"),
    "error": ("×", "err"),
    "closed": ("◍", "err"),
}

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def spinner_frame() -> str:
    return SPINNER[int(time.time() * 10) % len(SPINNER)]


def state_badge(state: str, animate: bool = True) -> Text:
    glyph, style = STATE_STYLE.get(state, ("·", "muted"))
    if state == "working" and animate:
        glyph = spinner_frame()
    return Text.assemble((glyph + " ", style), (state, style))


def ago(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    d = max(0, time.time() - ts)
    if d < 60:
        return f"{int(d)}s"
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    return f"{int(d // 86400)}d"


def short_path(path: Optional[str], width: int = 40) -> str:
    if not path:
        return "-"
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home) :]
    if len(path) > width:
        path = "…" + path[-(width - 1) :]
    return path


# ------------------------------------------------------------------ lines
def step(text: str, style: str = "accent") -> None:
    console.print(Text.assemble((DOT + " ", style), (text, "bold")))


def detail(text: str, style: str = "muted") -> None:
    console.print(Text.assemble(("  " + ELBOW + "  ", "faint"), (text, style)))


def ok(text: str) -> None:
    console.print(Text.assemble((DOT + " ", "ok"), (text, "")))


def warn(text: str) -> None:
    console.print(Text.assemble((DOT + " ", "warn"), (text, "warn")))


def error(text: str) -> None:
    console.print(Text.assemble((DOT + " ", "err"), (text, "err")))


def emit_printer(kind: str, text: str) -> None:
    """Default Pipeline event sink for one-shot commands."""
    if kind == "step":
        step(text)
    elif kind == "ok":
        detail(text, "ok")
    elif kind == "warn":
        detail(text, "warn")
    elif kind == "error":
        detail(text, "err")
    else:
        detail(text)


# ------------------------------------------------------------------ header
def header(version: str, subtitle: str = "", lines: Sequence[Any] = (), logo: bool = True) -> None:
    """Welcome box in the spirit of Claude Code's startup card."""
    title = Text.assemble((STAR + " ", "accent"), ("Welcome to ", ""), ("gitnolo", "accent.bold"), (f"  v{version}", "muted"))
    body: List[Any] = [title]
    if subtitle:
        body.append(Text(subtitle, style="muted"))
    if lines:
        body.append(Text(""))
        body.extend(lines)
    content: Any = Group(*body)
    if logo:
        try:
            from .logo import render_pixel_logo

            from .logo import MARK

            wide = console.width >= len(MARK[0]) + 50
            art = render_pixel_logo(width=len(MARK[0]) if wide else 0)
            if art:
                grid = Table.grid(padding=(0, 3))
                grid.add_column(width=max(len(Text.from_ansi(l).plain) for l in art))
                grid.add_column()
                grid.add_row(Text.from_ansi("\n".join(art)), content)
                content = grid
        except Exception:
            pass
    console.print(Panel(content, box=box.ROUNDED, border_style="accent", padding=(0, 1), expand=False))


def kv_lines(pairs: Iterable[Sequence[str]]) -> List[Text]:
    out = []
    for k, v, *style in pairs:
        out.append(Text.assemble((f"{k:<11}", "muted"), (v, style[0] if style else "")))
    return out


def table(*columns: str, title: Optional[str] = None, expand: bool = False) -> Table:
    t = Table(
        box=box.SIMPLE_HEAD,
        show_edge=False,
        header_style="muted",
        title=title,
        title_justify="left",
        title_style="bold",
        expand=expand or any(c.endswith("*") for c in columns),
        pad_edge=False,
    )
    for c in columns:
        if c.endswith("*"):  # flexible column: absorbs width pressure first
            t.add_column(c[:-1], no_wrap=True, overflow="ellipsis", ratio=1, min_width=8)
        else:
            t.add_column(c, no_wrap=True, min_width=len(c))
    return t


def rule(text: str = "") -> None:
    console.rule(Text(text, style="muted") if text else "", style="faint", align="left")


def heat(age_seconds: float, newest: float, oldest: float) -> str:
    """GitLens-style heatmap color: recent = warm accent, old = cool."""
    if oldest <= newest:
        return ACCENT
    t = min(1.0, max(0.0, (age_seconds - newest) / (oldest - newest)))
    warm = (0xC4, 0x3B, 0x55)
    cool = (0x4A, 0x6F, 0x8A)
    r, g, b = (int(w + (c - w) * t) for w, c in zip(warm, cool))
    return f"#{r:02x}{g:02x}{b:02x}"


def bar(value: float, maximum: float, width: int = 24, style: str = "accent") -> Text:
    if maximum <= 0:
        return Text("")
    n = value / maximum * width
    full = int(n)
    part = "▏▎▍▌▋▊▉"[int((n - full) * 7)] if n - full > 0.07 and full < width else ""
    return Text("█" * full + part, style=style)


def sparkline(values: Sequence[float]) -> str:
    ticks = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    hi = max(values) or 1
    return "".join(ticks[min(7, int(v / hi * 7.999))] if v else " " for v in values)


def notify(title: str, message: str) -> None:
    """Native macOS notification (silently ignored elsewhere)."""
    import subprocess
    import sys

    if sys.platform != "darwin":
        return
    script = f'display notification {_osa(message)} with title {_osa(title)}'
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _osa(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"')[:200] + '"'
