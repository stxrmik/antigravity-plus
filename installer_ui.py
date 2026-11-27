"""Antigravity Plus — the installer's terminal interface (install.py). Stdlib only, Python 3.8+, macOS/Linux/Windows.

Layers:
  (a) capabilities — `detect()`: rich (a person at a full terminal: colour, Unicode frames, spinners, keys) or plain
      (pipes, CI, agents' terminals, TERM=dumb, --plain: line by line, no escape sequences); NO_COLOR keeps rich
      without colour; stdout a TTY but stdin not (`… | sh`) = rich without questions.
  (b) styles — colours (truecolor → 256 → 16), gradients, frames, the on-screen width of a string (Cyrillic 1, wide
      characters and emoji 2, ANSI codes 0), wrapping and truncating with «…».
  (c) keys — POSIX termios + escape-sequence parsing (`parse_keys`), Windows msvcrt.getwch.
  (d) widgets — the logo header, steps with a spinner and final marks, the module multi-select, a yes/no question,
      the closing frame.

Nothing here touches the system: install.py does the work and tells this module what to show. Any failure of the
interface itself turns it plain (`UI.fail_over`) and the install goes on; the terminal is always restored (cursor
shown, attributes reset, raw mode off) — on return, on an exception, on Ctrl-C and on SIGTERM (atexit + finally).
"""
from __future__ import annotations

import atexit
import codecs
import math
import os
import re
import shutil
import signal
import sys
import threading
import time
import unicodedata
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

WINDOWS = os.name == "nt"

# ─────────────────────────────── (a) capabilities ───────────────────────────────


class Caps:
    """What the terminal can do. mode: "rich" | "plain"; color: "truecolor" | "256" | "16" | None;
    interactive: keys can be read (stdin is a terminal too); unicode: frames and braille spinners render."""

    def __init__(self, mode: str = "plain", color: Optional[str] = None, interactive: bool = False,
                 unicode: bool = True, reason: str = "") -> None:
        self.mode, self.color, self.interactive, self.unicode, self.reason = mode, color, interactive, unicode, reason

    @property
    def rich(self) -> bool:
        return self.mode == "rich"

    def __repr__(self) -> str:
        return (f"Caps(mode={self.mode!r}, color={self.color!r}, interactive={self.interactive}, "
                f"unicode={self.unicode}, reason={self.reason!r})")


def agent_terminal(env: Dict[str, str]) -> str:
    """Why nobody can answer here: an agent's terminal (Antigravity sets TERM=dumb, its CLI ANTIGRAVITY_AGENT=1,
    Claude Code CLAUDECODE) or CI."""
    if env.get("TERM", "") == "dumb":
        return "TERM=dumb"
    if env.get("CLAUDECODE"):
        return "CLAUDECODE"
    if env.get("ANTIGRAVITY_AGENT"):  # commands of an Antigravity CLI agent
        return "ANTIGRAVITY_AGENT"
    if env.get("CI"):
        return "CI"
    return ""


def color_depth(env: Dict[str, str]) -> Optional[str]:
    """None under NO_COLOR (https://no-color.org: set and not empty), else the best the terminal says it has."""
    if env.get("NO_COLOR", ""):
        return None
    term = env.get("TERM", "").lower()
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit") or env.get("WT_SESSION") or term.endswith("-direct"):
        return "truecolor"
    if "256" in term:
        return "256"
    return "16"


def unicode_ok(encoding: str) -> bool:
    try:
        return codecs.lookup(encoding or "ascii").name in ("utf-8", "utf-16", "utf-32")
    except LookupError:
        return False


def enable_vt() -> bool:
    """Windows: switch the console to VT mode (ENABLE_VIRTUAL_TERMINAL_PROCESSING); True where escape codes work."""
    if not WINDOWS:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004 | 0x0001))  # VT processing + processed output
    except Exception:
        return False


def isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def detect(env: Optional[Dict[str, str]] = None, stdin_tty: Optional[bool] = None, stdout_tty: Optional[bool] = None,
           plain: bool = False, encoding: Optional[str] = None, windows: Optional[bool] = None,
           vt: Optional[Callable[[], bool]] = None) -> Caps:
    """Pick the output mode. AGP_INSTALL_UI=plain|rich forces one (rich still asks only when both ends are TTYs)."""
    env = dict(os.environ) if env is None else env
    stdin_tty = isatty(sys.stdin) if stdin_tty is None else stdin_tty
    stdout_tty = isatty(sys.stdout) if stdout_tty is None else stdout_tty
    encoding = (getattr(sys.stdout, "encoding", "") or "") if encoding is None else encoding
    windows = WINDOWS if windows is None else windows
    forced = env.get("AGP_INSTALL_UI", "").lower()
    uni = unicode_ok(encoding)
    if plain or forced == "plain":
        return Caps("plain", None, False, uni, "--plain" if plain else "AGP_INSTALL_UI=plain")
    if forced != "rich":
        if not stdout_tty:
            return Caps("plain", None, False, uni, "stdout is not a terminal")
        why = agent_terminal(env)
        if why:
            return Caps("plain", None, False, uni, why)
    if windows and not (vt or enable_vt)():
        return Caps("plain", None, False, False, "no VT mode in this console")
    if not uni:
        return Caps("plain", None, False, False, f"the console encoding ({encoding}) has no Unicode")
    interactive = bool(stdin_tty and stdout_tty)
    return Caps("rich", color_depth(env), interactive, True, "" if interactive else "stdin is not a terminal")


def term_size() -> Tuple[int, int]:
    """(columns, rows), with (80, 24) when the size can't be read."""
    try:
        size = shutil.get_terminal_size((80, 24))
        cols, rows = size.columns or 80, size.lines or 24
    except (OSError, ValueError):
        cols, rows = 80, 24
    return max(cols, 20), max(rows, 5)


# ─────────────────────────────── (b) styles ───────────────────────────────

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFE0E, 0xFE0F, 0xFEFF}
ZWJ, VS16 = "‍", "️"


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def char_width(ch: str) -> int:
    o = ord(ch)
    if o < 32 or 0x7F <= o < 0xA0 or o in ZERO_WIDTH:
        return 0
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F") or 0x1F300 <= o <= 0x1FAFF:
        return 2
    return 1


