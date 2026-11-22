"""Terminal output: ANSI styling (off when not a TTY or NO_COLOR is set), aligned tables, hints."""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata

OK, FAIL, UP, DOWN, DOT, ARROW, WARN = "✓", "✗", "▲", "▼", "·", "→", "!"

_CODES = {
    "bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "grey": "90",
}


def color_enabled(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def style(text: str, *names: str, stream=None) -> str:
    if not names or not color_enabled(stream):
        return text
    return "\033[%sm%s\033[0m" % (";".join(_CODES[n] for n in names), text)


def bold(t): return style(t, "bold")
def dim(t): return style(t, "grey")
def good(t): return style(t, "green")
def bad(t): return style(t, "red")
def warn(t): return style(t, "yellow")
def accent(t): return style(t, "cyan")


def width() -> int:
    return max(80, min(shutil.get_terminal_size((100, 24)).columns, 140))


def vlen(s: str) -> int:
    """Visible width: ANSI codes removed, wide characters count 2."""
    out, i = 0, 0
    while i < len(s):
        if s[i] == "\033":
            j = s.find("m", i)
            i = len(s) if j < 0 else j + 1
            continue
        ch = s[i]
        out += 2 if unicodedata.east_asian_width(ch) in "WF" else (0 if unicodedata.combining(ch) else 1)
        i += 1
    return out


def pad(s: str, n: int, right: bool = False) -> str:
    gap = max(0, n - vlen(s))
    return (" " * gap + s) if right else (s + " " * gap)


def clip(s: str, n: int) -> str:
    if vlen(s) <= n:
        return s
    out = ""
    for ch in s:
        if vlen(out + ch) > n - 1:
            break
        out += ch
    return out + "…"


def table(headers, rows, align=None, indent: int = 2, sub=None) -> str:
    """Render rows as aligned columns. align: string of 'l'/'r' per column.
    sub: optional per-row secondary line (shown dim under the row)."""
    n = len(headers)
    align = align or "l" * n
    w = [vlen(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            w[i] = max(w[i], vlen(str(c)))
    total = sum(w) + 2 * (n - 1) + indent
    lim = width()
    if total > lim:  # shrink the widest left-aligned column
        widest = max((i for i in range(n) if align[i] == "l"), key=lambda i: w[i])
        w[widest] = max(8, w[widest] - (total - lim))
    sp = " " * indent
    lines = [sp + "  ".join(pad(dim(h), w[i], align[i] == "r") for i, h in enumerate(headers))]
    for k, r in enumerate(rows):
        cells = [pad(clip(str(c), w[i]), w[i], align[i] == "r") for i, c in enumerate(r)]
        lines.append(sp + "  ".join(cells).rstrip())
        if sub and sub[k]:
            import textwrap

            wrapped = textwrap.wrap(sub[k], max(40, min(lim, 110) - indent - 3))
            if len(wrapped) > 2:
                wrapped = [wrapped[0], wrapped[1] + " …"]
            lines.extend(sp + "   " + dim(ln) for ln in wrapped)
    return "\n".join(lines)


def headline(title: str, detail: str = "") -> None:
    print(bold(accent("ras-anything")) + " " + bold(title) + ((" " + dim(DOT + " " + detail)) if detail else ""))


def rule() -> None:
    print(dim("─" * min(width(), 100)))


def next_hint(cmd: str) -> None:
    print()
    print(dim("next: ") + bold("ras-anything " + cmd))


def error(what: str, impact: str = "", nxt: str = "") -> None:
    print(bad(FAIL + " " + what), file=sys.stderr)
    if impact:
        print("  " + impact, file=sys.stderr)
    if nxt:
        print(dim("next: ") + "ras-anything " + nxt, file=sys.stderr)


def pct(x: float | None, digits: int = 1) -> str:
    return "—" if x is None else ("%." + str(digits) + "f%%") % (100.0 * x)


def secs(x: float | None) -> str:
    if x is None:
        return "—"
    if x >= 100:
        return "%.0fs" % x
    if x >= 1:
        return "%.2fs" % x
    return "%.0fms" % (x * 1000)


def bar(frac: float, n: int = 20) -> str:
    frac = max(0.0, min(1.0, frac))
    full = int(round(frac * n))
    return "█" * full + dim("░" * (n - full))
