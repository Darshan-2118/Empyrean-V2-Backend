"""Empyrean dev-stack banner.

Prints a bold ASCII "Empyrean" title plus a welcome banner before the
API server starts. Used by scripts/start.bat in the empyrean-server tab.

Colors degrade gracefully: ANSI is used only when the output is a
terminal and NO_COLOR / EMPIREAN_NO_BANNER do not opt out.
"""

import os
import random
import shutil
import sys

ESC = "\x1b"

# ANSI Shadow-style block letters -- bold, heavy strokes like the
# OmniRoute reference art. Assembled letter-by-letter so the spelling
# is always correct, with a single space between letters.
_LETTERS = {
    "E": [
        "███████╗",
        "██╔════╝",
        "█████╗  ",
        "██╔══╝  ",
        "███████╗",
        "╚══════╝",
    ],
    "M": [
        "███╗   ███╗",
        "████╗ ████║",
        "██╔████╔██║",
        "██║╚██╔╝██║",
        "██║ ╚═╝ ██║",
        "╚═╝     ╚═╝",
    ],
    "P": [
        "██████╗ ",
        "██╔══██╗",
        "██████╔╝",
        "██╔═══╝ ",
        "██║     ",
        "╚═╝     ",
    ],
    "Y": [
        "██╗   ██╗",
        "╚██╗ ██╔╝",
        " ╚████╔╝ ",
        "  ╚██╔╝  ",
        "   ██║   ",
        "   ╚═╝   ",
    ],
    "R": [
        "██████╗ ",
        "██╔══██╗",
        "██████╔╝",
        "██╔══██╗",
        "██║  ██║",
        "╚═╝  ╚═╝",
    ],
    "A": [
        " █████╗ ",
        "██╔══██╗",
        "███████║",
        "██╔══██║",
        "██║  ██║",
        "╚═╝  ╚═╝",
    ],
    "N": [
        "███╗   ██╗",
        "████╗  ██║",
        "██╔██╗ ██║",
        "██║╚██╗██║",
        "██║ ╚████║",
        "╚═╝  ╚═══╝",
    ],
}

TITLE = "\n".join(
    " ".join(letter_rows).rstrip()
    for letter_rows in zip(*(_LETTERS[ch] for ch in "EMPYREAN"))
)

# Box content uses only single-width characters so the right border
# always lines up, regardless of terminal emoji rendering.
_WELCOME_LINES = [
    "",
    "✦  W E L C O M E   A B O A R D  ✦",
    "",
    "The Empyrean dev stack is waking up its engines...",
    "",
    "> API Server      →  hypercorn on :8000",
    "> Celery Worker   →  crunching tasks",
    "> Celery Beat     →  keeping the schedule",
    "> Redis / WSL     →  pinned and ready",
    "",
    "Sit back — ignition in progress",
    "",
]

_BOX_WIDTH = 70


def _build_box(lines: list[str], width: int = _BOX_WIDTH) -> str:
    """Wrap *lines* in an even double-line box of the given inner width.

    Lines starting with ``>`` are left-aligned (marker stripped); all
    others are centered.
    """
    out = ["╔" + "═" * width + "╗"]
    for line in lines:
        if line.startswith(">"):
            text = "   " + line[1:].lstrip()
            out.append("║" + text.ljust(width) + "║")
        else:
            out.append("║" + line.center(width) + "║")
    out.append("╚" + "═" * width + "╝")
    return "\n".join(out)


WELCOME = _build_box(_WELCOME_LINES)

# Per-line gradient for the title: deep blue -> cyan -> bright cyan.
TITLE_GRADIENT = [94, 94, 96, 96, 94, 94]
WELCOME_COLOR = "1;95"  # bold bright magenta

# ---------------------------------------------------------------------------
# Per-component banners. Each tab gets the same EMPYREAN title art plus a
# themed welcome box describing that component.
# ---------------------------------------------------------------------------