def clusters(s: str) -> List[str]:
    """Characters with what renders on top of them (combining marks, variation selectors, ZWJ joins)."""
    out: List[str] = []
    for ch in s:
        if out and (char_width(ch) == 0 and ord(ch) >= 32 or out[-1].endswith(ZWJ)):
            out[-1] += ch
        else:
            out.append(ch)
    return out


def cluster_width(c: str) -> int:
    if not c:
        return 0
    w = char_width(c[0])
    if w == 1 and VS16 in c:  # emoji presentation: ⚠️ ✔️ take two cells
        w = 2
    return w


def width(s: str) -> int:
    """Cells the string takes on screen: ANSI codes 0, Cyrillic 1, CJK and emoji 2."""
    return sum(cluster_width(c) for c in clusters(strip_ansi(s)))


def _tokens(s: str) -> Iterator[Tuple[str, bool]]:
    pos = 0
    for m in ANSI_RE.finditer(s):
        for c in clusters(s[pos:m.start()]):
            yield c, False
        yield m.group(0), True
        pos = m.end()
    for c in clusters(s[pos:]):
        yield c, False


def truncate(s: str, w: int, tail: str = "…") -> str:
    """At most `w` cells; what doesn't fit becomes «…». Escape codes are kept, and closed when one was cut."""
    if width(s) <= w:
        return s
    if w <= 0:
        return ""
    tail = tail if width(tail) <= w else ""
    limit, used, out, styled = w - width(tail), 0, [], False
    for tok, is_ansi in _tokens(s):
        if is_ansi:
            out.append(tok)
            styled = True
            continue
        cw = cluster_width(tok)
        if used + cw > limit:
            break
        out.append(tok)
        used += cw
    return "".join(out) + tail + ("\x1b[0m" if styled else "")


def pad(s: str, w: int) -> str:
    """Exactly `w` cells: truncated or filled with spaces."""
    s = truncate(s, w)
    return s + " " * max(0, w - width(s))


def wrap(text: str, w: int) -> List[str]:
    """Word-wrap plain text to `w` cells; words longer than a line are cut. Newlines start new lines."""
    w = max(w, 1)
    out: List[str] = []
    for para in text.split("\n"):
        line, lw = "", 0
        for word in para.split():
            ww = width(word)
            while ww > w:  # a word longer than the line: cut it
                if line:
                    out.append(line)
                    line, lw = "", 0
                head, used = "", 0
                for c in clusters(word):
                    if used + cluster_width(c) > w:
                        break
                    head += c
                    used += cluster_width(c)
                out.append(head)
                word = word[len(head):]
                ww = width(word)
            if not word:
                continue
            if line and lw + 1 + ww > w:
                out.append(line)
                line, lw = "", 0
            line, lw = (line + " " + word, lw + 1 + ww) if line else (word, ww)
        out.append(line)
    return out


RGB = Tuple[int, int, int]
BASIC16: List[RGB] = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188),
                      (17, 168, 205), (229, 229, 229), (102, 102, 102), (241, 76, 76), (35, 209, 139),
                      (245, 245, 67), (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255)]
CUBE = [0, 95, 135, 175, 215, 255]

# Mid-luminance accents read on dark and light backgrounds alike; the body text keeps the terminal's own colour.
BLUE: RGB = (66, 133, 244)
VIOLET: RGB = (147, 105, 230)
ROSE: RGB = (222, 98, 118)
GREEN: RGB = (34, 163, 96)
AMBER: RGB = (214, 140, 20)
RED: RGB = (220, 60, 60)
GRADIENT = [BLUE, VIOLET, ROSE]


def _dist(a: RGB, b: RGB) -> int:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def rgb_to_256(rgb: RGB) -> int:
    idx = [min(range(6), key=lambda i: abs(CUBE[i] - c)) for c in rgb]
    cube = 16 + 36 * idx[0] + 6 * idx[1] + idx[2]
    cube_rgb = (CUBE[idx[0]], CUBE[idx[1]], CUBE[idx[2]])
    gray_i = max(0, min(23, int(round((sum(rgb) / 3 - 8) / 10))))
    gray = 8 + 10 * gray_i
    return cube if _dist(rgb, cube_rgb) <= _dist(rgb, (gray, gray, gray)) else 232 + gray_i


def rgb_to_16(rgb: RGB) -> int:
    """The nearest of the 16 basic colours, never black or white (one of them is the background)."""
    usable = [i for i in range(16) if i not in (0, 7, 8, 15)]
    return min(usable, key=lambda i: _dist(rgb, BASIC16[i]))


def fg(rgb: RGB, depth: Optional[str]) -> str:
    if depth == "truecolor":
        return "\x1b[38;2;%d;%d;%dm" % rgb
    if depth == "256":
        return "\x1b[38;5;%dm" % rgb_to_256(rgb)
    if depth == "16":
        i = rgb_to_16(rgb)
        return "\x1b[%dm" % (30 + i if i < 8 else 82 + i)
    return ""


def mix(a: RGB, b: RGB, t: float) -> RGB:
    return (int(round(a[0] + (b[0] - a[0]) * t)), int(round(a[1] + (b[1] - a[1]) * t)),
            int(round(a[2] + (b[2] - a[2]) * t)))


def gradient_at(stops: Sequence[RGB], t: float) -> RGB:
    if len(stops) == 1:
        return stops[0]
    t = min(max(t, 0.0), 1.0) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    return mix(stops[i], stops[i + 1], t - i)


