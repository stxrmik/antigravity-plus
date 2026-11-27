"""Tiny VT100/ANSI terminal screen emulator for offline PTY tests (stdlib only)."""
from __future__ import annotations

import os
import re
import sys
from typing import List

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import installer_ui as tui  # noqa: E402

_CSI_RE = re.compile(r"\x1b\[([0-?]*[ -/]*)([@-~])")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ESC2_RE = re.compile(r"\x1b[@-Z\\-_]")


def _parse_params(raw: str) -> List[int]:
    if not raw:
        return []
    out: List[int] = []
    for part in raw.split(";"):
        try:
            out.append(int(part) if part else 0)
        except ValueError:
            out.append(0)
    return out


def _split_incomplete(data: str) -> tuple:
    i = data.rfind("\x1b")
    if i < 0:
        return data, ""
    rest = data[i:]
    if rest == "\x1b":
        return data[:i], rest
    if rest.startswith("\x1b["):
        if not _CSI_RE.match(rest):
            return data[:i], rest
    elif rest.startswith("\x1b]"):
        if not _OSC_RE.match(rest):
            return data[:i], rest
    return data, ""


class Screen:
    """Minimal VT100/ANSI screen buffer with scrollback."""

    def __init__(self, cols: int, rows: int) -> None:
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.grid: List[List[str]] = [[" "] * self.cols for _ in range(self.rows)]
        self.scrollback: List[List[str]] = []
        self.row = 0
        self.col = 0
        self._pending = ""

    def _lf(self) -> None:
        if self.row < self.rows - 1:
            self.row += 1
        else:
            self.scrollback.append(self.grid.pop(0))
            self.grid.append([" "] * self.cols)

    def _clear_cell(self, r: int, c: int) -> None:
        if not (0 <= r < self.rows and 0 <= c < self.cols):
            return
        line = self.grid[r]
        if line[c] == "" and c > 0:
            line[c - 1] = " "
        elif c + 1 < self.cols and line[c + 1] == "":
            line[c + 1] = " "
        line[c] = " "

    def _put_cluster(self, ch: str) -> None:
        w = tui.width(ch)
        if w <= 0:
            return
        if w > self.cols:
            w = self.cols
        if self.col + w > self.cols:
            self.col = 0
            self._lf()
        self._clear_cell(self.row, self.col)
        if w == 2 and self.col + 1 < self.cols:
            self._clear_cell(self.row, self.col + 1)
        self.grid[self.row][self.col] = ch
        if w == 2 and self.col + 1 < self.cols:
            self.grid[self.row][self.col + 1] = ""
        self.col += w

    def _handle_csi(self, params_raw: str, final: str) -> None:
        if params_raw.startswith(("?", ">", "=", "<")):
            return  # private modes like ?25l / ?25h
        params = _parse_params(params_raw)
        p0 = params[0] if params else 0
        if final == "A":
            self.row = max(0, self.row - (p0 or 1))
        elif final == "B":
            self.row = min(self.rows - 1, self.row + (p0 or 1))
        elif final == "C":
            self.col = min(self.cols - 1, self.col + (p0 or 1))
        elif final == "D":
            self.col = max(0, self.col - (p0 or 1))
        elif final == "E":
            self.row = min(self.rows - 1, self.row + (p0 or 1))
            self.col = 0
        elif final == "F":
            self.row = max(0, self.row - (p0 or 1))
            self.col = 0
        elif final == "G":
            self.col = max(0, min(self.cols - 1, (p0 or 1) - 1))
        elif final == "H":
            r = (params[0] if len(params) >= 1 and params[0] else 1) - 1
            c = (params[1] if len(params) >= 2 and params[1] else 1) - 1
            self.row = max(0, min(self.rows - 1, r))
            self.col = max(0, min(self.cols - 1, c))
        elif final == "K":
            if p0 == 0:
                for c in range(min(self.col, self.cols), self.cols):
                    self._clear_cell(self.row, c)
            elif p0 == 1:
                for c in range(0, min(self.col + 1, self.cols)):
                    self._clear_cell(self.row, c)
            elif p0 == 2:
                self.grid[self.row] = [" "] * self.cols
        elif final == "J":
            if p0 == 0:
                for c in range(min(self.col, self.cols), self.cols):
                    self._clear_cell(self.row, c)
                for r in range(self.row + 1, self.rows):
                    self.grid[r] = [" "] * self.cols
            elif p0 == 1:
                for r in range(0, self.row):
                    self.grid[r] = [" "] * self.cols
                for c in range(0, min(self.col + 1, self.cols)):
                    self._clear_cell(self.row, c)
            elif p0 == 2:
                self.grid = [[" "] * self.cols for _ in range(self.rows)]

    def feed(self, text: str) -> None:
        if not text:
            return
        data, self._pending = _split_incomplete(self._pending + text)
        i, n = 0, len(data)
        while i < n:
            ch = data[i]
            if ch == "\x1b":
                m = _CSI_RE.match(data, i)
                if m:
                    self._handle_csi(m.group(1), m.group(2))
                    i = m.end()
                    continue
                m = _OSC_RE.match(data, i)
                if m:
                    i = m.end()
                    continue
                m = _ESC2_RE.match(data, i)
                if m:
                    i = m.end()
                    continue
                i += 1
                continue
            if ch == "\r":
                self.col = 0
                i += 1
                continue
            if ch == "\n":
                self._lf()
                i += 1
                continue
            if ch == "\x08":
                self.col = max(0, self.col - 1)
                i += 1
                continue
            if ord(ch) < 32 or ord(ch) == 0x7F:
                i += 1
                continue
            j = i
            while j < n and data[j] not in ("\x1b", "\r", "\n", "\x08") and ord(data[j]) >= 32 and ord(data[j]) != 0x7F:
                j += 1
            for cluster in tui.clusters(data[i:j]):
                self._put_cluster(cluster)
            i = j

    def lines(self) -> List[str]:
        return ["".join(r).rstrip() for r in self.scrollback + self.grid]