COMPONENTS: dict[str, dict[str, object]] = {
    "server": {
        "color": "1;95",  # bold bright magenta
        "lines": [
            "",
            "✦  W E L C O M E   A B O A R D  ✦",
            "",
            "The Empyrean dev stack is waking up its engines...",
            "",
            "> API Server      →  hypercorn on :8000",
            "> Celery Worker   →  crunching tasks",
            "> Celery Beat     →  keeping the schedule",
            "> Redis / WSL     →  pinned and ready",
            "",
            "Sit back — ignition in progress",
            "",
        ],
    },
    "worker": {
        "color": "1;93",  # bold bright yellow
        "lines": [
            "",
            "⚙  C E L E R Y   W O R K E R  ⚙",
            "",
            "Booting the task engine — ready to crunch.",
            "",
            "> Queue        →  celery, brokered by Redis",
            "> Pool         →  solo (Windows-safe)",
            "> Concurrency  →  one task at a time",
            "",
            "Churning through tasks in the background",
            "",
        ],
    },
    "beat": {
        "color": "1;96",  # bold bright cyan
        "lines": [
            "",
            "◈  C E L E R Y   B E A T  ◈",
            "",
            "The metronome of the stack — schedules locked in.",
            "",
            "> Aggregations  →  rolling up readings",
            "> Alerts        →  evaluating thresholds",
            "> AQI / Forecast →  periodic refreshes",
            "",
            "Ticking along — every task on time",
            "",
        ],
    },
    "wsl": {
        "color": "1;92",  # bold bright green
        "lines": [
            "",
            "◆  W S L   I N S T A N C E  ◆",
            "",
            "Pinning the WSL VM open so Redis stays alive.",
            "",
            "> Purpose   →  keep the VM from idling out",
            "> Redis     →  served from this instance",
            "> Lifetime  →  as long as this window lives",
            "",
            "Holding the foundation steady",
            "",
        ],
    },
    "tunnel": {
        "color": "1;33",  # bold bright orange/yellow
        "lines": [
            "",
            "☁  C L O U D F L A R E   T U N N E L  ☁",
            "",
            "Boring a secure tunnel through the internet.",
            "",
            "> Tunnel      →  empyrean",
            "> Provider    →  Cloudflare (cloudflared)",
            "> Exposes     →  localhost:8000 to the world",
            "",
            "Your API, publicly reachable — securely",
            "",
        ],
    },
}

_BOX_WIDTH = 70


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("EMPIREAN_NO_BANNER"):
        return False
    if not sys.stdout.isatty():
        return False
    return True


def _enable_vt() -> None:
    """Enable ANSI escape processing in legacy Windows consoles."""
    if os.name == "nt":
        os.system("")


def _paint_title() -> None:
    lines = TITLE.strip("\n").splitlines()
    for i, line in enumerate(lines):
        code = TITLE_GRADIENT[i % len(TITLE_GRADIENT)]
        print(f"{ESC}[1;{code}m{line}{ESC}[0m")


def _paint_welcome(color: str, box: str) -> None:
    print(f"{ESC}[{color}m{box}{ESC}[0m")


def _tagline() -> None:
    tags = [
        "Realtime IoT telemetry, done right.",
        "From sensor to insight at light speed.",
        "Fuzzy logic. Sharp results.",
        "Your data, elevated.",
    ]
    tag = f"  ~ {random.choice(tags)} ~"
    width = max(shutil.get_terminal_size((80, 24)).columns, 40)
    print(f"{ESC}[2;36m{tag.center(width)}{ESC}[0m")
    print()


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    component = argv[0].lower() if argv else "server"
    spec = COMPONENTS.get(component, COMPONENTS["server"])
    color = str(spec["color"])
    box = _build_box(list(spec["lines"]))  # type: ignore[arg-type]

    _enable_vt()
    print()
    if _supports_color():
        _paint_title()
        _tagline()
        _paint_welcome(color, box)
    else:
        print(TITLE.strip("\n"))
        print(box)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