class Style:
    """Escape codes for one colour depth; `None` = no colour (NO_COLOR), `off` = no codes at all (plain)."""

    def __init__(self, depth: Optional[str], off: bool = False) -> None:
        self.depth, self.off = depth, off

    def paint(self, text: str, rgb: Optional[RGB] = None, bold: bool = False, dim: bool = False) -> str:
        if self.off or not text:
            return text
        codes = ("\x1b[1m" if bold else "") + ("\x1b[2m" if dim else "") + (fg(rgb, self.depth) if rgb else "")
        return codes + text + "\x1b[0m" if codes else text

    def bold(self, t: str) -> str:
        return self.paint(t, bold=True)

    def dim(self, t: str) -> str:
        return self.paint(t, dim=True)

    def accent(self, t: str, bold: bool = False) -> str:
        return self.paint(t, VIOLET, bold=bold)

    def gradient(self, text: str, stops: Sequence[RGB] = GRADIENT, span: Optional[int] = None, bold: bool = False) -> str:
        """Colour each cell along the line, from the first stop to the last (spaces stay plain)."""
        if self.off or self.depth is None:
            return self.paint(text, bold=bold)
        span = max((span or width(text)) - 1, 1)
        out, col = [], 0
        for c in clusters(text):
            if c.strip():
                out.append(fg(gradient_at(stops, col / span), self.depth) + ("\x1b[1m" if bold else "") + c)
            else:
                out.append("\x1b[0m" + c)
            col += cluster_width(c)
        return "".join(out) + "\x1b[0m"


# ─────────────────────────────── (c) keys ───────────────────────────────

CSI_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "btab"}
TILDE = {"1": "home", "7": "home", "4": "end", "8": "end", "5": "pgup", "6": "pgdn", "3": "delete", "2": "insert"}
CTRL = {"\r": "enter", "\n": "enter", " ": "space", "\t": "tab", "\x7f": "backspace", "\x08": "backspace",
        "\x03": "ctrl-c", "\x04": "ctrl-d", "\x1a": "ctrl-z"}


def incomplete(data: str) -> bool:
    """The buffer ends in the middle of an escape sequence (more bytes are on their way)."""
    i = data.rfind("\x1b")
    if i < 0:
        return False
    rest = data[i + 1:]
    if rest == "":
        return True
    if rest[0] == "[":
        return not re.match(r"\[[0-?]*[ -/]*[@-~]", rest)
    if rest[0] == "O":
        return len(rest) < 2
    return False


def parse_keys(data: str) -> List[str]:
    """Key names from what a terminal sends: up/down/left/right/home/end/pgup/pgdn/delete, enter, space, tab,
    backspace, esc, ctrl-c, ctrl-d, alt-<x>, or the character itself. Unknown sequences are dropped."""
    keys: List[str] = []
    i, n = 0, len(data)
    while i < n:
        ch = data[i]
        if ch == "\x1b":
            if i + 1 >= n:
                keys.append("esc")
                i += 1
                continue
            nxt = data[i + 1]
            if nxt == "[":
                m = re.match(r"\[([0-?]*)[ -/]*([@-~])", data[i + 1:])
                if not m:
                    keys.append("esc")
                    i += 1
                    continue
                params, final = m.group(1), m.group(2)
                if final == "~":
                    name = TILDE.get(params.split(";")[0])
                else:
                    name = CSI_FINAL.get(final)
                if name:
                    keys.append(name)
                i += 1 + m.end()
                continue
            if nxt == "O" and i + 2 < n:
                name = CSI_FINAL.get(data[i + 2])
                if name:
                    keys.append(name)
                i += 3
                continue
            if nxt == "\x1b":
                keys.append("esc")
                i += 1
                continue
            keys.append("alt-" + nxt)
            i += 2
            continue
        if ch in CTRL:
            if ch == "\r" and i + 1 < n and data[i + 1] == "\n":
                i += 1
            keys.append(CTRL[ch])
        elif ord(ch) >= 32:
            keys.append(ch)
        i += 1
    return keys


WIN_KEYS = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end", "I": "pgup",
            "Q": "pgdn", "S": "delete"}


class KeyReader:
    """Raw keys from the terminal while the `with` lasts. POSIX: echo, line mode and signals off (Ctrl-C comes as a
    key), output processing kept so «\\n» still starts a line; the old mode comes back on exit, at exit and on
    SIGTERM. Windows: msvcrt.getwch."""

    def __init__(self) -> None:
        self.fd = -1
        self.old = None
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def __enter__(self) -> "KeyReader":
        if not WINDOWS:
            import termios
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            new = termios.tcgetattr(self.fd)
            new[0] &= ~(termios.IXON | termios.ICRNL)
            new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            GUARD.add(self.restore)
            termios.tcsetattr(self.fd, termios.TCSADRAIN, new)
        return self

    def restore(self) -> None:
        if self.old is not None and not WINDOWS:
            try:
                import termios
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
            except Exception:
                pass
        self.old = None
        GUARD.discard(self.restore)

    def __exit__(self, *exc) -> None:
        self.restore()

    def read(self, timeout: float = 0.1) -> List[str]:
        """The keys pressed within `timeout` seconds ([] if none)."""
        if WINDOWS:
            return self._read_windows(timeout)
        import select
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return []
        data = self.decoder.decode(os.read(self.fd, 1024))
        while incomplete(data):  # a lone ESC or half a sequence: give the rest 50 ms to arrive
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if not ready:
                break
            data += self.decoder.decode(os.read(self.fd, 1024))
        return parse_keys(data)

    def _read_windows(self, timeout: float) -> List[str]:
        import msvcrt
        end = time.time() + timeout
        keys: List[str] = []
        while True:
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    name = WIN_KEYS.get(msvcrt.getwch())
                    if name:
                        keys.append(name)
                elif ch == "\x1b":
                    keys.append("esc")
                else:
                    keys += parse_keys(ch)
            if keys or time.time() >= end:
                return keys
            time.sleep(0.02)


# ─────────────────────────────── terminal restore ───────────────────────────────


