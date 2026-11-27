"""Offline tests for installer_ui.py and install.py (--demo, --plain, rich PTY)."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

PLUGIN, SP = sys.argv[1], sys.argv[2]
ROOT = os.path.dirname(PLUGIN)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "probe"))

import installer_ui as tui  # noqa: E402
import vt_screen  # noqa: E402

ok = True


def check(name: str, cond: bool, got: object = "") -> None:
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + ("" if cond else f" → {str(got)[-600:]}"))


# ── 1. Pure text/width/wrap/truncate/pad functions ──

check("width: ASCII, Cyrillic=1, CJK=2, emoji=2, VS16=2, ANSI=0",
      tui.width("") == 0
      and tui.width("hello") == 5
      and tui.width("Привет") == 6
      and tui.width("中") == 2
      and tui.width("🚀") == 2
      and tui.width("⚠️") == 2
      and tui.width("\x1b[38;2;66;133;244mabc\x1b[0m") == 3,
      (tui.width("Привет"), tui.width("中"), tui.width("🚀"), tui.width("⚠️"),
       tui.width("\x1b[38;2;66;133;244mabc\x1b[0m")))

tr1 = tui.truncate("Hello world, this is long", 10)
tr2 = tui.truncate("\x1b[1m\x1b[38;2;1;2;3mHello world\x1b[0m", 8)
tr3 = tui.truncate("Привет мир", 7)
check("truncate: adds ellipsis, respects width, preserves and closes ANSI",
      tr1.endswith("…") and tui.width(tr1) <= 10
      and tr2.startswith("\x1b[1m") and tr2.endswith("…\x1b[0m") and tui.width(tr2) <= 8
      and tr3.endswith("…") and tui.width(tr3) <= 7,
      (tr1, repr(tr2), tr3))

p_short = tui.pad("hi", 6)
p_long = tui.pad("hello world", 6)
p_cjk = tui.pad("中a", 5)
check("pad: exact cell width for short, long, and wide strings",
      tui.width(p_short) == 6 and p_short == "hi    "
      and tui.width(p_long) == 6 and p_long.endswith("…")
      and tui.width(p_cjk) == 5,
      (repr(p_short), repr(p_long), repr(p_cjk)))

wrapped = tui.wrap("one two supercalifragilisticexpialidocious three four", 10)
check("wrap: wraps by width, splits long words, no line wider than w",
      len(wrapped) >= 4 and all(tui.width(ln) <= 10 for ln in wrapped)
      and "supercalif" in wrapped,
      wrapped)

# ── 2. Key parsing & incomplete ──

check("parse_keys: CSI and SS3 arrows + modified arrow",
      tui.parse_keys("\x1b[A\x1b[B\x1b[C\x1b[D") == ["up", "down", "right", "left"]
      and tui.parse_keys("\x1bOA\x1bOB\x1bOC\x1bOD") == ["up", "down", "right", "left"]
      and tui.parse_keys("\x1b[1;5A") == ["up"],
      (tui.parse_keys("\x1b[A\x1b[B\x1b[C\x1b[D"), tui.parse_keys("\x1bOA"), tui.parse_keys("\x1b[1;5A")))

check("parse_keys: home/end/pgup/pgdn/delete tilde and CSI forms",
      tui.parse_keys("\x1b[1~\x1b[7~\x1b[4~\x1b[8~\x1b[5~\x1b[6~\x1b[3~\x1b[H\x1b[F")
      == ["home", "home", "end", "end", "pgup", "pgdn", "delete", "home", "end"],
      tui.parse_keys("\x1b[1~\x1b[7~\x1b[4~\x1b[8~\x1b[5~\x1b[6~\x1b[3~\x1b[H\x1b[F"))

check("parse_keys: enter (CR/LF/CRLF), space, backspace, ctrl-c, esc, alt-x, unknown CSI dropped",
      tui.parse_keys("\r") == ["enter"]
      and tui.parse_keys("\n") == ["enter"]
      and tui.parse_keys("\r\n") == ["enter"]
      and tui.parse_keys(" ") == ["space"]
      and tui.parse_keys("\x7f") == ["backspace"]
      and tui.parse_keys("\x03") == ["ctrl-c"]
      and tui.parse_keys("\x1b") == ["esc"]
      and tui.parse_keys("\x1bx") == ["alt-x"]
      and tui.parse_keys("\x1b[999~a\x1b[?1;2cb") == ["a", "b"],
      (tui.parse_keys("\r\n"), tui.parse_keys("\x1bx"), tui.parse_keys("\x1b[999~a\x1b[?1;2cb")))

check("incomplete: detects partial vs complete escape sequences",
      tui.incomplete("\x1b[") is True
      and tui.incomplete("\x1b[A") is False
      and tui.incomplete("\x1b") is True
      and tui.incomplete("\x1bO") is True
      and tui.incomplete("\x1bOA") is False,
      (tui.incomplete("\x1b["), tui.incomplete("\x1b[A")))

# ── 3. detect() matrix ──

d_tty = tui.detect(env={"TERM": "xterm"}, stdin_tty=True, stdout_tty=True, plain=False, encoding="utf-8", windows=False)
d_dumb = tui.detect(env={"TERM": "dumb"}, stdin_tty=True, stdout_tty=True, encoding="utf-8", windows=False)
d_claude = tui.detect(env={"TERM": "xterm", "CLAUDECODE": "1"}, stdin_tty=True, stdout_tty=True, encoding="utf-8", windows=False)
d_agy = tui.detect(env={"TERM": "xterm-256color", "ANTIGRAVITY_AGENT": "1"}, stdin_tty=True, stdout_tty=True, encoding="utf-8", windows=False)
d_ci = tui.detect(env={"TERM": "xterm", "CI": "1"}, stdin_tty=True, stdout_tty=True, encoding="utf-8", windows=False)
d_notty = tui.detect(env={"TERM": "xterm"}, stdin_tty=True, stdout_tty=False, encoding="utf-8", windows=False)
d_plain = tui.detect(env={"TERM": "xterm"}, stdin_tty=True, stdout_tty=True, plain=True, encoding="utf-8", windows=False)
d_nostdin = tui.detect(env={"TERM": "xterm"}, stdin_tty=False, stdout_tty=True, encoding="utf-8", windows=False)
d_nocolor = tui.detect(env={"TERM": "xterm-256color", "NO_COLOR": "1"}, stdin_tty=True, stdout_tty=True, encoding="utf-8", windows=False)
d_tc = tui.detect(env={"TERM": "xterm", "COLORTERM": "truecolor"}, stdin_tty=True, stdout_tty=True, encoding="utf-8", windows=False)
d_256 = tui.detect(env={"TERM": "xterm-256color"}, stdin_tty=True, stdout_tty=True, encoding="utf-8", windows=False)
d_16 = tui.detect(env={"TERM": "xterm"}, stdin_tty=True, stdout_tty=True, encoding="utf-8", windows=False)
d_cp1251 = tui.detect(env={"TERM": "xterm"}, stdin_tty=True, stdout_tty=True, encoding="cp1251", windows=False)
d_win_novt = tui.detect(env={"TERM": "xterm"}, stdin_tty=True, stdout_tty=True, encoding="utf-8", windows=True, vt=lambda: False)
d_force_p = tui.detect(env={"TERM": "xterm", "AGP_INSTALL_UI": "plain"}, stdin_tty=True, stdout_tty=True, encoding="utf-8", windows=False)
d_force_r = tui.detect(env={"TERM": "dumb", "AGP_INSTALL_UI": "rich"}, stdin_tty=False, stdout_tty=False, encoding="utf-8", windows=False)

check("detect: TTY+TTY+xterm -> rich interactive",
      d_tty.rich and d_tty.interactive and d_tty.color == "16", d_tty)
check("detect: TERM=dumb / CLAUDECODE / CI / stdout not TTY / plain=True -> plain",
      not d_dumb.rich and not d_claude.rich and not d_ci.rich and not d_notty.rich and not d_plain.rich,
      (d_dumb, d_claude, d_ci, d_notty, d_plain))
check("detect: an Antigravity CLI agent's command (ANTIGRAVITY_AGENT=1) -> plain, never a menu",
      not d_agy.rich and not d_agy.interactive and d_agy.reason == "ANTIGRAVITY_AGENT", d_agy)
_env = dict(os.environ)
os.environ.update(TERM="xterm-256color", ANTIGRAVITY_AGENT="1")
for _v in ("CLAUDECODE", "CI"):
    os.environ.pop(_v, None)
import install as _inst
_inst_tty = (sys.stdin.isatty, sys.stdout.isatty)
sys.stdin.isatty = sys.stdout.isatty = lambda: True  # type: ignore
try:
    check("install.interactive_terminal(): no menu under ANTIGRAVITY_AGENT=1", _inst.interactive_terminal() is False)
finally:
    sys.stdin.isatty, sys.stdout.isatty = _inst_tty  # type: ignore
    os.environ.clear(); os.environ.update(_env)
# ── the installer's language: the system's, unless chosen ──
import install as _li
L = _li.lang_of_system
_bad_plist = os.path.join(SP, "bad.plist"); open(_bad_plist, "wb").write(b"not a plist")
_none = lambda: None
check("language: macOS ru-RU with an empty LANG -> ru", L("darwin", {}, apple=lambda: ["ru-RU", "en-US"]) == "ru")
check("language: macOS en-US beats LANG=ru_RU.UTF-8", L("darwin", {"LANG": "ru_RU.UTF-8"}, apple=lambda: ["en-US"]) == "en")
check("language: Linux LC_ALL=en_US beats LANG=ru_RU", L("linux", {"LC_ALL": "en_US", "LANG": "ru_RU"}) == "en")
check("language: LANGUAGE=ru:en -> ru", L("linux", {"LANGUAGE": "ru:en", "LANG": "en_US.UTF-8"}) == "ru")
check("language: C / POSIX don't count -> English", L("linux", {"LC_ALL": "C", "LANG": "POSIX"}, getlocale=lambda: "") == "en"
      and L("linux", {"LC_ALL": "C", "LANG": "ru_RU.UTF-8"}) == "ru")
check("language: a broken or missing plist doesn't fail, the environment decides",
      _li.apple_languages(_bad_plist) == [] and _li.apple_languages(os.path.join(SP, "none.plist")) == []
      and L("darwin", {"LANG": "ru_RU.UTF-8"}, apple=lambda: _li.apple_languages(_bad_plist)) == "ru"
      and L("darwin", {}, apple=lambda: 1 / 0, getlocale=lambda: "") == "en")
check("language: Windows UI language 0x419 -> ru, 0x409 -> en, Russian_Russia locale -> ru",
      L("win32", {}, win=lambda: 0x0419) == "ru" and L("win32", {"LANG": "ru_RU"}, win=lambda: 0x0409) == "en"
      and L("win32", {}, win=_none, getlocale=lambda: "Russian_Russia") == "ru")
check("language: --lang and the saved explicit choice beat the system",
      _li.pick_language("en", "ru", platform="darwin", env={}, apple=lambda: ["ru-RU"]) == "en"
      and _li.pick_language(None, "en", platform="darwin", env={}, apple=lambda: ["ru-RU"]) == "en"
      and _li.pick_language(None, None, platform="darwin", env={}, apple=lambda: ["ru-RU"]) == "ru")
check("detect: stdin not TTY -> rich without interactive",
      d_nostdin.rich and not d_nostdin.interactive, d_nostdin)
check("detect: NO_COLOR=1 -> rich with color=None",
      d_nocolor.rich and d_nocolor.color is None, d_nocolor)
check("detect: color depths truecolor / 256 / 16",
      d_tc.color == "truecolor" and d_256.color == "256" and d_16.color == "16",
      (d_tc.color, d_256.color, d_16.color))
check("detect: cp1251 -> plain; windows without VT -> plain",
      not d_cp1251.rich and not d_win_novt.rich, (d_cp1251, d_win_novt))
check("detect: AGP_INSTALL_UI=plain/rich overrides",
      not d_force_p.rich and d_force_r.rich and not d_force_r.interactive, (d_force_p, d_force_r))

# ── 4. Color degradation ──

c_tc = tui.fg(tui.BLUE, "truecolor")
c_256 = tui.fg(tui.BLUE, "256")
m_256 = re.search(r"\x1b\[38;5;(\d+)m", c_256)
c16_codes = []
for sample in (tui.BLUE, tui.VIOLET, tui.ROSE, tui.GREEN, tui.AMBER, tui.RED, (0, 0, 0), (255, 255, 255)):
    m = re.match(r"^\x1b\[(\d+)m$", tui.fg(sample, "16"))
    c16_codes.append(int(m.group(1)) if m else -1)
valid_16 = set(range(31, 37)) | set(range(91, 97))
check("fg: truecolor (38;2;), 256 (38;5;16..255), 16 (31..36|91..96), None -> empty",
      "38;2;" in c_tc
      and m_256 is not None and 16 <= int(m_256.group(1)) <= 255
      and all(c in valid_16 for c in c16_codes)
      and tui.fg(tui.BLUE, None) == "",
      (repr(c_tc), repr(c_256), c16_codes))

# ── 5. SelectState + render_select ──

items = [
    tui.Item("core", "Core", "Core summary", "Core details", "required", locked=True, group="g1"),
    tui.Item("alpha", "Alpha", "Alpha summary", "Alpha details", "100 tokens", group="g1"),
    tui.Item("beta", "Beta", "Beta summary", "Beta details", "200 tokens", group="g2"),
    tui.Item("gamma", "Gamma", "Gamma summary", "Gamma details", "300 tokens", group="g2"),
    tui.Item("delta", "Delta", "Delta summary", "Delta details", "400 tokens", group="g2"),
    tui.Item("eps", "Epsilon", "Epsilon summary", "Epsilon details", "500 tokens", group="g2"),
    tui.Item("zeta", "Zeta", "Zeta summary", "Zeta details", "600 tokens", group="g2"),
    tui.Item("blocked", "Blocked", "Blocked summary", "Blocked details", "outside",
             unavailable="no tool", blocked=True, group="g3"),
]

st = tui.SelectState(items, ["core", "alpha"])
st.key("space")  # cursor at 0 (core, locked)
core_locked_ok = st.message == "locked" and "core" in st.chosen
st.key("down")   # cursor at 1 (alpha)
st.key("space")  # toggle alpha off
alpha_off_ok = "alpha" not in st.chosen
st.key("end")    # cursor at last (blocked)
st.key("space")  # try enable blocked
blocked_ok = st.message == "cant" and "blocked" not in st.chosen
st.key("a")      # all non-blocked
all_ok = "alpha" in st.chosen and "beta" in st.chosen and "blocked" not in st.chosen
st.key("n")      # required only
req_ok = st.chosen == {"core"}
st.key("enter")
enter_ok = st.done == "ok"
st_cancel = tui.SelectState(items, ["core"])
st_cancel.key("esc")
esc_ok = st_cancel.done == "cancel"

check("SelectState: down/space/a/n/enter/esc, locked stays, blocked stays off",
      core_locked_ok and alpha_off_ok and blocked_ok and all_ok and req_ok and enter_ok and esc_ok,
      (core_locked_ok, alpha_off_ok, blocked_ok, all_ok, req_ok, enter_ok, esc_ok))

style_tc = tui.Style("truecolor")
lines_compact = tui.render_select(tui.SelectState(items, ["core"]), 50, 12, style_tc, "en", "Pick", tui.ICONS_UNI)
lines_wide = tui.render_select(tui.SelectState(items, ["core"]), 100, 20, style_tc, "en", "Pick", tui.ICONS_UNI)
check("render_select: fits within cols and rows for 50x12 (compact) and 100x20",
      len(lines_compact) < 12 and all(tui.width(ln) < 50 for ln in lines_compact)
      and len(lines_wide) < 20 and all(tui.width(ln) < 100 for ln in lines_wide),
      (len(lines_compact), max(tui.width(ln) for ln in lines_compact),
       len(lines_wide), max(tui.width(ln) for ln in lines_wide)))

st_scroll = tui.SelectState(items, ["core"])
st_scroll.key("end")  # move to bottom (Blocked)
lines_scroll = tui.render_select(st_scroll, 60, 12, style_tc, "en", "Pick", tui.ICONS_UNI)
plain_scroll = [tui.strip_ansi(ln) for ln in lines_scroll]
check("render_select: bottom cursor visible with scroll indicator",
      any("Blocked" in ln for ln in plain_scroll) and any("↑" in ln or "↓" in ln for ln in plain_scroll),
      plain_scroll)

# ── 6. Style(None) / Style(..., off=True) and UI plain / fail_over ──

s_none = tui.Style(None)
s_off = tui.Style("truecolor", off=True)
check("Style(None) and Style(..., off=True): no color/off ESC",
      "\x1b" not in s_none.paint("hello", tui.BLUE)
      and "\x1b" not in s_none.gradient("hello")
      and "\x1b" not in s_off.paint("hello", tui.BLUE, bold=True, dim=True)
      and "\x1b" not in s_off.gradient("hello", bold=True),
      (repr(s_none.paint("hello", tui.BLUE)), repr(s_off.paint("hello", tui.BLUE, bold=True))))

buf_plain = io.StringIO()
ui_plain = tui.UI(tui.Caps("plain"), out=buf_plain, lang="en")
ui_plain.banner("0.30.1", "tagline", "subtitle")
ui_plain.rows([("ok", "Git", "ready"), ("warn", "LSP", "missing")])
for kind in ("ok", "warn", "fail"):
    with ui_plain.step(f"step {kind}") as sp:
        getattr(sp, kind)(f"detail {kind}")
ui_plain.box("Done", [("Where", "/tmp/x"), "Next line"], "ok")
plain_out = buf_plain.getvalue()
check("UI plain mode: banner/rows/step(ok/warn/fail)/box produce no ESC",
      "\x1b" not in plain_out and "Antigravity Plus 0.30.1" in plain_out
      and "[ok] step ok" in plain_out and "[!!] step warn" in plain_out and "[xx] step fail" in plain_out
      and "Done" in plain_out,
      plain_out[-400:])

buf_fail = io.StringIO()
ui_fail = tui.UI(tui.Caps("rich", "16", False, True), out=buf_fail, lang="en")
ui_fail._rows_rich = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
ui_fail.rows([("ok", "FallbackRow", "works")])
fail_out = buf_fail.getvalue()
check("UI fail_over: exception in _rows_rich switches to plain and prints row",
      not ui_fail.rich and "FallbackRow" in fail_out and "plain mode" in fail_out,
      (ui_fail.rich, fail_out))

# ── 7. cp1251 encoding safety ──

cp_ok = True
cp_err = ""
for caps in (tui.Caps("plain", None, False, False), tui.Caps("rich", "16", False, True)):
    raw_cp = io.BytesIO()
    wrap_cp = io.TextIOWrapper(raw_cp, encoding="cp1251", errors="strict", write_through=True)
    ui_cp = tui.UI(caps, out=wrap_cp, lang="ru")
    try:
        ui_cp.banner("0.30.1", "Установка 🚀", "Подзаголовок ⚠️")
        ui_cp.rows([("ok", "Модуль", "✔ готово → путь")])
        with ui_cp.step("Проверка ⠋") as sp:
            sp.ok("готово")
        ui_cp.box("Итог", [("Куда", "~/dir"), "Строка с эмодзи 🚀"], "ok")
    except UnicodeEncodeError as e:
        cp_ok = False
        cp_err = repr(e)
check("UI over cp1251 TextIOWrapper (plain and rich): no UnicodeEncodeError", cp_ok, cp_err)

# ── 8. install.py --demo --plain and captured --demo ──

demo_env = dict(os.environ, AGP_DEMO_FAST="1")
r_dp = subprocess.run([sys.executable, os.path.join(ROOT, "install.py"), "--demo", "--plain", "--lang", "en"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace", env=demo_env, timeout=60)
r_dc = subprocess.run([sys.executable, os.path.join(ROOT, "install.py"), "--demo", "--lang", "en"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace", env=demo_env, timeout=60)
check("install.py --demo --plain and captured --demo: exit 0, no ESC, header & summary",
      r_dp.returncode == 0 and "\x1b" not in r_dp.stdout + r_dp.stderr
      and "Antigravity Plus" in r_dp.stdout and "installed" in r_dp.stdout
      and r_dc.returncode == 0 and "\x1b" not in r_dc.stdout + r_dc.stderr
      and "Antigravity Plus" in r_dc.stdout and "installed" in r_dc.stdout,
      (r_dp.returncode, r_dp.stdout[-300:], r_dc.returncode, r_dc.stdout[-300:]))

# ── 9. POSIX PTY tests ──

if os.name == "nt":
    check("POSIX PTY tests: skipped on Windows", True)
else:
    import fcntl
    import pty
    import select
    import struct
    import termios

    def run_pty(args, key_seq, cols=100, rows=30, wait_for="Which modules to install", extra_env=None):
        master, slave = pty.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CI")}
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["AGP_DEMO_FAST"] = "1"
        if extra_env:
            env.update(extra_env)
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "install.py"), *args],
            stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True,
        )
        chunks: list = []
        sent = False
        deadline = time.time() + 45
        try:
            while time.time() < deadline:
                rlist, _, _ = select.select([master], [], [], 0.05)
                if rlist:
                    try:
                        data = os.read(master, 65536)
                        if data:
                            chunks.append(data)
                    except OSError:
                        break
                if not sent and wait_for.encode("utf-8") in b"".join(chunks):
                    for k in key_seq:
                        os.write(master, k)
                        time.sleep(0.08)
                    sent = True
                if proc.poll() is not None:
                    while True:
                        r2, _, _ = select.select([master], [], [], 0.05)
                        if not r2:
                            break
                        try:
                            data = os.read(master, 65536)
                            if not data:
                                break
                            chunks.append(data)
                        except OSError:
                            break
                    break
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            attrs = termios.tcgetattr(slave)
        finally:
            os.close(master)
            os.close(slave)
        raw = b"".join(chunks)
        text = raw.decode("utf-8", errors="replace")
        scr = vt_screen.Screen(cols, rows)
        scr.feed(text)
        return proc.returncode, raw, text, scr.lines(), attrs

    # 9a. --demo --lang en with down, space, enter
    rc_d, raw_d, txt_d, lines_d, attrs_d = run_pty(["--demo", "--lang", "en"], [b"\x1b[B", b" ", b"\r"])
    joined_d = "\n".join(lines_d)
    before_sim_fail = joined_d.split("Demo: a failing step")[0]
    check("PTY --demo (down, space, enter): exit 0, 12 of 13 chosen, box, no Traceback, cursor restored",
          rc_d == 0
          and "12 of 13 chosen" in joined_d
          and "Demo: a failing step" in joined_d
          and "╰" in joined_d
          and "Traceback" not in joined_d
          and not any("space toggle" in ln for ln in lines_d)  # the picker collapsed to one line
          and any(ln.startswith(" ◆ Installing") for ln in lines_d)
          and b"\x1b[?25h" in raw_d[-128:],
          (rc_d, b"\x1b[?25h" in raw_d[-128:], lines_d[-15:]))

    # 9b. --demo --lang en with q -> Cancelled
    rc_q, _raw_q, _txt_q, lines_q, _attrs_q = run_pty(["--demo", "--lang", "en"], [b"q"])
    joined_q = "\n".join(lines_q)
    check("PTY --demo (q): Cancelled — nothing changed, exit 0",
          rc_q == 0 and "Cancelled — nothing changed" in joined_q,
          (rc_q, lines_q[-10:]))

    # 9b2. SIGTERM in the middle of the steps: exit 128+15, cursor shown, terminal mode back
    import signal as _signal
    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CI", "AGP_DEMO_FAST")}
    env.update(TERM="xterm-256color")
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "install.py"), "--demo", "--lang", "en"],
                            stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    buf, answered = b"", False
    deadline = time.time() + 30
    while time.time() < deadline and proc.poll() is None:
        if select.select([master], [], [], 0.05)[0]:
            buf += os.read(master, 65536)
        if b"Which modules to install" in buf and not answered:
            os.write(master, b"\r")
            answered = True
        if b"Installing" in buf:
            time.sleep(0.5)
            proc.send_signal(_signal.SIGTERM)
            break
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    while select.select([master], [], [], 0.1)[0]:
        try:
            more = os.read(master, 65536)
        except OSError:
            break
        if not more:
            break
        buf += more
    attrs_t = termios.tcgetattr(slave)
    os.close(master); os.close(slave)
    check("PTY SIGTERM during the steps: exit 143, cursor shown, echo and line mode back",
          proc.returncode in (143, -15) and buf.rstrip().endswith(b"\x1b[?25h") and attrs_t[3] & termios.ECHO
          and attrs_t[3] & termios.ICANON, (proc.returncode, buf[-80:]))

    # 9c. --choose --lang en real install in temp HOME + termios restored
    home = os.path.expanduser("~")
    dest = os.path.join(home, ".gemini", "config", "plugins", "antigravity-plus")
    choice_path = os.path.join(home, ".gemini", "antigravity-plus", "modules.json")
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    if os.path.exists(choice_path):
        os.remove(choice_path)
    rc_c, _raw_c, _txt_c, lines_c, attrs_c = run_pty(["--choose", "--lang", "en"], [b"\x1b[B", b" ", b"\r"])
    saved_choice = json.load(open(choice_path, encoding="utf-8")) if os.path.isfile(choice_path) else {}
    echo_on = bool(attrs_c[3] & termios.ECHO)
    icanon_on = bool(attrs_c[3] & termios.ICANON)
    check("PTY --choose (down, space, enter): installs without quality, exit 0, ECHO & ICANON restored",
          rc_c == 0
          and "quality" not in (saved_choice.get("installed") or [])
          and "core" in (saved_choice.get("installed") or [])
          and echo_on and icanon_on,
          (rc_c, saved_choice.get("installed"), echo_on, icanon_on, lines_c[-10:]))

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
