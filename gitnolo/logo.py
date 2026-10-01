"""
Gitnolo mark: the festomanolo signature "A" as block pixel art.

Traced from the centerlines of the festomanolo signature and rasterized with
square half-block pixels (each character cell = 1 x 2 pixels), so it keeps the
signature's gesture: the rounded apex, the left leg crossing the smile-shaped
crossbar and trailing off, the right leg crossing into a heavy foot, and the
crossbar's tips sweeping up past both legs.
"""

from __future__ import annotations

from typing import List

CRIMSON = "#c43b55"  # festomanolo crimson (#7e2e40), lifted for dark terminals

MARK = [
    "            ▄██           ",
    "           ▄█▀██          ",
    "          ██  ▀█▄         ",
    "        ▄█▀    ██▄        ",
    "       ▄█▀      ██▄       ",
    "▄▄    ██         ██    ▄▄▄",
    "▀▀█▄▄█▀           ██▄▄█▀▀ ",
    "   ████▄▄       ▄▄███▀    ",
    " ▄█▀   ▀▀▀████▀▀▀▀  ██▄   ",
    "▀▀                   ███  ",
]

MARK_SMALL = [
    "       ██      ",
    "     ▄█▀██     ",
    "    ▄▀   █▄    ",
    "█▄▄█▀     █▄▄▄█",
    " ███▄▄▄▄▄▄███  ",
    "█▀   ▀▀▀▀  ▀██ ",
]


def render_pixel_logo(width: int = 26, color: str = CRIMSON) -> List[str]:
    """ANSI-colored lines of the mark; the compact variant for narrow layouts."""
    art = MARK if width >= 26 else MARK_SMALL
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return [f"\033[38;2;{r};{g};{b}m{line}\033[0m" for line in art]