class Guard:
    """Puts the terminal back however the program ends: raw mode off (registered restorers), attributes reset,
    cursor shown. atexit covers returns, exceptions and SystemExit; SIGTERM/SIGHUP restore, then exit."""

    def __init__(self) -> None:
        self.restorers: List[Callable[[], None]] = []
        self.out = None
        self.dirty = False
        self.installed = False

    def install(self, out) -> None:
        self.out = out
        if self.installed:
            return
        self.installed = True
        atexit.register(self.restore)
        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                if signal.getsignal(sig) in (signal.SIG_DFL, None):
                    signal.signal(sig, self._on_signal)
            except (ValueError, OSError):  # not the main thread
                pass

    def _on_signal(self, signum, _frame) -> None:
        self.restore()
        raise SystemExit(128 + signum)

    def add(self, fn: Callable[[], None]) -> None:
        self.restorers.append(fn)

    def discard(self, fn: Callable[[], None]) -> None:
        self.restorers = [f for f in self.restorers if f != fn]

    def restore(self) -> None:
        for fn in list(reversed(self.restorers)):
            try:
                fn()
            except Exception:
                pass
        self.restorers = []
        if self.dirty and self.out is not None:
            try:
                self.out.write("\x1b[0m\x1b[?25h")
                self.out.flush()
            except Exception:
                pass
        self.dirty = False


GUARD = Guard()

# ─────────────────────────────── (d) widgets ───────────────────────────────

TEXT = {
    "ru": {
        "keys": "↑↓ выбор · пробел отметить · a все · n только обязательные · enter установить · esc отмена",
        "keys_short": "↑↓ · пробел · a · n · enter · esc",
        "chosen": "выбрано {n} из {m}",
        "required": "обязательно",
        "locked": "Этот модуль обязателен — без него остальное не работает.",
        "cant": "Недоступно: {why}",
        "more_up": "↑ ещё {n}",
        "more_down": "↓ ещё {n}",
        "yes": "да", "no": "нет", "cancelled": "отменено",
        "yn": "(д/Н)", "Yn": "(Д/н)",
        "failed_ui": "Интерфейс дал сбой ({err}) — продолжаю в простом режиме.",
        "running": "идёт",
    },
    "en": {
        "keys": "↑↓ move · space toggle · a all · n required only · enter install · esc cancel",
        "keys_short": "↑↓ · space · a · n · enter · esc",
        "chosen": "{n} of {m} chosen",
        "required": "required",
        "locked": "This module is required — everything else builds on it.",
        "cant": "Not available: {why}",
        "more_up": "↑ {n} more",
        "more_down": "↓ {n} more",
        "yes": "yes", "no": "no", "cancelled": "cancelled",
        "yn": "(y/N)", "Yn": "(Y/n)",
        "failed_ui": "The interface failed ({err}) — going on in plain mode.",
        "running": "running",
    },
}

ICONS_UNI = {"ok": "✔", "warn": "▲", "fail": "✖", "skip": "–", "info": "•", "todo": "○", "found": "✔", "none": "○",
             "pointer": "❯", "on": "◉", "off": "◯", "locked": "◉", "arrow": "→", "head": "◆"}
ICONS_ASCII = {"ok": "[ok]", "warn": "[!!]", "fail": "[xx]", "skip": "[--]", "info": "*", "todo": "[ ]",
               "found": "[ok]", "none": "[--]", "pointer": ">", "on": "[x]", "off": "[ ]", "locked": "[x]",
               "arrow": "->", "head": "=="}
SPINNER_UNI = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "|/-\\"
LOGO_FONT = {
    "A": ("▄▀█", "█▀█"), "N": ("█▄ █", "█ ▀█"), "T": ("▀█▀", " █ "), "I": ("█", "█"), "G": ("█▀▀", "█▄█"),
    "R": ("█▀█", "█▀▄"), "V": ("█ █", "▀▄▀"), "Y": ("█▄█", " █ "), "P": ("█▀█", "█▀▀"), "L": ("█  ", "█▄▄"),
    "U": ("█ █", "█▄█"), "S": ("█▀", "▄█"), " ": ("  ", "  "),
}
KIND_COLOR = {"ok": GREEN, "warn": AMBER, "fail": RED, "skip": None, "info": VIOLET, "found": GREEN, "none": None}


def logo_lines(text: str = "ANTIGRAVITY PLUS") -> List[str]:
    rows = ["", ""]
    for i, ch in enumerate(text.upper()):
        glyph = LOGO_FONT.get(ch, ("?", "?"))
        sep = "" if i == 0 else " "
        rows[0] += sep + glyph[0]
        rows[1] += sep + glyph[1]
    return rows


class Item:
    """One row of the multi-select: a module."""

    def __init__(self, id: str, title: str, summary: str = "", details: str = "", meta: str = "",
                 locked: bool = False, unavailable: str = "", group: str = "", blocked: bool = False) -> None:
        """unavailable: why it can't work here (a tool it needs is missing) — shown dimmed; blocked: it can't be
        installed at all then (Claude Code's side without Claude Code)."""
        self.id, self.title, self.summary, self.details, self.meta = id, title, summary, details, meta
        self.locked, self.unavailable, self.group, self.blocked = locked, unavailable, group, blocked


class SelectState:
    """The multi-select's state, separate from drawing so tests can drive it with keys."""

    def __init__(self, items: List[Item], chosen: Iterable[str]) -> None:
        self.items = items
        self.chosen: Set[str] = set(chosen) | {it.id for it in items if it.locked}
        self.cursor = 0
        self.offset = 0
        self.message = ""
        self.done: Optional[str] = None  # "ok" | "cancel"
        self.touched: Set[str] = set()

    def key(self, k: str) -> None:
        self.message = ""
        n = len(self.items)
        if k in ("up", "k", "btab"):
            self.cursor = (self.cursor - 1) % n
        elif k in ("down", "j", "tab"):
            self.cursor = (self.cursor + 1) % n
        elif k in ("home", "g"):
            self.cursor = 0
        elif k in ("end", "G"):
            self.cursor = n - 1
        elif k == "pgup":
            self.cursor = max(0, self.cursor - 5)
        elif k == "pgdn":
            self.cursor = min(n - 1, self.cursor + 5)
        elif k in ("space", "x", "right", "left"):
            it = self.items[self.cursor]
            if it.locked:
                self.message = "locked"
            elif it.blocked and it.id not in self.chosen:
                self.message = "cant"
            else:
                self.chosen ^= {it.id}
                self.touched.add(it.id)
        elif k in ("a", "A", "ф", "Ф"):
            self.chosen = {it.id for it in self.items if not it.blocked or it.locked}
            self.touched = {it.id for it in self.items}
        elif k in ("n", "N", "т", "Т"):
            self.chosen = {it.id for it in self.items if it.locked}
            self.touched = {it.id for it in self.items}
        elif k == "enter":
            self.done = "ok"
        elif k in ("esc", "q", "Q", "й", "ctrl-c", "ctrl-d"):
            self.done = "cancel"


def render_select(st: SelectState, cols: int, rows: int, style: Style, lang: str, title: str,
                  icons: Dict[str, str]) -> List[str]:
    """The lines of the multi-select for a cols×rows screen. Every line is shorter than `cols`, and there are
    fewer lines than `rows`, so a redraw never scrolls the screen."""
    t = TEXT[lang]
    w = max(cols - 2, 10)  # one column of left margin, like the sections and frames around it
    compact = cols < 60 or rows < 15
    n_on = len(st.chosen)
    counter = t["chosen"].format(n=n_on, m=len(st.items))
    head = style.accent(icons["head"], bold=True) + " " + style.bold(title)
    gap = w - width(head) - width(counter) - 1
    lines = [truncate(head + " " * max(1, gap) + style.dim(counter), w) if gap > 0 else truncate(head, w)]
    if not compact:
        lines.append("")
    # rows of the list: (group header text, None) or ("", item index)
    entries: List[Tuple[str, Optional[int]]] = []
    last_group = None
    for i, it in enumerate(st.items):
        if not compact and it.group and it.group != last_group and it.group != it.title:
            entries.append((it.group, None))
        last_group = it.group
        entries.append(("", i))
    cur = st.items[st.cursor]
    panel: List[str] = []
    if compact:
        why = cur.unavailable and t["cant"].format(why=cur.unavailable)
        panel = [truncate("  " + style.dim(why or cur.summary), w)]
    else:
        inner = w - 4
        body = wrap(cur.details or cur.summary, inner)
        room = max(2, min(8, rows - 14))
        if cur.unavailable:
            body = [t["cant"].format(why=cur.unavailable)] + body
        if len(body) > room:
            body = body[:room - 1] + [truncate(body[room - 1] + " " + " ".join(body[room:]), inner)]
        label = truncate(" " + cur.title + " ", inner)
        h, v, tl, tr, bl, br = ("─", "│", "╭", "╮", "╰", "╯") if icons is ICONS_UNI else ("-", "|", "+", "+", "+", "+")
        panel = [style.dim(tl + h) + style.accent(label, bold=True) + style.dim(h * max(0, inner + 1 - width(label)) + tr)]
        panel += [style.dim(v) + " " + pad(b, inner) + " " + style.dim(v) for b in body]
        panel.append(style.dim(bl + h * (inner + 2) + br))
    msg = ""
    if st.message == "locked":
        msg = style.paint(t["locked"], AMBER)
    elif st.message == "cant":
        msg = style.paint(t["cant"].format(why=cur.unavailable), AMBER)
    footer = [truncate(style.dim(t["keys"] if cols >= 90 else t["keys_short"]), w)]
    if msg:
        footer.insert(0, truncate(msg, w))
    fixed = len(lines) + len(panel) + len(footer) + (0 if compact else 1)
    view = max(3, rows - 1 - fixed)
    if view < len(entries) and not compact and len(panel) > 3:  # a short screen: shrink the panel first
        spare = len(entries) - view
        while spare > 0 and len(panel) > 3:
            panel.pop(-2)
            spare -= 1
            view += 1
    view = min(view, len(entries))
    cur_row = next(r for r, e in enumerate(entries) if e[1] == st.cursor)
    if cur_row > 0 and entries[cur_row - 1][1] is None:
        cur_row_top = cur_row - 1  # keep the group header in view with its first item
    else:
        cur_row_top = cur_row
    if cur_row_top < st.offset:
        st.offset = cur_row_top
    if cur_row >= st.offset + view:
        st.offset = cur_row - view + 1
    st.offset = max(0, min(st.offset, len(entries) - view))
    shown = entries[st.offset:st.offset + view]
    above, below = st.offset, len(entries) - st.offset - view
    body_lines: List[str] = []
    title_w = max((width(it.title) for it in st.items), default=10) + 1
    for k, (group, idx) in enumerate(shown):
        if idx is None:
            body_lines.append(truncate("  " + style.dim(group.upper() if lang == "en" else group), w))
            continue
        it = st.items[idx]
        on = it.id in st.chosen
        here = idx == st.cursor
        pointer = style.accent(icons["pointer"], bold=True) if here else " "
        mark = icons["locked"] if it.locked else icons["on"] if on else icons["off"]
        mark = style.paint(mark, GREEN if on else None, dim=not on)
        name = it.title
        if it.unavailable and not on:
            name = style.dim(pad(name, title_w))
            meta = style.dim(t["cant"].format(why=it.unavailable))
        else:
            name = style.bold(pad(name, title_w)) if here else pad(name, title_w)
            meta = style.dim((t["required"] + " · " if it.locked else "") + it.meta)
        if compact:
            row = f"{pointer} {mark} {it.title if not (it.unavailable and not on) else style.dim(it.title)}"
        else:
            row = f" {pointer} {mark} {name} {meta}"
        if k == 0 and above:
            row = truncate(row, w - width(t["more_up"].format(n=above)) - 2)
            row = pad(row, w - width(t["more_up"].format(n=above)) - 1) + " " + style.dim(t["more_up"].format(n=above))
        elif k == len(shown) - 1 and below:
            row = truncate(row, w - width(t["more_down"].format(n=below)) - 2)
            row = pad(row, w - width(t["more_down"].format(n=below)) - 1) + " " + style.dim(t["more_down"].format(n=below))
        body_lines.append(truncate(row, w))
    out = lines + body_lines + ([] if compact else [""]) + [truncate(p, w) for p in panel] + footer
    return [" " + ln if ln else "" for ln in out[:max(1, rows - 1)]]


class UI:
    """What install.py shows. Rich: colours, the logo, live steps, keys; plain: one line per event, no escape codes.
    Any failure of the rich side switches to plain (`fail_over`) and the call is repeated there."""

    def __init__(self, caps: Caps, out=None, lang: str = "en") -> None:
        self.caps = caps
        self.out = out or sys.stdout
        self.lang = lang if lang in TEXT else "en"
        self.t = TEXT[self.lang]
        self.style = Style(caps.color, off=not caps.rich)
        self.icons = ICONS_UNI if caps.rich and caps.unicode else ICONS_ASCII
        self.lock = threading.RLock()
        self.cursor_hidden = False
        if caps.rich:
            GUARD.install(self.out)

    @property
    def rich(self) -> bool:
        return self.caps.rich

    # ── output ──
    def write(self, s: str) -> None:
        with self.lock:
            try:
                self.out.write(s)
            except UnicodeEncodeError:  # a console code page without these characters (cp1251, cp437…)
                enc = getattr(self.out, "encoding", None) or "ascii"
                self.out.write(s.encode(enc, errors="replace").decode(enc, errors="replace"))
            try:
                self.out.flush()
            except Exception:
                pass
            if self.caps.rich and "\x1b" in s:
                GUARD.dirty = True

    def line(self, s: str = "") -> None:
        self.write(s + "\n")

    def hide_cursor(self) -> None:
        if self.rich and not self.cursor_hidden:
            self.write("\x1b[?25l")
            self.cursor_hidden = True

    def show_cursor(self) -> None:
        if self.cursor_hidden:
            self.cursor_hidden = False
            self.write("\x1b[?25h")

    def close(self) -> None:
        self.show_cursor()
        GUARD.restore()

    def fail_over(self, err: BaseException) -> None:
        """The rich interface broke: put the terminal back and go on plain."""
        try:
            GUARD.restore()
            if self.cursor_hidden:
                self.out.write("\x1b[0m\x1b[?25h\n")
        except Exception:
            pass
        self.cursor_hidden = False
        self.caps = Caps("plain", None, False, False, f"interface error: {err!r}")
        self.style = Style(None, off=True)
        self.icons = ICONS_ASCII
        self.line(self.t["failed_ui"].format(err=type(err).__name__))

    def safe(self, rich_fn: Callable, plain_fn: Callable, *args, **kw):
        if self.rich:
            try:
                return rich_fn(*args, **kw)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:  # never let the interface stop an install
                self.fail_over(e)
        return plain_fn(*args, **kw)

    def width(self) -> int:
        return term_size()[0] if self.rich else 100

    def icon(self, kind: str) -> str:
        ic = self.icons.get(kind, kind)
        rgb = KIND_COLOR.get(kind)
        return self.style.paint(ic, rgb, bold=kind in ("ok", "fail", "warn")) if rgb else self.style.dim(ic)

    # ── header ──
    def banner(self, version: str, tagline: str, subtitle: str = "") -> None:
        self.safe(self._banner_rich, self._banner_plain, version, tagline, subtitle)

    def _banner_plain(self, version: str, tagline: str, subtitle: str = "") -> None:
        self.line(f"Antigravity Plus {version} — {tagline}")
        if subtitle:
            self.line(subtitle)

    def _banner_rich(self, version: str, tagline: str, subtitle: str = "") -> None:
        cols = self.width()
        s = self.style
        self.line()
        logo = logo_lines()
        if cols >= width(logo[0]) + 4:
            span = width(logo[0])
            for row in logo:
                self.line("  " + s.gradient(row, span=span))
            self.line()
            self.line(truncate("  " + s.paint("v" + version, VIOLET, bold=True) + s.dim("  ·  ") + tagline, cols - 1))
        else:
            self.line(truncate("  " + s.gradient("✦ Antigravity Plus", bold=True) + "  " + s.dim("v" + version), cols - 1))
            for ln in wrap(tagline, cols - 5):
                self.line("  " + ln)
        if subtitle:
            for ln in wrap(subtitle, cols - 5):
                self.line("  " + s.dim(ln))
        self.line()

    def section(self, title: str) -> None:
        if self.rich:
            try:
                self.line(truncate(" " + self.style.accent(self.icons["head"], bold=True) + " " + self.style.bold(title),
                                   self.width() - 1))
                return
            except Exception as e:
                self.fail_over(e)
        self.line(title)

    def rows(self, rows: Sequence[Tuple[str, str, str]], indent: int = 3) -> None:
        """Aligned rows: (kind, name, detail) — kind is ok/warn/fail/skip/found/none/info."""
        self.safe(self._rows_rich, self._rows_plain, rows, indent)

    def _rows_plain(self, rows, indent: int = 3) -> None:
        for kind, name, detail in rows:
            detail = detail[2:] if detail.startswith(("→ ", "-> ")) else detail
            self.line(f"{ICONS_ASCII.get(kind, kind)} {name}" + (f" — {detail}" if detail else ""))

    def _rows_rich(self, rows, indent: int = 3) -> None:
        cols = self.width()
        name_w = max((width(r[1]) for r in rows), default=0) + 2
        stacked = cols - 1 - indent - 2 - name_w < 24  # too narrow for name and detail side by side
        for kind, name, detail in rows:
            head = " " * indent + self.icon(kind) + " " + (name if stacked else pad(name, name_w))
            if stacked and detail:
                self.line(truncate(head.rstrip(), cols - 1))
                self.line(truncate(" " * (indent + 2) + self.style.dim(detail), cols - 1))
            else:
                self.line(truncate(head + self.style.dim(detail), cols - 1))

    def note(self, text: str, kind: str = "info") -> None:
        if self.rich:
            try:
                cols = self.width()
                for i, ln in enumerate(wrap(text, cols - 6)):
                    self.line("   " + (self.icon(kind) if i == 0 else " ") + " " + ln)
                return
            except Exception as e:
                self.fail_over(e)
        self.line(text)

    def paragraph(self, text: str, indent: int = 5, dim: bool = True) -> None:
        """A wrapped block of text (a module's description)."""
        if self.rich:
            try:
                cols = self.width()
                for ln in wrap(text, max(20, min(cols - indent - 2, 96))):
                    self.line(" " * indent + (self.style.dim(ln) if dim else ln))
                return
            except Exception as e:
                self.fail_over(e)
        self.line(" " * indent + text)

    # ── steps ──
    def step(self, label: str) -> "Step":
        return Step(self, label)

    # ── questions ──
    def select(self, items: List[Item], chosen: Iterable[str], title: str) -> Optional[Tuple[Set[str], Set[str]]]:
        """The module multi-select. (chosen, touched) or None = cancelled. Raises NotImplementedError where keys can't
        be read (plain or no stdin terminal) — the caller falls back to its line menu."""
        if not (self.rich and self.caps.interactive):
            raise NotImplementedError("no interactive terminal")
        try:
            return self._select_rich(items, chosen, title)
        except (KeyboardInterrupt, SystemExit, NotImplementedError):
            raise
        except Exception as e:
            self.fail_over(e)
            raise NotImplementedError("the interface failed") from e

    def _select_rich(self, items: List[Item], chosen: Iterable[str], title: str):
        st = SelectState(items, chosen)
        live = Live(self)
        resized = [False]
        old_winch = None
        winch = getattr(signal, "SIGWINCH", None)
        if winch is not None:
            try:
                old_winch = signal.signal(winch, lambda *_: resized.__setitem__(0, True))
            except (ValueError, OSError):
                winch = None
        self.hide_cursor()
        try:
            with KeyReader() as keys:
                size = term_size()
                live.draw(render_select(st, size[0], size[1], self.style, self.lang, title, self.icons))
                while st.done is None:
                    pressed = keys.read(0.1)
                    now = term_size()
                    if not pressed and now == size and not resized[0]:
                        continue
                    resized[0] = False
                    size = now
                    for k in pressed:
                        st.key(k)
                        if st.done:
                            break
                    live.draw(render_select(st, size[0], size[1], self.style, self.lang, title, self.icons))
        finally:
            if winch is not None:
                try:
                    signal.signal(winch, old_winch or signal.SIG_DFL)
                except (ValueError, OSError):
                    pass
            try:
                live.clear()
            finally:
                self.show_cursor()
        if st.done != "ok":
            self.line(truncate(" " + self.icon("skip") + " " + title + self.style.dim(" · " + self.t["cancelled"]),
                               self.width() - 1))
            return None
        names = [it.title for it in items if it.id in st.chosen]
        summary = (" " + self.icon("ok") + " " + self.style.bold(title) + self.style.dim(
            " · " + self.t["chosen"].format(n=len(st.chosen), m=len(items)) + " — ") + ", ".join(names))
        self.line(truncate(summary, self.width() - 1))
        return set(st.chosen), set(st.touched)

    def confirm(self, question: str, default: bool = False) -> Optional[bool]:
        """Yes/no with one key. Plain or no keyboard: the default. None = cancelled (esc, Ctrl-C)."""
        if not (self.rich and self.caps.interactive):
            return default
        try:
            return self._confirm_rich(question, default)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            self.fail_over(e)
            return default

    def _confirm_rich(self, question: str, default: bool) -> Optional[bool]:
        hint = self.t["Yn"] if default else self.t["yn"]
        prompt = " " + self.style.accent("?", bold=True) + " " + self.style.bold(question) + " " + self.style.dim(hint) + " "
        live = Live(self)
        answer: Optional[bool] = None
        self.hide_cursor()
        try:
            with KeyReader() as keys:
                live.draw([truncate(prompt, self.width() - 1)])
                while True:
                    ks = keys.read(0.2)
                    if not ks:
                        continue
                    k = ks[0]
                    if k in ("y", "Y", "д", "Д", "l", "L"):
                        answer = True
                    elif k in ("n", "N", "н", "Н", "т", "Т"):
                        answer = False
                    elif k == "enter":
                        answer = default
                    elif k in ("esc", "ctrl-c", "ctrl-d", "q"):
                        answer = None
                    else:
                        continue
                    break
        finally:
            live.clear()
            self.show_cursor()
        said = self.t["cancelled"] if answer is None else self.t["yes"] if answer else self.t["no"]
        kind = "skip" if answer is None else "ok"
        self.line(truncate(" " + self.icon(kind) + " " + question + " " + self.style.dim(said), self.width() - 1))
        return answer

    # ── closing frame ──
    def box(self, title: str, lines: Sequence, kind: str = "ok") -> None:
        """The closing frame: lines are text (wrapped) or (label, value) pairs (aligned)."""
        self.safe(self._box_rich, self._box_plain, title, lines, kind)

    def _box_plain(self, title: str, lines: Sequence, kind: str = "ok") -> None:
        self.line()
        self.line(f"{ICONS_ASCII.get(kind, '')} {title}")
        for ln in lines:
            if isinstance(ln, tuple):
                self.line(f"  {ln[0]}: {ln[1]}")
            else:
                self.line("  " + strip_ansi(ln) if ln else "")

    def _box_rich(self, title: str, lines: Sequence, kind: str = "ok") -> None:
        cols = self.width()
        inner = max(20, min(cols - 6, 78))
        s = self.style
        rgb = KIND_COLOR.get(kind) or VIOLET
        uni = self.icons is ICONS_UNI
        h, v, tl, tr, bl, br = ("─", "│", "╭", "╮", "╰", "╯") if uni else ("-", "|", "+", "+", "+", "+")
        head = " " + self.icon(kind) + " " + s.bold(title) + " "
        head = truncate(head, inner)
        self.line()
        self.line(" " + s.paint(tl + h, rgb) + head + s.paint(h * max(0, inner + 1 - width(head)) + tr, rgb))
        body: List[str] = []
        label_w = max((width(ln[0]) for ln in lines if isinstance(ln, tuple)), default=0) + 2
        for ln in lines:
            if isinstance(ln, tuple):  # (label, value): the value wraps under itself
                parts = wrap(str(ln[1]), max(10, inner - label_w)) or [""]
                body.append(s.dim(pad(ln[0], label_w)) + parts[0])
                body += [" " * label_w + p for p in parts[1:]]
                continue
            if ln and not strip_ansi(ln).strip():
                body.append("")
                continue
            if "\x1b" in ln:
                body.append(truncate(ln, inner))
            else:
                lead = len(ln) - len(ln.lstrip(" "))
                body += [" " * lead + w for w in wrap(ln.strip(), inner - lead)] if ln.strip() else [""]
        for ln in body:
            self.line(" " + s.paint(v, rgb) + " " + pad(ln, inner) + " " + s.paint(v, rgb))
        self.line(" " + s.paint(bl + h * (inner + 2) + br, rgb))


class Live:
    """A block of lines redrawn in place (no alt-screen): moves the cursor back over what it drew, clears to the end
    of the screen and writes the new lines. Lines must be shorter than the terminal; a resize is accounted for by
    counting how many rows the old lines wrap to at the new width."""

    def __init__(self, ui: UI) -> None:
        self.ui = ui
        self.prev: List[int] = []  # cell widths of the lines drawn last

    def _rows_used(self) -> int:
        cols = term_size()[0]
        return sum(max(1, math.ceil(w / cols)) if w else 1 for w in self.prev)

    def clear(self) -> None:
        if not self.prev:
            return
        up = self._rows_used() - 1
        self.ui.write("\r" + (f"\x1b[{up}A" if up > 0 else "") + "\x1b[J")
        self.prev = []

    def draw(self, lines: List[str]) -> None:
        up = self._rows_used() - 1 if self.prev else -1
        buf = ("\r" + (f"\x1b[{up}A" if up > 0 else "") + "\x1b[J") if up >= 0 else ""
        buf += "\n".join(lines)
        self.ui.write(buf)
        self.prev = [width(ln) for ln in lines]


class Step:
    """One step of the install: a spinner while it runs, then ✔/▲/✖/– with a short detail. Output of what it runs
    is captured by the caller and shown only on failure (`fail(body=…)`), so the spinner never mixes with it.
    The spinner thread always stops: in __exit__, whatever happened."""

    def __init__(self, ui: UI, label: str) -> None:
        self.ui, self.label = ui, label
        self.status: Optional[str] = None
        self.detail = ""
        self.body: List[str] = []
        self.hint = ""
        self.live_detail = ""
        self.started = 0.0
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def __enter__(self) -> "Step":
        self.started = time.time()
        if self.ui.rich:
            try:
                self.ui.hide_cursor()
                self._frame(0)
                self.thread = threading.Thread(target=self._spin, name="agp-spinner", daemon=True)
                self.thread.start()
            except Exception as e:
                self._halt()
                self.ui.fail_over(e)
        return self

    def _frame(self, i: int) -> None:
        frames = SPINNER_UNI if self.ui.icons is ICONS_UNI else SPINNER_ASCII
        spin = self.ui.style.paint(frames[i % len(frames)], VIOLET, bold=True)
        took = time.time() - self.started
        extra = (" " + self.live_detail if self.live_detail else "") + (f"  {int(took)} s" if took >= 2 else "")
        text = "   " + spin + " " + self.label + self.ui.style.dim(extra)
        self.ui.write("\r\x1b[2K" + truncate(text, self.ui.width() - 1))

    def _spin(self) -> None:
        i = 1
        while not self.stop.wait(0.08):
            try:
                self._frame(i)
            except Exception:
                return
            i += 1

    def _halt(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None

    def update(self, text: str) -> None:
        """A short live detail next to the spinner (e.g. «412 files»)."""
        self.live_detail = text

    def ok(self, detail: str = "") -> None:
        self.status, self.detail = "ok", detail

    def warn(self, detail: str = "", body: Sequence[str] = (), hint: str = "") -> None:
        self.status, self.detail, self.body, self.hint = "warn", detail, list(body), hint

    def fail(self, detail: str = "", body: Sequence[str] = (), hint: str = "") -> None:
        self.status, self.detail, self.body, self.hint = "fail", detail, list(body), hint

    def skip(self, detail: str = "") -> None:
        self.status, self.detail = "skip", detail

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._halt()
        if exc_type is not None and self.status in (None, "ok"):
            if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
                self.status, self.detail = "skip", self.ui.t["cancelled"]
            else:
                self.status, self.detail = "fail", f"{exc_type.__name__}: {exc}"
        status = self.status or "ok"
        if self.ui.rich:
            try:
                self._finish_rich(status)
                return False
            except Exception as e:
                self.ui.fail_over(e)
        self._finish_plain(status)
        return False

    def _finish_rich(self, status: str) -> None:
        ui, s = self.ui, self.ui.style
        cols = ui.width()
        took = time.time() - self.started
        timing = f"  {took:.1f} s" if took >= 1 else ""
        line = "   " + ui.icon(status) + " " + (s.dim(self.label) if status == "skip" else self.label)
        if self.detail or timing:
            line += "  " + s.dim(self.detail + timing)
        ui.write("\r\x1b[2K" + truncate(line, cols - 1) + "\n")
        bar = s.paint("│", RED if status == "fail" else AMBER) if ui.icons is ICONS_UNI else "|"
        tail = self.body[-20:]
        if len(self.body) > 20:
            ui.line("     " + bar + " " + s.dim(f"… {len(self.body) - 20} more lines"))
        for ln in tail:
            for part in wrap(strip_ansi(ln), cols - 8) or [""]:
                ui.line("     " + bar + " " + part)
        if self.hint:
            for i, part in enumerate(wrap(self.hint, cols - 8)):
                ui.line("     " + (s.accent(ui.icons["arrow"]) if i == 0 else " ") + " " + part)
        ui.show_cursor()

    def _finish_plain(self, status: str) -> None:
        ui = self.ui
        ui.line(f"{ICONS_ASCII[status]} {self.label}" + (f" — {self.detail}" if self.detail else ""))
        for ln in self.body[-20:]:
            ui.line("    " + strip_ansi(ln))
        if self.hint:
            ui.line("    -> " + self.hint)
