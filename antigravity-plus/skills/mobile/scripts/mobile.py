#!/usr/bin/env python3
"""Antigravity Plus — /mobile: run an app in the iOS simulator or the Android emulator, look at it and use it.

  mobile.py devices                       what can run here: simulators, emulators, the tools found
  mobile.py project [DIR]                 the mobile project in DIR: how to build it, where the app lands
  mobile.py boot [NAME] [--show]          start a simulator or an emulator (no window unless --show)
  mobile.py run [APP_OR_APK]              install, launch, screenshot, errors: the one command after a build
  mobile.py shot [--dark] [--big-text]    a screenshot (also in the other appearance / with large text)
  mobile.py ui [--find TEXT]              what is on the screen: buttons, texts, fields, with coordinates
  mobile.py tap X Y | --text T | --id I   tap (--long to hold)
  mobile.py swipe up|down|left|right      or: swipe X1 Y1 X2 Y2
  mobile.py type TEXT [--into LABEL]      put text into a field
  mobile.py key back|home|enter|…         a key or a system gesture
  mobile.py open URL                      a link or a deep link
  mobile.py wait TEXT [--gone]            until a text appears (or goes away)
  mobile.py logs [APP]                    the app's errors and its crash, if there was one
  mobile.py launch|stop|install|uninstall|grant|set|shutdown …   the single steps

Coordinates are pixels of the screenshot `shot` makes; `ui` prints them the same way. With several devices running,
add --ios, --android or --device NAME. iOS needs a Mac with Xcode: `simctl` does the rest and a small helper
(simbridge.m, compiled on first use) does the touches, the text and the list of elements. Android needs the SDK's
adb and emulator, found by themselves — they don't have to be on PATH. Only simulators and emulators are used: a
phone on a cable is touched only when you name it with --device.
Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "hooks"))
import compat  # noqa: E402  (which OS this is, and how Python is started here)

HOME = os.path.expanduser("~/.gemini/antigravity-plus/mobile")
SHOTS = os.path.join(HOME, "shots")
STATE = os.path.join(HOME, "state.json")
BRIDGE_SRC = os.path.join(HERE, "simbridge.m")
LONG_SIDE = 1600          # iOS screenshots are scaled down to this: a 1206×2622 PNG is 2 MB and no clearer to a model
KEEP_SHOTS = 80
SPRINGBOARD = "com.apple.springboard"
SKIP_DIRS = {"node_modules", "Pods", "build", ".build", "DerivedData", "Carthage", "vendor", "dist", ".gradle", ".git",
             ".idea", "venv", ".venv", "__pycache__", "xcuserdata"}
SOURCES = (".swift", ".m", ".mm", ".h", ".kt", ".kts", ".java", ".xml", ".dart", ".tsx", ".ts", ".jsx", ".js",
           ".storyboard", ".xib", ".strings", ".xcstrings", ".json", ".gradle")
ANDROID_KEYS = {"back": 4, "home": 3, "enter": 66, "backspace": 67, "delete": 67, "tab": 61, "escape": 111, "menu": 82,
                "recents": 187, "up": 19, "down": 20, "left": 21, "right": 22, "center": 23, "ok": 23, "search": 84,
                "play": 85, "space": 62, "power": 26}
SYSTEM_LANG = ("system", "default", "reset")  # --lang values that mean «whatever the device is set to»
IOS_FIELDS = {"TextField", "TextArea", "SearchField", "SecureTextField", "ComboBox"}
IOS_KEYS = {"shift", "delete", "more", "space", "Return", "dictation"}  # identifiers of the on-screen keyboard's keys
TAPPABLE = {"Button", "Link", "Cell", "Switch", "CheckBox", "Tab", "TabButton", "MenuItem", "PopUpButton", "Slider",
            "RadioButton", "Toggle"} | IOS_FIELDS
IOS_SIZES = {"normal": "large", "big": "accessibility-large"}
ANDROID_SIZES = {"normal": "1.0", "big": "1.3"}
# The system log of an app's process is mostly the system talking to itself (haptics, the keyboard, analytics…).
# Kept: what the app logs itself, and the system's words about the app's own mistakes.
IOS_NOISE = re.compile(r"^objc\[\d+\]: Class \S+ is implemented in both|^\s*(One of the (two|duplicates)|This may cause)")
IOS_SYSTEM = re.compile(r"\] \[com\.apple\.[\w.-]+:|\] \((?!Foundation\)|libswiftCore)[\w.+-]+\) ")
IOS_USEFUL = re.compile(r"\[com\.apple\.(SwiftUI|runtime-issues|UIKit:(LayoutConstraints|Layout)\b|CFNetwork|network:|coredata|"
                        r"CoreData|SwiftData|StoreKit|WebKit:(Loading|Process)|AVFoundation)|\((CFNetwork|CoreData|SwiftData|"
                        r"SwiftUI|SwiftUICore)\) |constraint|Thread \d+: |Main Thread Checker", re.I)


class Fail(Exception):
    """Something the agent can act on: the message says what happened and what to do next."""


# ------------------------------------------------------------------------------------------------------- plumbing

def run(cmd: List[str], timeout: float = 30.0, data: Optional[bytes] = None, binary: bool = False,
        env: Optional[dict] = None):
    """(exit code, stdout, stderr); never raises. 127 = no such program, 124 = no answer in time.
    The command gets its own process group, and on a timeout the whole group is killed: `xcrun simctl …` and
    `adb …` start helpers of their own, and a helper that hangs would otherwise keep the pipes — and us — waiting."""
    empty = b"" if binary else ""
    group = {"creationflags": 0x00000200} if compat.WINDOWS else {"start_new_session": True}
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **group)
    except FileNotFoundError:
        return 127, empty, f"{cmd[0]}: not found"
    except OSError as e:
        return 126, empty, str(e)
    try:
        out, err = proc.communicate(data, timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired as late:  # something it started still holds the pipes: keep what was read
            out = late.stdout or b""
        code, err = 124, f"no answer in {timeout:.0f} s".encode()
    return code, out if binary else out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def kill_tree(proc) -> None:
    try:
        if compat.WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def read(path: str, limit: int = 2_000_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    try:
        os.makedirs(HOME, exist_ok=True)
        tmp = f"{STATE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        compat.replace(tmp, STATE)
    except OSError:
        pass


def remember(key: str, device: str, value) -> None:
    st = load_state()
    st.setdefault(key, {})[device] = value
    save_state(st)


def png_size(path: str) -> Tuple[int, int]:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return 0, 0
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    return struct.unpack(">II", head[16:24])


def squeeze(lines: List[str]) -> List[str]:
    """The same message many times over becomes one line with a count (timestamps and addresses don't count)."""
    out: List[str] = []
    counts: Dict[str, int] = {}
    for ln in lines:
        key = re.sub(r"^\d{4}-\d\d-\d\d [\d:.]+|^\d\d-\d\d [\d:.]+|\[\d+:\w+\]|\(\s*\d+\)|0x[0-9a-f]+", "", ln)
        if key in counts:
            counts[key] += 1
        else:
            counts[key] = 1
            out.append(ln)
    for i, ln in enumerate(out):
        key = re.sub(r"^\d{4}-\d\d-\d\d [\d:.]+|^\d\d-\d\d [\d:.]+|\[\d+:\w+\]|\(\s*\d+\)|0x[0-9a-f]+", "", ln)
        if counts[key] > 1:
            out[i] = f"{ln[:280]}  (×{counts[key]})"
    return out


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def me() -> str:
    """How to call this script, for the commands printed to the agent."""
    return f'{compat.python_cmd()} "{os.path.abspath(__file__)}"'


def free_space_note() -> str:
    try:
        free = shutil.disk_usage(os.path.expanduser("~")).free
    except OSError:
        return ""
    if free > 6 * 1024 ** 3:
        return ""
    return (f"Only {free / 1024 ** 3:.1f} GB is free on the disk: emulators, simulators and builds need several. Tell the "
            "user; what to delete is their call (Xcode's DerivedData, old simulators and AVDs, Gradle caches are the "
            "usual places).")


# ------------------------------------------------------------------------------------------------------ iOS tools

def xcrun() -> Optional[str]:
    """`xcrun`, when this Mac has Xcode's simulators (the /usr/bin/xcrun stub alone is on every Mac)."""
    if not compat.MAC or not os.path.isdir("/Library/Developer/PrivateFrameworks/CoreSimulator.framework"):
        return None
    return shutil.which("xcrun")


def developer_dir() -> str:
    code, out, _ = run(["xcode-select", "-p"], 10)
    return out.strip() if code == 0 else ""


def simctl(*args: str, timeout: float = 30.0, **kw):
    x = xcrun()
    if not x:
        raise Fail("iOS simulators need a Mac with Xcode (xcrun isn't here).")
    return run([x, "simctl", *args], timeout, **kw)


def ios_devices() -> List[dict]:
    """iOS simulators: running ones first, then the most recently used."""
    if not xcrun():
        return []
    code, out, _ = simctl("list", "devices", "-j", timeout=20)
    try:
        data = json.loads(out) if code == 0 else {}
    except ValueError:
        data = {}
    devs = []
    for runtime, items in (data.get("devices") or {}).items():
        m = re.search(r"SimRuntime\.iOS-([\d-]+)$", runtime)
        if not m:
            continue  # tvOS, watchOS, visionOS: no touch screen this tool knows how to use
        for d in items or []:
            if d.get("isAvailable") is False or not d.get("udid"):
                continue
            devs.append({"platform": "ios", "id": d["udid"], "name": d.get("name", ""), "kind": "simulator",
                         "os": "iOS " + m.group(1).replace("-", "."), "running": d.get("state") == "Booted",
                         "last": d.get("lastUsedAt") or d.get("lastBootedAt") or ""})
    devs.sort(key=lambda d: d["last"], reverse=True)
    devs.sort(key=lambda d: not d["running"])
    return devs


def bridge_bin() -> str:
    """The compiled simbridge helper; built on first use and again whenever its source changes."""
    try:
        with open(BRIDGE_SRC, "rb") as f:
            source = f.read()
    except OSError:
        raise Fail(f"{BRIDGE_SRC} is missing: reinstall the plugin.")
    folder = os.path.join(HOME, "bin")
    path = os.path.join(folder, "simbridge-" + hashlib.sha1(source).hexdigest()[:12])
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    os.makedirs(folder, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    code, _, err = run([xcrun() or "xcrun", "clang", "-fobjc-arc", "-O1", "-framework", "Foundation", "-framework",
                        "CoreGraphics", "-o", tmp, BRIDGE_SRC], 120)
    if code != 0 or not os.path.isfile(tmp):
        raise Unsupported("the iOS helper didn't compile: " + (err.strip().splitlines() or ["no compiler output"])[-1][:300])
    os.replace(tmp, path)
    for old in os.listdir(folder):
        if old.startswith("simbridge-") and os.path.join(folder, old) != path:
            try:
                os.remove(os.path.join(folder, old))
            except OSError:
                pass
    return path


class Unsupported(Fail):
    """The built-in iOS helper can't work with this Xcode."""


def unsupported_hint(why: str) -> str:
    return (f"Touches, text and the element list aren't available on this simulator: {why}.\nScreenshots, launching, "
            "links and logs still work. Reach a screen through the app's own deep links (`open`) or launch arguments, "
            "or run its UI tests; a separate tool such as AXe or idb can send touches on other Xcode versions — ask the "
            "user before installing one.")


# -------------------------------------------------------------------------------------------------- Android tools

def android_sdk(start: str = "") -> Optional[str]:
    """The Android SDK: from the environment, a project's local.properties, the usual places, or adb on PATH."""
    cands: List[Optional[str]] = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]
    folder = os.path.abspath(start or os.getcwd())
    for _ in range(6):
        for sub in ("", "android"):
            m = re.search(r"^\s*sdk\.dir\s*=\s*(.+?)\s*$", read(os.path.join(folder, sub, "local.properties"), 20000), re.M)
            if m:
                cands.append(m.group(1).replace("\\:", ":").replace("\\\\", "\\"))
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    if compat.MAC:
        cands.append("~/Library/Android/sdk")
    elif compat.WINDOWS:
        cands.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"))
    else:
        cands += ["~/Android/Sdk", "~/android-sdk"]
    on_path = shutil.which("adb")
    if on_path:
        cands.append(os.path.dirname(os.path.dirname(os.path.realpath(on_path))))
    for c in cands:
        c = os.path.expanduser(c) if c else ""
        if c and os.path.isdir(os.path.join(c, "platform-tools")):
            return c
    return None


def sdk_tool(folder: str, name: str) -> Optional[str]:
    exe = name + (".exe" if compat.WINDOWS else "")
    sdk = android_sdk()
    if sdk and os.path.isfile(os.path.join(sdk, folder, exe)):
        return os.path.join(sdk, folder, exe)
    return shutil.which(name)


def adb() -> Optional[str]:
    return sdk_tool("platform-tools", "adb")


_adb_started = False


def need_adb() -> str:
    """adb, with its server running. The server is started here with nothing attached to it: started by an ordinary
    command, on Windows it inherits that command's output pipes and the command then never seems to end."""
    global _adb_started
    a = adb()
    if not a:
        raise Fail("adb isn't here: the Android SDK wasn't found (ANDROID_HOME, local.properties, the usual folders). "
                   "Android Studio installs it; then run this again.")
    if not _adb_started:
        _adb_started = True
        try:
            subprocess.run([a, "start-server"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=40)
        except (OSError, subprocess.SubprocessError):
            pass
    return a


def avd_home() -> str:
    if os.environ.get("ANDROID_AVD_HOME"):
        return os.environ["ANDROID_AVD_HOME"]
    base = os.environ.get("ANDROID_USER_HOME") or os.environ.get("ANDROID_EMULATOR_HOME") or os.path.expanduser("~/.android")
    return os.path.join(base, "avd")


def ini(path: str) -> Dict[str, str]:
    out = {}
    for line in read(path, 200000).splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            out[key.strip()] = value.strip()
    return out


def android_avds() -> List[dict]:
    """The emulators (AVDs) created on this machine, read from their files: nothing is started."""
    home, out = avd_home(), []
    try:
        names = sorted(fn[:-4] for fn in os.listdir(home) if fn.endswith(".ini"))
    except OSError:
        return []
    for name in names:
        cfg = ini(os.path.join(ini(os.path.join(home, name + ".ini")).get("path") or os.path.join(home, name + ".avd"),
                               "config.ini"))
        api = re.search(r"android-(\w+)", cfg.get("image.sysdir.1", ""))
        tag = cfg.get("tag.id", "")
        try:
            w, h, dpi = int(cfg.get("hw.lcd.width", 0)), int(cfg.get("hw.lcd.height", 0)), int(cfg.get("hw.lcd.density", 0))
        except ValueError:
            w = h = dpi = 0
        model = cfg.get("hw.device.name", "")
        form = ("TV" if "tv" in tag or re.match(r"(tv|television)[_ -]|.*[_ -]tv$", model, re.I) else "watch" if "wear" in tag
                else "car" if "automotive" in tag else "tablet" if dpi and min(w, h) * 160 / dpi >= 600 else "phone")
        out.append({"name": name, "api": api.group(1) if api else "", "form": form, "size": f"{w}×{h}" if w else "",
                    "model": model.replace("_", " ")})
    return out


def android_running() -> List[dict]:
    """What adb sees: running emulators (with their AVD names) and devices on a cable."""
    if not adb():
        return []
    a = need_adb()
    code, out, _ = run([a, "devices", "-l"], 25)
    devs = []
    for line in out.splitlines()[1:] if code == 0 else []:
        parts = line.split()
        if len(parts) < 2 or parts[0].startswith("*"):
            continue
        serial, state = parts[0], parts[1]
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        emu = serial.startswith("emulator-")
        name = ""
        if emu and state == "device":
            c, o, _ = run([a, "-s", serial, "emu", "avd", "name"], 6)
            name = o.splitlines()[0].strip() if c == 0 and o.strip() else ""
        devs.append({"platform": "android", "id": serial, "kind": "emulator" if emu else "device", "os": "",
                     "name": name or info.get("model", serial).replace("_", " "), "running": state == "device",
                     "state": state})
    return devs


# ------------------------------------------------------------------------------------------------------ elements

def element_line(el: dict, n: int) -> str:
    parts = [f"{n:>3}  {el['role'] or '?'}"]
    if el["label"]:
        parts.append("“" + el["label"][:80].replace("\n", " ") + "”")
    value = el.get("value") or ""
    if value and value != el["label"]:
        parts.append("= " + json.dumps(value[:60], ensure_ascii=False))
    if el.get("hint") and el["hint"] not in (el["label"], value):
        parts.append("hint " + json.dumps(el["hint"][:40], ensure_ascii=False))
    if el.get("id"):
        parts.append("id=" + el["id"])
    parts.append(f"({el['x']},{el['y']})")
    if el.get("flags"):
        parts.append("[" + ", ".join(el["flags"]) + "]")
    return "  ".join(parts)


def find_elements(elements: List[dict], text: str = "", ident: str = "", every: bool = False) -> List[dict]:
    """Elements named by a text (label, value or hint) or an id: the exact matches if there are any, otherwise the
    ones that contain it (`every`: both, exact ones first). On-screen and tappable ones come first."""
    def fields(el):
        return [str(el.get(k) or "").casefold() for k in ("label", "value", "hint")]

    def order(found):
        return sorted(found, key=lambda e: ("off-screen" in e.get("flags", []), e["role"] not in TAPPABLE and "tap" not in e.get("flags", [])))

    if ident:
        want = ident.casefold()
        exact = [e for e in elements if (e.get("id") or "").casefold() == want]
        loose = [e for e in elements if want in (e.get("id") or "").casefold() and e not in exact]
    else:
        want = " ".join(text.casefold().split())
        if not want:
            return []
        exact = [e for e in elements if want in fields(e)]
        loose = [e for e in elements if any(want in f for f in fields(e)) and e not in exact]
    return order(exact) + order(loose) if every else order(exact or loose)


def texts(elements: List[dict], limit: int = 14) -> str:
    seen, out = set(), []
    for e in elements:
        label = (e["label"] or e.get("value") or "").strip().replace("\n", " ")
        if label and label not in seen and "off-screen" not in e.get("flags", []):
            seen.add(label)
            out.append(label[:40])
    return " · ".join(out[:limit]) + (f" · … {len(out) - limit} more" if len(out) > limit else "")


# ------------------------------------------------------------------------------------------------------------ iOS

class Sim:
    platform = "ios"

    def __init__(self, d: dict):
        self.id, self.name, self.os = d["id"], d["name"], d.get("os", "")

    def title(self) -> str:
        return f"{self.name} ({self.os}, simulator)"

    def bridge(self, *args: str, timeout: float = 40.0) -> str:
        """Run the helper. Raises Unsupported when it can't work with this Xcode, Fail for everything else."""
        code, out, err = run([bridge_bin(), developer_dir(), self.id, *args], timeout)
        if code == 0:
            return out
        why = err.strip().replace("simbridge: ", "") or f"the helper stopped with code {code}"
        if code == 3:
            raise Unsupported(why)
        raise Fail(why)

    def bridge_json(self, *args: str) -> dict:
        try:
            data = json.loads(self.bridge(*args) or "null")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise Fail("the simulator's answer couldn't be read (the app may be starting or switching screens): try again.")
        return data

    # ---- screen
    def shot(self, path: str) -> Tuple[int, int]:
        full = path + ".full.png"
        for old in (full, path):
            if os.path.exists(old):
                os.remove(old)
        code, _, err = simctl("io", self.id, "screenshot", "--type=png", full, timeout=40)
        w, h = png_size(full)
        if code != 0 or not w:
            raise Fail("no screenshot from the simulator: " + (err.strip().splitlines() or ["no reason given"])[-1][:300])
        if max(w, h) > LONG_SIDE and shutil.which("sips"):
            run(["sips", "-Z", str(LONG_SIDE), full, "--out", path], 40)
        if png_size(path)[0]:  # scaled down
            os.remove(full)
        else:
            os.replace(full, path)
        size = png_size(path)
        remember("screens", self.id, list(size))
        return size

    def size(self) -> Tuple[int, int]:
        """The size of the screenshots `shot` makes of this simulator (what coordinates are measured in)."""
        known = load_state().get("screens", {}).get(self.id)
        if isinstance(known, list) and len(known) == 2 and all(isinstance(v, int) and v > 0 for v in known):
            return known[0], known[1]
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="agp-size-")
        os.close(fd)
        try:
            return self.shot(tmp)
        finally:
            for p in (tmp, tmp + ".full.png"):
                if os.path.exists(p):
                    os.remove(p)

    def elements(self) -> Tuple[List[dict], dict]:
        tree = self.bridge_json("ui")
        frame = tree.get("frame") or [0, 0, 0, 0]
        pw, ph = float(frame[2]), float(frame[3])
        w, h = self.size()
        if pw <= 0 or ph <= 0:
            raise Fail("the simulator gave no screen size: is the app still starting? Try again in a second.")
        info = {"app": tree.get("label") or tree.get("title") or "", "pid": tree.get("pid"), "cut": bool(tree.get("cut")),
                "points": (pw, ph)}
        if abs(w / h - pw / ph) > 0.03:
            info["note"] = ("the app's layout is rotated relative to the screenshot (landscape?): the coordinates below "
                            "may not match the picture — tap by --text, or read positions from the screenshot.")
        sx, sy = w / pw, h / ph
        out: List[dict] = []

        def walk(node: dict) -> None:
            f = node.get("frame") or [0, 0, 0, 0]
            role = str(node.get("role") or "")
            role = role[2:] if role.startswith("AX") else role
            label = str(node.get("label") or node.get("title") or "")
            if f[2] > 0 and f[3] > 0 and role != "Application" and (label or node.get("value") or node.get("id") or role in TAPPABLE):
                cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
                flags = [k for k in ("disabled", "focused", "selected") if node.get(k)]
                if not (0 <= cx <= pw and 0 <= cy <= ph):
                    flags.append("off-screen")
                out.append({"role": role, "label": label, "value": str(node.get("value") or ""), "id": str(node.get("id") or ""),
                            "hint": str(node.get("placeholder") or ""), "x": round(cx * sx), "y": round(cy * sy),
                            "w": round(f[2] * sx), "h": round(f[3] * sy), "flags": flags, "pt": (cx, cy)})
            for child in node.get("children") or []:
                walk(child)

        walk(tree)
        keys = [e for e in out if e["role"] == "Button" and (len(e["label"]) == 1 or e["id"] in IOS_KEYS)]
        if sum(len(e["label"]) == 1 for e in keys) >= 15:  # the on-screen keyboard is up
            info["keyboard"] = min(e["y"] - e["h"] // 2 for e in keys)
            for e in out:  # the keys, and the row of buttons under them
                if e["y"] >= info["keyboard"] and e["role"] == "Button":
                    e["flags"].append("key")
        return out, info

    # ---- input
    def tap(self, x: float, y: float, hold_ms: int = 0) -> None:
        w, h = self.size()
        self.bridge("tap", f"{x / w:.5f}", f"{y / h:.5f}", str(hold_ms))

    def swipe(self, x1: float, y1: float, x2: float, y2: float, ms: int = 300) -> None:
        w, h = self.size()
        # A touch that starts on the very edge is a system gesture (left edge = back), as on a real iPhone.
        edge = 1 if x1 <= w * 0.012 else 2 if x1 >= w * 0.988 else 3 if y1 <= h * 0.006 else 4 if y1 >= h * 0.994 else 0
        self.bridge("swipe", f"{x1 / w:.5f}", f"{y1 / h:.5f}", f"{x2 / w:.5f}", f"{y2 / h:.5f}", str(ms), str(edge))

    def key(self, name: str) -> str:
        if name == "home":
            self.launch(SPRINGBOARD, keep=True)
            return "home screen"
        if name == "back":
            try:
                button = next((e for e in self.elements()[0] if e["id"] == "BackButton" and "off-screen" not in e["flags"]), None)
            except Unsupported:
                raise
            except Fail:
                button = None
            if button:
                self.tap(button["x"], button["y"])
                return f"back (the “{button['label'] or 'Back'}” button of the navigation bar)"
            # No standard back button: the gesture. A quick flick is ignored; a finger takes about half a second.
            self.bridge("swipe", "0.003", "0.5", "0.8", "0.5", "600", "1")
            return "back (a swipe from the left edge)"
        on_screen = {"enter": "Return", "return": "Return", "backspace": "delete", "delete": "delete", "space": "space"}.get(name)
        if on_screen:
            key = next((e for e in self.elements()[0] if e["id"] == on_screen and "key" in e["flags"]), None)
            if key:
                self.tap(key["x"], key["y"])
                return f"{name} (the “{key['label'] or on_screen}” key of the on-screen keyboard)"
            raise Fail(f"There's no on-screen keyboard to press “{name}” on: tap a text field first. To submit a form, "
                       "tapping its button is surer.")
        raise Fail(f"The iOS simulator takes no “{name}” key from here. Use what a finger would: tap the on-screen button, "
                   "`key enter` for the keyboard's return key, `key back` for the back gesture, `key home` for the home "
                   "screen; `type` replaces a field's text.")

    def touch_ok(self) -> Optional[bool]:
        """Do touches reach the simulator? Pulls the home screen down — that opens the system search — and looks for
        its field; then closes it. None when it can't tell (the screen can't be read yet)."""
        for attempt in range(4):
            try:
                self.launch(SPRINGBOARD, keep=True)
                time.sleep(1.0)
                w, h = self.size()
                self.swipe(w * 0.5, h * 0.35, w * 0.5, h * 0.62, 250)
                time.sleep(1.3)
                opened = any(e["id"] == "SpotlightSearchField" for e in self.elements()[0])
                if opened:
                    self.swipe(w * 0.5, h * 0.3, w * 0.5, h * 0.1, 250)
                    time.sleep(0.8)
                    return True
                if attempt:  # twice in a row nothing happened: that's an answer
                    return False
            except Unsupported:
                raise
            except Fail:
                pass
            time.sleep(2.0)
        return None

    def revive(self) -> Optional[bool]:
        """Restart the simulator's input service. Now and then a simulator comes up with touches going nowhere: its
        input service (backboardd) registered badly during the boot, and every touch from outside is lost until it
        registers again. Restarting that one service is enough and takes seconds; the home screen and the apps
        restart with it. Says whether touches arrive afterwards."""
        for service in ("user/foreground/com.apple.backboardd", "system/com.apple.backboardd"):
            if simctl("spawn", self.id, "launchctl", "kickstart", "-k", service, timeout=40)[0] == 0:
                break
        deadline = time.time() + 40
        while time.time() < deadline:  # until the home screen is back
            time.sleep(2.0)
            try:
                if len(self.elements()[0]) >= 3:
                    break
            except Unsupported:
                raise
            except Fail:
                pass
        return self.touch_ok()

    def working_touches(self) -> Optional[bool]:
        """Touches that arrive, restarting the input service if they don't. None when it can't be told."""
        ok = self.touch_ok()
        if ok is False:
            print("The simulator takes no touches (its input service came up badly): restarting that service.", flush=True)
            ok = self.revive()
        return ok

    def set_text(self, el: dict, text: str) -> str:
        cx, cy = el["pt"]
        return str(self.bridge_json("settext", f"{cx:.1f}", f"{cy:.1f}", text).get("value") or "")

    def is_field(self, el: dict) -> bool:
        return el["role"] in IOS_FIELDS

    # ---- apps
    def install(self, path: str) -> None:
        code, _, err = simctl("install", self.id, path, timeout=240)
        if code != 0:
            raise Fail("install failed: " + (err.strip().splitlines() or ["no reason given"])[-1][:400])

    def uninstall(self, app: str) -> None:
        simctl("uninstall", self.id, app, timeout=60)

    def out_log(self, app: str) -> str:
        return os.path.join(HOME, "logs", f"{self.id[:8]}-{re.sub(r'[^A-Za-z0-9.-]', '_', app)}.log")

    def launch(self, app: str, argv: Optional[List[str]] = None, lang: str = "", keep: bool = False) -> str:
        args = ["launch"]
        if app != SPRINGBOARD:
            log = self.out_log(app)
            os.makedirs(os.path.dirname(log), exist_ok=True)
            open(log, "w").close()  # print() and NSLog of this launch only
            args += [f"--stdout={log}", f"--stderr={log}"]
            if not keep:
                args.append("--terminate-running-process")
        extra = list(argv or [])
        if lang and lang.lower() not in SYSTEM_LANG:  # for this launch only
            extra += ["-AppleLanguages", f"({lang})", "-AppleLocale", lang.replace("-", "_")]
        # print() into a file is buffered until the app quits unless the app is told otherwise.
        env = dict(os.environ, SIMCTL_CHILD_NSUnbufferedIO="YES")
        code, out, err = simctl(*args, self.id, app, *extra, timeout=60, env=env)
        if code != 0:
            reason = (err.strip().splitlines() or ["no reason given"])[-1][:300]
            hint = " Is it installed? Check the id with `simctl listapps`, or install the build first." if "not" in reason.lower() else ""
            raise Fail(f"launch of {app} failed: {reason}{hint}")
        m = re.search(r":\s*(\d+)\s*$", out.strip())
        return f"pid {m.group(1)}" if m else "started"

    def stop(self, app: str) -> None:
        simctl("terminate", self.id, app, timeout=30)

    def process(self, app: str) -> Tuple[bool, Optional[int]]:
        """(could the simulator be asked, the app's process id — None when it isn't running)."""
        code, out, _ = simctl("spawn", self.id, "launchctl", "list", timeout=20)
        if code != 0:
            return False, None
        for line in out.splitlines():
            if f"UIKitApplication:{app}[" in line:
                first = line.split()[0]
                return True, int(first) if first.isdigit() else None
        return True, None

    def alive(self, app: str) -> Optional[bool]:
        asked, pid = self.process(app)
        return (pid is not None) if asked else None

    def open_url(self, url: str) -> None:
        code, _, err = simctl("openurl", self.id, url, timeout=30)
        if code != 0:
            raise Fail("the simulator didn't open the link: " + (err.strip().splitlines() or ["no reason given"])[-1][:300])

    def grant(self, app: str, what: str) -> None:
        code, _, err = simctl("privacy", self.id, "grant", what, app, timeout=30)
        if code != 0:
            raise Fail(f"grant failed: {(err.strip().splitlines() or [''])[-1][:300]}\nThe simulator knows: all, photos, "
                       "photos-add, location, location-always, contacts, calendar, microphone, reminders, motion, "
                       "media-library, siri. (The camera has no dialog to skip on a simulator.)")

    def executable(self, app: str) -> str:
        code, out, _ = simctl("get_app_container", self.id, app, "app", timeout=20)
        try:
            with open(os.path.join(out.strip(), "Info.plist"), "rb") as f:
                return str(plistlib.load(f).get("CFBundleExecutable") or "")
        except (OSError, ValueError, plistlib.InvalidFileException):
            return ""

    def logs(self, app: str, seconds: int, everything: bool = False) -> List[str]:
        out: List[str] = []
        printed = [ln for ln in read(self.out_log(app), 400000).splitlines() if ln.strip() and not IOS_NOISE.search(ln)] if app else []
        if printed:
            out += ["— the app's own output (print, NSLog) since its last launch —"] + squeeze(printed)[-30:]
        exe = self.executable(app) if app else ""
        if app and not exe:
            return out + [f"{app} isn't installed on this simulator, so there's no system log to filter."]
        # While the app runs: the log of that very process, i.e. of this run. Once it's gone (a crash): everything
        # its name logged in the time asked for.
        pid = self.process(app)[1] if exe else None
        pred = f"processIdentifier == {pid}" if pid else f'process == "{exe}"' if exe else "messageType == fault"
        if exe and not everything:
            pred += " AND (messageType == error OR messageType == fault)"
        code, text, err = simctl("spawn", self.id, "log", "show", "--last", f"{seconds}s", "--style", "compact",
                                 "--predicate", pred, timeout=90)
        lines = [ln for ln in text.splitlines() if re.match(r"\d{4}-\d\d-\d\d ", ln)] if code == 0 else []
        if not everything:
            lines = [ln for ln in lines if not IOS_SYSTEM.search(ln) or IOS_USEFUL.search(ln)]
        if lines:
            out += [f"— system log, {'this run of the app' if pid else 'last ' + ago(seconds)}"
                    + ("" if everything else ", the app's errors") + " —"] + squeeze(lines)[-40:]
        elif code != 0:
            out.append("the system log didn't answer: " + (err.strip().splitlines() or [""])[-1][:200])
        crash = self.crash(exe, seconds) if exe else ""
        if crash:
            out += ["— crash report —", crash]
        if app and not pid and self.alive(app) is False:
            out.append(f"⚠️ {app} is not running now: it crashed or was closed.")
        return out

    @staticmethod
    def crash(exe: str, seconds: int) -> str:
        folder = os.path.expanduser("~/Library/Logs/DiagnosticReports")
        try:
            reports = [os.path.join(folder, fn) for fn in os.listdir(folder) if fn.startswith(exe + "-") and fn.endswith(".ips")]
        except OSError:
            return ""
        reports = [p for p in reports if time.time() - os.path.getmtime(p) <= seconds + 30]
        if not reports:
            return ""
        newest = max(reports, key=os.path.getmtime)
        text = read(newest, 400000)
        try:
            body = json.loads(text.split("\n", 1)[1])
        except (ValueError, IndexError):
            return f"{newest} (open it to see why the app crashed)"
        exc = body.get("exception") or {}
        why = " ".join(str(v) for v in (exc.get("type"), exc.get("signal"), exc.get("subtype")) if v)
        frames = []
        for th in body.get("threads") or []:
            if th.get("triggered"):
                frames = [str(fr.get("symbol")) for fr in th.get("frames") or [] if fr.get("symbol")][:6]
        return f"{why or 'crashed'}; top of the crashed thread: {' ← '.join(frames) or 'no symbols'}\nFull report: {newest}"

    # ---- look and feel
    def appearance(self) -> str:
        code, out, _ = simctl("ui", self.id, "appearance", timeout=20)
        return out.strip() if code == 0 and out.strip() in ("dark", "light") else ""

    def set_appearance(self, mode: str) -> None:
        simctl("ui", self.id, "appearance", "dark" if mode == "dark" else "light", timeout=20)

    def text_size(self) -> str:
        code, out, _ = simctl("ui", self.id, "content_size", timeout=20)
        return out.strip() if code == 0 else ""

    def set_text_size(self, size: str) -> None:
        simctl("ui", self.id, "content_size", IOS_SIZES.get(size, size), timeout=20)


# -------------------------------------------------------------------------------------------------------- Android

class Droid:
    platform = "android"

    def __init__(self, d: dict):
        self.id, self.name, self.kind = d["id"], d["name"], d.get("kind", "emulator")

    def title(self) -> str:
        return f"{self.name} ({self.id}, {'emulator' if self.kind == 'emulator' else 'a real device'})"

    def adb(self, *args: str, timeout: float = 30.0, binary: bool = False):
        return run([need_adb(), "-s", self.id, *args], timeout, binary=binary)

    def shell(self, command: str, timeout: float = 30.0) -> str:
        """Run a command line in the device's shell; its stdout (stderr when stdout is empty)."""
        code, out, err = self.adb("shell", command, timeout=timeout)
        if code in (124, 127):
            raise Fail(f"adb: {err}")
        return out if out.strip() else err

    # ---- screen
    def shot(self, path: str) -> Tuple[int, int]:
        code, data, err = self.adb("exec-out", "screencap", "-p", timeout=40, binary=True)
        if code != 0 or data[:8] != b"\x89PNG\r\n\x1a\n":
            raise Fail("no screenshot from the device: " + (err.strip().splitlines() or ["screencap printed no PNG"])[-1][:300])
        with open(path, "wb") as f:
            f.write(data)
        return png_size(path)

    def size(self) -> Tuple[int, int]:
        """The screen in pixels, the way it is turned now (what screenshots and `input tap` use)."""
        now = re.search(r"\bcur=(\d+)x(\d+)", self.shell("dumpsys window displays"))
        if now:
            return int(now.group(1)), int(now.group(2))
        found = re.findall(r"(\d+)x(\d+)", self.shell("wm size"))
        if not found:
            raise Fail("the device didn't report its screen size (`wm size`).")
        return int(found[-1][0]), int(found[-1][1])  # an override, when set, comes last

    def elements(self) -> Tuple[List[dict], dict]:
        xml = ""
        tmp = "/data/local/tmp/agp-ui.xml"
        for attempt in range(3):
            if attempt != 1:  # into a file the shell may write, then read it back
                raw = self.shell(f"uiautomator dump {tmp} >/dev/null 2>&1; cat {tmp}; rm -f {tmp}", timeout=40)
            else:  # the other way round: straight to the terminal, where a device has one for this
                _code, raw, _err = self.adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=40)
            start, end = raw.find("<?xml"), raw.rfind("</hierarchy>")
            if start >= 0 and end > start:
                xml = raw[start:end + len("</hierarchy>")]
                break
            time.sleep(0.8)  # «could not get idle state»: an animation is running
        if not xml:
            raise Fail("the screen gave no element list (an animation, a video or a game is running). Use the "
                       "screenshot and tap by coordinates, or try again.")
        return parse_uiautomator(xml)

    # ---- input
    def tap(self, x: float, y: float, hold_ms: int = 0) -> None:
        if hold_ms:
            self.shell(f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(hold_ms)}")
        else:
            self.shell(f"input tap {int(x)} {int(y)}")

    def swipe(self, x1: float, y1: float, x2: float, y2: float, ms: int = 300) -> None:
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}")

    def key(self, name: str) -> str:
        code = ANDROID_KEYS.get(name) or (int(name) if name.isdigit() else None)
        if code is None:
            raise Fail(f"unknown key “{name}”: use {', '.join(sorted(ANDROID_KEYS))} or an Android key code number.")
        self.shell(f"input keyevent {code}")
        return name

    def is_field(self, el: dict) -> bool:
        return "EditText" in el["role"] or "TextField" in el["role"] or "field" in el.get("flags", [])

    def set_text(self, el: Optional[dict], text: str) -> str:
        if any(ord(ch) > 126 or ord(ch) < 32 for ch in text):
            raise Fail("adb can type only Latin letters, digits and punctuation (`input text` has no other characters). "
                       "For other text: open the screen with it through a deep link (`open`), or put it in with the "
                       "app's own debug hook.")
        if el is not None:
            if "focused" not in el.get("flags", []):
                self.tap(el["x"], el["y"])
                time.sleep(0.5)
            if el.get("value"):  # replace what is there: go to the end, then backspace over it
                self.shell("input keyevent 123 && input keyevent " + " ".join(["67"] * min(len(el["value"]) + 2, 250)), timeout=60)
        parts = android_text(text)
        if parts:
            self.shell(" && ".join("input text " + shlex.quote(part) for part in parts), timeout=60)
        return text

    # ---- apps
    def install(self, path: str, grant: bool = False) -> None:
        code, out, err = self.adb("install", "-r", "-t", "-d", *(["-g"] if grant else []), path, timeout=300)
        if code != 0 or "Success" not in out:
            reason = next((ln for ln in (out + "\n" + err).splitlines() if "Failure" in ln or "error" in ln.lower()), "")
            hint = (" The installed copy was signed with another key: `uninstall` it first (its data goes with it — ask the "
                    "user if that matters).") if "UPDATE_INCOMPATIBLE" in reason else ""
            raise Fail(f"install failed: {reason[:400] or 'adb gave no reason'}{hint}")

    def uninstall(self, app: str) -> None:
        self.adb("uninstall", app, timeout=60)

    def component(self, app: str) -> str:
        """The activity a tap on the app's icon starts, as `package/activity`. An app can have several launcher
        activities (a debug build with LeakCanary has two): its own first one is taken. Asked to choose between them,
        Android names its chooser screen instead — so only an activity of this very package is accepted."""
        categories = ["LAUNCHER", "LEANBACK_LAUNCHER"]
        if "tv" in self.shell("getprop ro.build.characteristics").lower():
            categories.reverse()
        for category in categories:
            intent = f"-a android.intent.action.MAIN -c android.intent.category.{category} {shlex.quote(app)}"
            listed = [ln.strip() for ln in self.shell("cmd package query-activities --brief --components " + intent).splitlines()]
            one = (self.shell("cmd package resolve-activity --brief " + intent).strip().splitlines() or [""])[-1].strip()
            own = [c for c in listed + [one] if re.fullmatch(r"[\w.]+/[\w.$]+", c) and c.split("/")[0] == app]
            own.sort(key=lambda c: "leakcanary" in c.lower())  # a debugging tool's own icon comes last
            if own:
                return own[0]
        return ""

    def launch(self, app: str, argv: Optional[List[str]] = None, lang: str = "", keep: bool = False) -> str:
        note = ""
        if lang:  # Android 13+ keeps a language per app — until it is changed back
            system = lang.lower() in SYSTEM_LANG
            said = self.shell(f"cmd locale set-app-locales {shlex.quote(app)}" + ("" if system else f" --locales {shlex.quote(lang)}"))
            note = ("" if "Unknown package" in said else
                    "; this Android has no per-app language, --lang did nothing" if said.strip() else
                    "; language: the system's again" if system else
                    f"; language {lang} — it stays for this app until `launch {app} --lang system`")
        comp = self.component(app)
        if not comp:
            if not keep:
                self.stop(app)
            for category in ("LAUNCHER", "LEANBACK_LAUNCHER"):  # the old way: let monkey find the launcher activity
                if "injected: 1" in self.shell(f"monkey -p {shlex.quote(app)} -c android.intent.category.{category} 1", timeout=40):
                    return "started" + note
            listed = self.shell(f"pm list packages {shlex.quote(app)}")
            raise Fail(f"{app} has no launcher activity on this device" + ("" if app in listed else ": it isn't installed")
                       + ". Check the package name (`project` prints it) or install the build first.")
        extra = " ".join(shlex.quote(a) for a in argv or [])
        out = self.shell(f"am start {'' if keep else '-S '}-W -n {shlex.quote(comp)} {extra}".strip(), timeout=60)
        if "Error" in out or "Exception" in out:
            raise Fail("launch failed: " + next((ln for ln in out.splitlines() if "Error" in ln or "Exception" in ln), out)[:400])
        took = re.search(r"TotalTime:\s*(\d+)", out)
        return (f"{comp}, started in {took.group(1)} ms" if took else comp) + note

    def stop(self, app: str) -> None:
        self.shell(f"am force-stop {shlex.quote(app)}")

    def alive(self, app: str) -> Optional[bool]:
        return bool(self.shell(f"pidof {shlex.quote(app)}").strip().split())

    def open_url(self, url: str) -> None:
        out = self.shell(f"am start -W -a android.intent.action.VIEW -d {shlex.quote(url)}", timeout=40)
        if "Error" in out or "unable to resolve" in out.lower():
            raise Fail("nothing on the device opens this link: " + (out.strip().splitlines() or [""])[-1][:300])

    def grant(self, app: str, what: str) -> None:
        perm = what if "." in what else "android.permission." + what.upper()
        out = self.shell(f"pm grant {shlex.quote(app)} {shlex.quote(perm)}")
        if out.strip():
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            why = next((ln for ln in lines if not ln.startswith(("at ", "Exception occurred"))), lines[-1])
            raise Fail("grant failed: " + why[:300])

    def logs(self, app: str, seconds: int, everything: bool = False) -> List[str]:
        out: List[str] = []
        crash = self.crash(app, seconds)
        if crash:
            out += ["— crash —"] + crash
        pid = self.shell(f"pidof {shlex.quote(app)}").strip().split()[:1] if app else []
        if app and not pid:
            return out + [f"{app} isn't running" + (": see the crash above." if crash else ".")]
        # For the app: every line the log still holds for its process, i.e. for this run of it. No `-t N` there: logd
        # cuts to the last N lines of any level first, and an error followed by a lot of chatter would be lost.
        level = "V" if everything else "W" if pid else "E"  # quoted below: the device's shell would expand the *
        cmd = "logcat -d -v brief " + (f"--pid={pid[0]} " if pid else "-t 3000 ") + f"'*:{level}'"
        lines = [ln for ln in self.shell(cmd, timeout=60).splitlines() if ln.strip() and not ln.startswith("-----")]
        if lines:
            out += ["— logcat" + (" of this run of the app" if pid else "") + ("" if everything else ", warnings and errors") + " —"] \
                + squeeze(lines)[-40:]
        return out

    def crash(self, app: str, seconds: int) -> List[str]:
        """The newest crash in the device's crash log that is this app's and happened in the last `seconds`, by the
        device's own clock (the log keeps old crashes until the device restarts)."""
        raw = self.shell("logcat -b crash -d -v time -t 500", timeout=40)
        lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.startswith("-----")]
        starts = [i for i, ln in enumerate(lines) if re.search(r"FATAL EXCEPTION|ANR in |Fatal signal ", ln)]
        mine = starts
        if app:  # «Process: net.ani, PID: 1», «>>> net.ani <<<», «ANR in net.ani» — not net.ani.tv
            name = re.compile(r"(?<![\w.])" + re.escape(app) + r"(?![\w.])")
            mine = [i for i in starts if any(name.search(ln) for ln in lines[i:i + 6])]
        if not mine:
            return []
        first = mine[-1]
        block = lines[first:min([j for j in starts if j > first] + [first + 30])]
        stamp = r"(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)"
        then, now = re.match(stamp, block[0]), re.search(stamp, self.shell("date '+%m-%d %H:%M:%S'"))
        try:  # the log has no year: 2000 is a leap one, so every date in it exists
            age = (dt.datetime(2000, *(int(v) for v in now.groups())) - dt.datetime(2000, *(int(v) for v in then.groups()))).total_seconds()
        except (AttributeError, ValueError):
            return block  # no clock to compare with: better shown than hidden
        return block if -3600 <= age <= seconds + 30 else []  # otherwise an old crash, not what happened now

    # ---- look and feel
    def appearance(self) -> str:
        """"dark", "light", or the device's own word for a mode that follows something (auto, custom_schedule,
        custom_bedtime); "" when it doesn't say."""
        m = re.search(r"night mode:\s*(\w+)", self.shell("cmd uimode night"), re.I)
        return {"yes": "dark", "no": "light"}.get(m.group(1).lower(), m.group(1).lower()) if m else ""

    def is_dark(self) -> bool:
        """Whether the screen is currently in night mode (including under `auto` / schedules)."""
        m = re.search(r"\bmCurUiMode=0x([0-9a-f]+)", self.shell("dumpsys uimode"), re.I)
        if m:
            return (int(m.group(1), 16) & 0x30) == 0x20
        return self.appearance() == "dark"

    def set_appearance(self, mode: str) -> None:
        self.shell("cmd uimode night " + shlex.quote({"dark": "yes", "light": "no"}.get(mode, mode)))

    def text_size(self) -> str:
        value = self.shell("settings get system font_scale").strip()
        return value if re.fullmatch(r"\d+(\.\d+)?", value) else "1.0"  # «null» until someone sets it

    def set_text_size(self, size: str) -> None:
        self.shell("settings put system font_scale " + shlex.quote(ANDROID_SIZES.get(size, size)))


def android_text(text: str) -> List[str]:
    """The pieces to hand to `input text`, one call each. It reads «%s» as a space, so spaces become %s — and a «%s»
    that is really in the text is split between two calls, where it can't be read that way."""
    return [part.replace(" ", "%s") for part in re.split(r"(?<=%)(?=s)", text) if part]


def parse_uiautomator(xml: str) -> Tuple[List[dict], dict]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise Fail("the device's element list isn't valid XML; try again.")
    edges = [re.match(r"\[-?\d+,-?\d+\]\[(-?\d+),(-?\d+)\]", n.attrib.get("bounds", "")) for n in root.findall("node")]
    w = max([int(m.group(1)) for m in edges if m] or [0])  # the windows on the screen span it
    h = max([int(m.group(2)) for m in edges if m] or [0])
    out: List[dict] = []
    app = ""
    for node in root.iter("node"):
        a = node.attrib
        m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        app = app or a.get("package", "")
        label = a.get("text") or a.get("content-desc") or ""
        ident = a.get("resource-id", "").split(":id/")[-1]
        cls = a.get("class", "").rsplit(".", 1)[-1]
        field = "EditText" in cls or a.get("password") == "true"
        clickable = a.get("clickable") == "true" or a.get("long-clickable") == "true" or a.get("checkable") == "true"
        if not (label or ident or clickable or field or a.get("scrollable") == "true"):
            continue
        flags = [name for name, key in (("tap", "clickable"), ("scroll", "scrollable"), ("focused", "focused"),
                                        ("checked", "checked"), ("selected", "selected")) if a.get(key) == "true"]
        if field:
            flags.append("field")
        if a.get("enabled") == "false":
            flags.append("disabled")
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        if not (0 <= cx <= w and 0 <= cy <= h):
            flags.append("off-screen")
        # A field's text attribute is what was typed — or its hint while it is empty: newer Android names the hint
        # separately, so the two can be told apart there. Kept apart from a button's label either way.
        hint = a.get("hint", "") if field else ""
        typed = a.get("text", "") if field else ""
        out.append({"role": cls, "label": "" if field else label, "value": "" if hint and typed == hint else typed,
                    "hint": hint, "id": ident, "x": cx, "y": cy, "w": x2 - x1, "h": y2 - y1, "flags": flags})
    return out, {"app": app, "pid": None, "cut": False}


def apk_package(path: str) -> str:
    """The package name in an APK's binary AndroidManifest.xml."""
    try:
        with zipfile.ZipFile(path) as z:
            data = z.read("AndroidManifest.xml")
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""
    strings: List[str] = []
    off = 8
    try:
        while off + 8 <= len(data):
            kind, head, size = struct.unpack_from("<HHI", data, off)
            if size < 8:
                break
            if kind == 0x0001:  # the string pool
                count, _styles, flags, start = struct.unpack_from("<IIII", data, off + 8)
                for pos in struct.unpack_from(f"<{count}I", data, off + head):
                    p = off + start + pos
                    if flags & 0x100:  # UTF-8: length in characters, length in bytes, bytes
                        for _ in range(2):
                            n = data[p]
                            p += 1
                            if n & 0x80:
                                n = ((n & 0x7F) << 8) | data[p]
                                p += 1
                        strings.append(data[p:p + n].decode("utf-8", "replace"))
                    else:
                        n = struct.unpack_from("<H", data, p)[0]
                        p += 2
                        if n & 0x8000:
                            n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0]
                            p += 2
                        strings.append(data[p:p + n * 2].decode("utf-16-le", "replace"))
            elif kind == 0x0102:  # the first element is <manifest>
                start, attr_size, attr_count = struct.unpack_from("<HHH", data, off + 24)
                for i in range(attr_count):
                    _ns, name, raw, _size, _res, dtype, value = struct.unpack_from("<IIIHBBI", data, off + 16 + start + i * attr_size)
                    if 0 <= name < len(strings) and strings[name] == "package":
                        idx = raw if raw != 0xFFFFFFFF else value if dtype == 3 else -1
                        return strings[idx] if 0 <= idx < len(strings) else ""
                return ""
            off += size
    except (struct.error, IndexError):
        pass
    return ""


# ----------------------------------------------------------------------------------------------- choosing a device

def running(args) -> List[dict]:
    out: List[dict] = []
    if not getattr(args, "android", False):
        out += [d for d in ios_devices() if d["running"]]
    if not getattr(args, "ios", False):
        out += [d for d in android_running() if d["running"]]
    return out


def describe_device(d: dict) -> str:
    kind = "simulator" if d["platform"] == "ios" else d["kind"]
    return f"{d['name']} — {d.get('os') or 'Android'} {kind} — {d['id']}"


def make(d: dict):
    return Sim(d) if d["platform"] == "ios" else Droid(d)


def choose(args):
    """The device a command acts on: the one named, or the only simulator or emulator that is running."""
    want = (getattr(args, "device", "") or "").strip().casefold()
    cands = running(args)
    if want:
        found = [d for d in cands if want in (d["id"].casefold(), d["name"].casefold())]
        found = found or [d for d in cands if want in d["name"].casefold() or d["id"].casefold().startswith(want)]
        if len(found) == 1:
            return make(found[0])
        if not found:
            raise Fail(f"“{args.device}” isn't running. Start it: {me()} boot \"{args.device}\""
                       + ("\nRunning now:\n" + "\n".join("  " + describe_device(d) for d in cands) if cands else ""))
        raise Fail(f"“{args.device}” matches several:\n" + "\n".join("  " + describe_device(d) for d in found))
    cands = [d for d in cands if d["kind"] != "device"]  # a real phone only when named
    if len(cands) == 1:
        return make(cands[0])
    if not cands:
        ios, android = getattr(args, "ios", False), getattr(args, "android", False)
        raise Fail(f"No {'iOS simulator' if ios else 'Android emulator' if android else 'simulator or emulator'} is running. "
                   f"Start one: {me()} boot{' --ios' if ios else ' --android' if android else ''}   (what's there: {me()} devices)")
    raise Fail("Several are running — add --ios, --android or --device NAME:\n" + "\n".join("  " + describe_device(d) for d in cands))


# ------------------------------------------------------------------------------------------------------- projects

def walk_dirs(root: str, depth: int):
    """Folders under root, down to `depth` levels, skipping build output and dependencies."""
    todo = [(root, 0)]
    while todo:
        folder, level = todo.pop(0)
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        yield folder, entries
        if level < depth:
            todo += [(e.path, level + 1) for e in entries if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS
                     and not e.name.startswith(".") and not e.name.endswith((".xcodeproj", ".xcworkspace", ".app", ".framework"))]


def detect(root: str) -> dict:
    """The mobile apps in a project folder, from its files alone (nothing is run):
    {"ios": [{dir, container, flag, scheme, bundle}], "android": [{dir, module, package, flavors, tv}],
     "flutter": dir or "", "rn": "expo" | "react-native" | ""}."""
    root = os.path.abspath(root)
    found = {"ios": [], "android": [], "flutter": "", "rn": ""}
    for folder, entries in walk_dirs(root, 3):
        names = {e.name for e in entries}
        if "pubspec.yaml" in names and re.search(r"^\s*flutter\s*:", read(os.path.join(folder, "pubspec.yaml"), 200000), re.M):
            found["flutter"] = found["flutter"] or folder
        if "package.json" in names and not found["rn"]:
            deps = re.findall(r'"(expo|react-native)"\s*:', read(os.path.join(folder, "package.json"), 400000))
            found["rn"] = "expo" if "expo" in deps else "react-native" if deps else ""
        projects = sorted(n for n in names if n.endswith(".xcodeproj"))
        spaces = sorted(n for n in names if n.endswith(".xcworkspace"))
        if projects or spaces:
            container = spaces[0] if spaces else projects[0]
            project = os.path.join(folder, projects[0]) if projects else ""
            schemes = []
            for place in dict.fromkeys(filter(None, (project, os.path.join(folder, container)))):
                try:
                    schemes += [fn[:-9] for fn in sorted(os.listdir(os.path.join(place, "xcshareddata", "xcschemes")))
                                if fn.endswith(".xcscheme")]
                except OSError:
                    pass
            schemes = list(dict.fromkeys(s for s in schemes if not re.search(r"Tests?$", s))) or [os.path.splitext(container)[0]]
            pbx = read(os.path.join(project, "project.pbxproj")) if project else ""
            if pbx and not re.search(r"IPHONEOS_DEPLOYMENT_TARGET|TARGETED_DEVICE_FAMILY|iphonesimulator|SDKROOT = (iphoneos|auto)", pbx):
                continue  # a Mac (or TV, or watch) app only: nothing for an iPhone simulator
            ids = re.findall(r"PRODUCT_BUNDLE_IDENTIFIER = \"?([\w.-]+)\"?;", pbx)
            ids = sorted({i for i in ids if not re.search(r"tests?$", i, re.I)}, key=lambda i: (len(i), i))
            found["ios"].append({"dir": folder, "container": container, "scheme": schemes[0], "schemes": schemes,
                                 "flag": "-workspace" if spaces else "-project", "bundle": ids[0] if ids else ""})
        settings = next((n for n in ("settings.gradle.kts", "settings.gradle") if n in names), "")
        if settings:
            text = read(os.path.join(folder, settings), 200000)
            modules = re.findall(r"""["']:([\w:.-]+)["']""", "\n".join(re.findall(r"^\s*include\b.*$", text, re.M))) or ["app"]
            for module in modules:
                mdir = os.path.join(folder, *module.split(":"))
                build = read(os.path.join(mdir, "build.gradle.kts"), 400000) or read(os.path.join(mdir, "build.gradle"), 400000)
                if not re.search(r"com\.android\.application|android\.application\b|androidApplication", build):
                    continue
                package = re.search(r"""applicationId\s*=?\s*\(?\s*["']([\w.]+)["']""", build)
                manifest = read(os.path.join(mdir, "src", "main", "AndroidManifest.xml"), 400000)
                found["android"].append({"dir": folder, "module": module, "package": package.group(1) if package else "",
                                         "flavors": gradle_flavors(build), "tv": "LEANBACK_LAUNCHER" in manifest})
    return found


def gradle_flavors(build: str) -> List[str]:
    m = re.search(r"productFlavors\s*\{", build)
    if not m:
        return []
    depth, i, names = 1, m.end(), []
    while i < len(build) and depth:
        ch = build[i]
        if ch == "{":
            if depth == 1:
                head = build[max(m.end(), build.rfind("\n", 0, i) + 1):i]
                name = re.search(r"""(?:create|register|maybeCreate)\(\s*["'](\w+)["']|^\s*(\w+)\s*$""", head)
                if name:
                    names.append(name.group(1) or name.group(2))
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return names


def derived_products(container: str) -> str:
    """Where Xcode puts simulator builds of this project: DerivedData/<name>-<hash>/Build/Products/Debug-iphonesimulator."""
    base = os.path.expanduser("~/Library/Developer/Xcode/DerivedData")
    name = os.path.splitext(os.path.basename(container))[0]
    best = ""
    try:
        folders = [os.path.join(base, fn) for fn in os.listdir(base) if fn.startswith(name + "-")]
    except OSError:
        return ""
    for folder in folders:
        try:
            with open(os.path.join(folder, "info.plist"), "rb") as f:
                same = os.path.realpath(str(plistlib.load(f).get("WorkspacePath") or "")) == os.path.realpath(container)
        except (OSError, ValueError, plistlib.InvalidFileException):
            same = False
        products = os.path.join(folder, "Build", "Products", "Debug-iphonesimulator")
        if same and os.path.isdir(products) and (not best or os.path.getmtime(products) > os.path.getmtime(best)):
            best = products
    return best


TEST_BUILD = re.compile(r"androidTest|unitTest|Tests?-Runner\.app$|Tests\.app$|[-_.]tests?\.apk$", re.I)


def newest(folder: str, suffix: str, depth: int = 4) -> str:
    """The most recently built .app or debug .apk under a folder: the app itself, not its tests' runner."""
    best, best_time = "", 0.0
    for base, entries in walk_dirs_all(folder, depth):
        for e in entries:
            if e.name.endswith(suffix) and (suffix != ".apk" or "release" not in e.name.lower()) \
                    and not TEST_BUILD.search(os.path.relpath(e.path, folder)):
                if suffix == ".app" and not os.path.isfile(os.path.join(e.path, "Info.plist")):
                    continue
                t = os.path.getmtime(e.path)
                if t > best_time:
                    best, best_time = e.path, t
    return best


def walk_dirs_all(root: str, depth: int):
    todo = [(root, 0)]
    while todo:
        folder, level = todo.pop(0)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        yield folder, entries
        if level < depth:
            todo += [(e.path, level + 1) for e in entries if e.is_dir(follow_symlinks=False) and not e.name.endswith((".app", ".dSYM"))]


def built_artifact(root: str, platform: str = "") -> str:
    """The newest build of the project in `root` that a simulator or an emulator can run."""
    found = detect(root)
    cands = []
    if platform != "android":
        for p in found["ios"]:
            for place in (derived_products(os.path.join(p["dir"], p["container"])), os.path.join(p["dir"], "build")):
                app = newest(place, ".app") if place and os.path.isdir(place) else ""
                if app and "iphonesimulator" in app:
                    cands.append(app)
        if found["flutter"]:
            cands.append(newest(os.path.join(found["flutter"], "build", "ios", "iphonesimulator"), ".app", 1))
    if platform != "ios":
        for p in found["android"]:
            cands.append(newest(os.path.join(p["dir"], *p["module"].split(":"), "build", "outputs", "apk"), ".apk"))
        if found["flutter"]:
            cands.append(newest(os.path.join(found["flutter"], "build", "app", "outputs", "flutter-apk"), ".apk", 1))
    cands = [c for c in cands if c]
    return max(cands, key=built_at) if cands else ""


def built_at(artifact: str) -> float:
    """When a build was made. A folder's own time doesn't move when a file inside is rewritten, so an .app is as new
    as the newest thing directly in it."""
    times = [os.path.getmtime(artifact)]
    if os.path.isdir(artifact):
        try:
            times += [e.stat().st_mtime for e in os.scandir(artifact)]
        except OSError:
            pass
    return max(times)


TEST_DIR = re.compile(r"^(tests?|androidTest|unitTest|sharedTest|testFixtures|__tests__|\w*Tests)$")


def stale_note(artifact: str, root: str) -> str:
    """A note when one of the app's own source files is newer than the build about to be shown. Only what goes into
    that build counts: its platform's sources, not tests, not the server next door."""
    found = detect(root)
    if artifact.endswith(".apk"):
        folders, kinds = [p["dir"] for p in found["android"]], (".kt", ".java", ".xml", ".kts")
    else:
        folders, kinds = [p["dir"] for p in found["ios"]], (".swift", ".m", ".mm", ".h", ".storyboard", ".xib", ".xcstrings", ".strings")
    if found["flutter"]:
        folders, kinds = folders + [os.path.join(found["flutter"], "lib")], kinds + (".dart",)
    built = built_at(artifact)
    newest_path, newest_time, seen = "", built + 2, 0
    for top in dict.fromkeys(folders):
        todo = [top]
        while todo and seen < 20000:
            folder = todo.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for e in entries:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in SKIP_DIRS and not e.name.startswith(".") and not TEST_DIR.match(e.name) \
                            and not e.name.endswith((".app", ".framework", ".xcassets")):
                        todo.append(e.path)
                elif e.name.endswith(kinds):
                    seen += 1
                    if e.stat().st_mtime > newest_time:
                        newest_path, newest_time = e.path, e.stat().st_mtime
    if not newest_path:
        return ""
    return (f"⚠️ {os.path.relpath(newest_path, root)} was changed {ago(time.time() - newest_time)} ago, after this build was "
            f"made ({ago(time.time() - built)} ago). If you haven't built since that change, build first: this would show "
            "the old code.")


def session_lines(ws: str) -> List[str]:
    """For the session context (hooks/session_start.py): the mobile app in this workspace and the commands that show
    it running. Reads only the project's files: nothing is started."""
    found = detect(ws)
    kinds = []
    if found["flutter"]:
        kinds.append("Flutter")
    if found["rn"]:
        kinds.append("Expo" if found["rn"] == "expo" else "React Native")
    if found["ios"] and not found["flutter"]:
        kinds.append("iOS" + (f" ({found['ios'][0]['bundle']})" if found["ios"][0]["bundle"] else ""))
    if found["android"] and not found["flutter"]:
        kinds.append("Android (" + ", ".join(":" + p["module"] for p in found["android"][:3]) + ")")
    if not kinds:
        return []
    cross = bool(found["flutter"] or found["rn"])
    ios, android = bool(xcrun()) and (cross or bool(found["ios"])), bool(adb()) and (cross or bool(found["android"]))
    if not (ios or android):
        return [f"Mobile app here ({', '.join(kinds)}), but this computer has no Xcode simulator or Android SDK for it: it "
                "can't be run here — say so when a change needs a look."]
    where = " or ".join(filter(None, ["the iOS simulator" if ios else "", "the Android emulator" if android else ""]))
    return [f"Mobile app here: {', '.join(kinds)}. To see a change, run it in {where} and look (skill /mobile; these "
            "commands need BypassSandbox: true):",
            f"- how to build it and where the app lands: `{me()} project \"{ws}\"`",
            f"- after a build: `{me()} run --project \"{ws}\"` — installs, launches, takes a screenshot (open it with "
            "read_file) and shows the app's errors; `boot` starts a simulator or an emulator when none is running",
            "- then `ui` lists what is on the screen, and `tap --text \"…\"`, `swipe up`, `type \"…\"` use it."]


# ------------------------------------------------------------------------------------------------------- commands

def shot_path(dev, suffix: str = "", out: str = "") -> str:
    if out:
        out = os.path.abspath(os.path.expanduser(out))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        return out
    os.makedirs(SHOTS, exist_ok=True)
    try:
        old = sorted((e.stat().st_mtime, e.path) for e in os.scandir(SHOTS) if e.name.endswith(".png"))
        for _t, path in old[:-KEEP_SHOTS]:
            os.remove(path)
    except OSError:
        pass
    name = re.sub(r"[^A-Za-z0-9]+", "-", dev.name).strip("-").lower() or dev.platform
    base = os.path.join(SHOTS, f"{time.strftime('%Y%m%d-%H%M%S')}-{name}{'-' + suffix if suffix else ''}")
    extra = next(n for n in range(1, 1000) if not os.path.exists(f"{base}{'' if n == 1 else f'-{n}'}.png"))
    return f"{base}{'' if extra == 1 else f'-{extra}'}.png"


def take(dev, suffix: str = "", out: str = "", label: str = "Screenshot") -> str:
    path = shot_path(dev, suffix, out)
    w, h = dev.shot(path)
    note = ""
    if os.path.getsize(path) < 12000:
        note = "  ⚠️ almost one flat colour: a blank screen, or the app is still starting"
    print(f"{label}: {path}  ({w}×{h} px){note}")
    return path


def after(dev, args) -> None:
    """--shot on an action: wait for the screen to settle, then show it."""
    if getattr(args, "shot", False):
        time.sleep(max(0.0, getattr(args, "wait", 1.0)))
        take(dev)
        print("Look at it (read_file) before the next step; coordinates are its pixels.")


def cmd_devices(args) -> int:
    print("## Devices this machine can run\n")
    ios = ios_devices()
    if xcrun():
        _c, version, _e = run(["xcodebuild", "-version"], 15)
        print(f"iOS simulators ({(version.splitlines() or ['Xcode'])[0]}):")
        for d in ios[:12]:
            print(f"  {'▶' if d['running'] else ' '} {d['name']} — {d['os']}{' — running' if d['running'] else ''} — {d['id']}")
        if not ios:
            print("  none: add one in Xcode → Window → Devices and Simulators.")
        booted = next((d for d in ios if d["running"]), None)
        try:
            bridge_bin()
            if booted:
                Sim(booted).bridge("probe")
            print("  touches, text, the element list: built in" + ("" if booted else " (checked when a simulator runs)"))
        except Fail as e:
            print("  " + unsupported_hint(str(e)).replace("\n", "\n  "))
    else:
        print("iOS simulators: not on this machine (they need a Mac with Xcode).")
    print()
    sdk, a = android_sdk(), adb()
    if not a:
        print("Android: the SDK wasn't found (ANDROID_HOME, local.properties, the usual folders). Android Studio installs it.")
    else:
        live = android_running()
        print(f"Android (SDK {sdk or 'on PATH'}):")
        for d in live:
            state = "running" if d["running"] else d.get("state", "")
            real = " — a REAL device: used only with --device " + d["id"] if d["kind"] == "device" else ""
            print(f"  ▶ {d['name']} — {d['kind']} {d['id']} — {state}{real}")
        names = {d["name"] for d in live}
        for v in android_avds():
            if v["name"] not in names:
                print(f"    {v['name']} — {v['form']}, API {v['api'] or '?'}{', ' + v['size'] if v['size'] else ''}")
        if not live and not android_avds():
            print("  no emulators: create one in Android Studio → Device Manager.")
        if not shutil.which("adb"):
            print(f"  adb isn't on PATH. This script finds it; in your own commands use the full path: {a}")
        if not sdk_tool("emulator", "emulator"):
            print("  the emulator itself isn't installed (SDK Manager → Android Emulator).")
        if shutil.which("android"):
            print("  the `android` CLI is here too: `android layout` and `android screen` use the same pixel coordinates.")
    note = free_space_note()
    if note:
        print("\n⚠️ " + note)
    print(f"\nStart one: {me()} boot [NAME]     After a build: {me()} run")
    return 0


def build_line(folder: str, command: str, tail: int = 30) -> str:
    """A build command the agent can paste: for sh on macOS and Linux, for PowerShell on Windows (no `&&` and no
    `tail` there). Only the end of the output is kept: that's where a build says what failed."""
    if compat.WINDOWS:
        return f'cd "{folder}"; {command} 2>&1 | Select-Object -Last {tail}'
    return f'cd "{folder}" && {command} 2>&1 | tail -{tail}'


def cmd_project(args) -> int:
    root = os.path.abspath(os.path.expanduser(args.dir or "."))
    found = detect(root)
    if not (found["ios"] or found["android"] or found["flutter"] or found["rn"]):
        print(f"No mobile project found in {root} (no .xcodeproj, no Gradle app module, no Flutter or React Native).")
        return 1
    print(f"## Mobile project in {root}\n")
    sims = ios_devices()
    sim = next((d for d in sims if d["running"]), sims[0] if sims else None)
    if found["flutter"]:
        print(f"Flutter app in {found['flutter']}:")
        if compat.MAC:
            print("  iOS:     " + build_line(found["flutter"], "flutter build ios --simulator --debug", 20))
        print("  Android: " + build_line(found["flutter"], "flutter build apk --debug", 20))
        print(f"  then:    {me()} run --project \"{root}\" " + ("--ios   (or --android)\n" if compat.MAC else "--android\n"))
    if found["rn"]:
        print(f"{'Expo' if found['rn'] == 'expo' else 'React Native'} app: a debug build loads its JavaScript from Metro. Keep "
              f"`npx {'expo start' if found['rn'] == 'expo' else 'react-native start'}` running in the background, and for "
              f"Android run `adb reverse tcp:8081 tcp:8081` once per emulator start.\n")
    for p in found["ios"]:
        if found["flutter"] and p["dir"].startswith(found["flutter"]):
            continue
        dest = f"platform=iOS Simulator,id={sim['id']}" if sim else "generic/platform=iOS Simulator"
        print(f"iOS app — {os.path.join(p['dir'], p['container'])}")
        print(f"  scheme {p['scheme']}" + (f" (also: {', '.join(p['schemes'][1:4])})" if len(p["schemes"]) > 1 else "")
              + (f", bundle id {p['bundle']}" if p["bundle"] else ""))
        if not xcrun():
            print("  it can be built and run only on a Mac with Xcode: not on this computer.\n")
            continue
        print("  build:  " + build_line(p["dir"], f"xcodebuild {p['flag']} \"{p['container']}\" -scheme \"{p['scheme']}\" "
                                                  f"-configuration Debug -destination '{dest}' -quiet build"))
        products = derived_products(os.path.join(p["dir"], p["container"]))
        print(f"  run:    {me()} run " + (f'"{products}"' if products else f'--ios --project "{root}"')
              + (f"        (on {sim['name']}, {sim['os']}" + ("" if sim["running"] else f"; start it first: {me()} boot --ios") + ")"
                 if sim else ""))
        print()
    for p in found["android"]:
        if found["flutter"] and p["dir"].startswith(found["flutter"]):
            continue
        variant = (p["flavors"][0].capitalize() if p["flavors"] else "") + "Debug"
        wrapper = ".\\gradlew.bat" if compat.WINDOWS else "./gradlew"
        print(f"Android app — module :{p['module']} in {p['dir']}" + (" (Android TV)" if p["tv"] else ""))
        if p["package"]:
            print(f"  package {p['package']}" + (" (a flavor may change it)" if p["flavors"] else ""))
        if p["flavors"]:
            print(f"  flavors: {', '.join(p['flavors'])} — the commands below use {p['flavors'][0]}")
        print("  build:  " + build_line(p["dir"], f"{wrapper} :{p['module']}:assemble{variant} -q"))
        outputs = os.path.join(p["dir"], *p["module"].split(":"), "build", "outputs", "apk")
        print(f"  run:    {me()} run \"{outputs}\"")
        if p["tv"]:
            tv = next((v["name"] for v in android_avds() if v["form"] == "TV"), "")
            print("  it's a TV app: run it on an Android TV emulator" + (f" (`boot {tv}`)" if tv else "")
                  + " and move with `key up|down|left|right|center|back`.")
        print()
    built = built_artifact(root)
    if built:
        print(f"Newest build found: {built} ({ago(time.time() - built_at(built))} ago)")
        note = stale_note(built, root)
        if note:
            print(note)
    else:
        print("No build found yet: build first.")
    return 0


def cmd_boot(args) -> int:
    want = (args.name or "").strip().casefold()
    sims = [] if args.android else ios_devices()
    avds = [] if args.ios else android_avds()
    live = [] if args.ios else android_running()
    pick_sim = next((d for d in sims if want and want in (d["name"].casefold(), d["id"].casefold())), None)
    pick_avd = next((v for v in avds if want and want == v["name"].casefold()), None)
    if want and not (pick_sim or pick_avd):
        pick_sim = next((d for d in sims if want in d["name"].casefold()), None)
        pick_avd = next((v for v in avds if want in v["name"].casefold()), None) if not pick_sim else None
    if want and not (pick_sim or pick_avd):
        raise Fail(f"Nothing is called “{args.name}”. See: {me()} devices")
    if not want:  # no name: what the project here needs — an emulator for an Android app, a TV one for a TV app
        found = detect(project_root(args))
        android_app = bool(found["android"]) and not (found["ios"] or found["flutter"] or found["rn"])
        form = "TV" if found["android"] and all(p["tv"] for p in found["android"]) else "phone"
        if args.android or (android_app and not args.ios) or (not sims and avds):
            pick_avd = next((v for v in avds if v["form"] == form), avds[0] if avds else None)
            if not pick_avd and (args.android or android_app):
                raise Fail("There's no Android emulator to start: create one in Android Studio → Device Manager.")
        else:
            pick_sim = sims[0] if sims else None
    if pick_sim:
        sim = Sim(pick_sim)
        try:
            if pick_sim["running"] and not args.restart:
                print(f"Already running: {describe_device(pick_sim)}")
            elif pick_sim["running"] and sim.touch_ok():
                print(f"{describe_device(pick_sim)} takes touches: nothing to restart. If a tap still does nothing, look where "
                      "it lands (`ui`, a screenshot) — the app may be busy, or something may be over the button.")
            else:
                touches: Optional[bool] = None
                if pick_sim["running"]:  # the cheap cure first: the input service alone
                    print("The simulator takes no touches: restarting its input service.", flush=True)
                    touches = sim.revive()
                for attempt in (1, 2):
                    if touches:
                        break
                    if pick_sim["running"] or attempt == 2:
                        simctl("shutdown", pick_sim["id"], timeout=90)
                        time.sleep(2)
                    code, _, err = simctl("bootstatus", pick_sim["id"], "-b", timeout=400)
                    if code != 0:
                        raise Fail("the simulator didn't boot: " + (err.strip().splitlines() or ["no reason given"])[-1][:300]
                                   + ("\n" + free_space_note() if free_space_note() else ""))
                    time.sleep(3)
                    touches = sim.working_touches()
                    if touches is None:
                        break
                now = next((d for d in ios_devices() if d["id"] == pick_sim["id"]), pick_sim)
                if not pick_sim["running"]:
                    remember("booted", pick_sim["id"], {"at": now["last"]})  # this very boot: `shutdown` stops only that
                print(f"Booted: {describe_device(pick_sim)}" + ("; touches arrive" if touches else
                      "; touches still don't arrive — screenshots, launching and text work, taps and swipes don't" if touches is False else ""))
        except Unsupported as e:
            print(f"Booted: {describe_device(pick_sim)}")
            print(unsupported_hint(str(e)))
        if args.show:
            print(show_simulator(pick_sim["id"]))
        return 0
    if not pick_avd:
        raise Fail("There's nothing to start: no simulators and no emulators. " + ("Xcode adds simulators; " if compat.MAC else "")
                   + "Android Studio → Device Manager adds emulators.")
    already = next((d for d in live if d["name"] == pick_avd["name"] and d["running"]), None)
    if already:
        print(f"Already running: {describe_device(already)}")
        return 0
    return boot_emulator(pick_avd, args.show)


def show_simulator(udid: str) -> str:
    """Open the window that shows simulators: DeviceHub in Xcode 27, Simulator.app before it."""
    dev = developer_dir()
    for app, extra in ((os.path.join(os.path.dirname(dev), "Applications", "DeviceHub.app"), []),
                       (os.path.join(dev, "Applications", "Simulator.app"), ["--args", "-CurrentDeviceUDID", udid])):
        if os.path.isdir(app) and run(["open", "-a", app, *extra], 20)[0] == 0:
            return f"{os.path.basename(app)[:-4]} is open: the user can watch the simulator there."
    return ("This Xcode has no window app for simulators that could be opened from here; the simulator runs without a "
            "window. The user can open it from Xcode (Window → Devices and Simulators).")


def boot_emulator(avd: dict, show: bool) -> int:
    emu, a = sdk_tool("emulator", "emulator"), need_adb()
    if not emu:
        raise Fail("The Android emulator isn't installed (Android Studio → SDK Manager → Android Emulator).")
    os.makedirs(os.path.join(HOME, "logs"), exist_ok=True)
    log = os.path.join(HOME, "logs", f"emulator-{avd['name']}.log")
    before = {d["id"] for d in android_running()}
    flags = ["-avd", avd["name"], "-no-boot-anim"] + ([] if show else ["-no-window"])
    # When it stops, an emulator writes its whole state to disk — gigabytes. With little room left it is started so
    # that it doesn't: what was installed in this session is then gone after the next start.
    try:
        tight = shutil.disk_usage(os.path.expanduser("~")).free < 6 * 1024 ** 3
    except OSError:
        tight = False
    if tight:
        flags.append("-no-snapshot-save")
    with open(log, "wb") as out:
        extra = {"creationflags": 0x00000008 | 0x00000200} if compat.WINDOWS else {"start_new_session": True}
        env = dict(os.environ, ANDROID_SDK_ROOT=android_sdk() or os.environ.get("ANDROID_SDK_ROOT", ""))
        proc = subprocess.Popen([emu, *flags], stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, **extra)
    deadline, serial = time.time() + 300, ""
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = [ln for ln in read(log, 400000).splitlines() if re.search(r"FATAL|ERROR|PANIC|not enough|cannot", ln, re.I)]
            reason = (tail or read(log, 400000).splitlines() or ["no output"])[-1].strip()
            raise Fail(f"the emulator stopped while starting: {reason[:300]}\nFull log: {log}"
                       + ("\n" + free_space_note() if free_space_note() else ""))
        if not serial:
            serial = next((d["id"] for d in android_running() if d["kind"] == "emulator" and d["id"] not in before), "")
        if serial:
            _c, done, _e = run([a, "-s", serial, "shell", "getprop", "sys.boot_completed"], 10)
            if done.strip() == "1":
                remember("booted", serial, {"pid": proc.pid, "avd": avd["name"]})
                print(f"Booted: {avd['name']} — Android emulator — {serial}" + ("" if show else
                      "  (no window: the user can watch it in the Android Device Hub pane, or start it with --show)"))
                if tight:
                    print("The disk is nearly full, so it was started without saving its state on exit: apps installed "
                          "now will be gone after its next start.")
                return 0
        time.sleep(2)
    raise Fail(f"the emulator didn't finish booting in 5 minutes. Its log: {log}")


def cmd_shutdown(args) -> int:
    st = load_state()
    mine = st.get("booted", {})
    targets = [d for d in running(args) if d["kind"] != "device"]
    if args.device:
        want = args.device.casefold()
        targets = [d for d in targets if want in (d["id"].casefold(), d["name"].casefold())]
    elif not args.all:
        targets = [d for d in targets if started_here(d, mine.get(d["id"]))]
    if not targets:
        print("Nothing to shut down" + ("." if args.all or args.device else
              " that this tool started (the ones the user started stay; --all or --device NAME to stop them)."))
        return 0
    for d in targets:
        if d["platform"] == "ios":
            simctl("shutdown", d["id"], timeout=60)
        else:
            if run([need_adb(), "-s", d["id"], "emu", "kill"], 30)[0] == 0:
                deadline = time.time() + 25
                while time.time() < deadline and any(x["id"] == d["id"] for x in android_running()):
                    time.sleep(0.5)
        mine.pop(d["id"], None)
        print(f"Shut down: {describe_device(d)}")
    st["booted"] = mine
    save_state(st)
    return 0


def started_here(d: dict, note) -> bool:
    """Is this running device the one this tool booted — not one the user started later under the same id?"""
    if not isinstance(note, dict):
        return False
    if d["platform"] == "ios":
        return bool(note.get("at")) and note.get("at") == d.get("last")
    try:
        return any(pid == note.get("pid") for pid, _cmd in compat.processes("-avd " + str(note.get("avd"))))
    except Exception:
        return False


def app_id(path: str) -> str:
    if path.endswith(".apk"):
        return apk_package(path)
    try:
        with open(os.path.join(path, "Info.plist"), "rb") as f:
            return str(plistlib.load(f).get("CFBundleIdentifier") or "")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return ""


def project_root(args) -> str:
    return os.path.abspath(os.path.expanduser(getattr(args, "project", "") or os.getcwd()))


def resolve_artifact(args) -> str:
    """The .app or .apk a command should install: the path given, the newest one in a folder given, or the newest
    build of the project in the current folder."""
    given = os.path.abspath(os.path.expanduser(args.path)) if getattr(args, "path", "") else ""
    platform = "ios" if args.ios else "android" if args.android else ""
    if given and os.path.isdir(given) and not given.endswith(".app"):
        found = max(filter(None, [newest(given, ".app") if platform != "android" else "",
                                  newest(given, ".apk") if platform != "ios" else ""]), key=os.path.getmtime, default="")
        if not found:
            raise Fail(f"no .app or debug .apk under {given}")
        given = found
    if not given:
        root = project_root(args)
        given = built_artifact(root, platform)
        if not given:
            raise Fail(f"No build found for the project in {root}. Build it first ({me()} project prints the "
                       "command), or pass the path of the .app or .apk.")
    if given.endswith(".ipa"):
        raise Fail("An .ipa is a build for a real iPhone; the simulator runs the .app from a simulator build.")
    if not os.path.exists(given) or not given.endswith((".app", ".apk")):
        raise Fail(f"{given} isn't an .app (iOS simulator build) or an .apk (Android).")
    return given


def cmd_install(args) -> int:
    path = resolve_artifact(args)
    args.ios, args.android = path.endswith(".app"), path.endswith(".apk")
    dev = choose(args)
    if dev.platform == "android":
        dev.install(path, grant=args.grant)
    else:
        dev.install(path)
    print(f"Installed {app_id(path) or os.path.basename(path)} on {dev.title()}")
    return 0


def cmd_run(args) -> int:
    path = resolve_artifact(args)
    args.ios, args.android = path.endswith(".app"), path.endswith(".apk")
    dev = choose(args)
    app = app_id(path)
    if not app:
        raise Fail(f"couldn't read the app's id from {path}")
    print(f"## {app} on {dev.title()}\n")
    print(f"Build: {path} ({ago(time.time() - built_at(path))} old)")
    root = project_root(args)
    note = stale_note(path, root) if detect(root)["ios" if dev.platform == "ios" else "android"] else ""
    if note:
        print(note)
    print("Installing…")
    if dev.platform == "android":
        dev.install(path, grant=args.grant)
    else:
        dev.install(path)
    started = time.time()
    print(f"Launched: {dev.launch(app, args.arg, args.lang)}")
    time.sleep(max(0.5, args.wait))
    alive = dev.alive(app)
    if alive is False:
        print("⚠️ The app is NOT running any more: it crashed or quit on start.")
    take(dev)
    if alive is not False:
        try:
            elements, info = dev.elements()
            print(f"On screen: {texts(elements) or 'no texts'}")
        except Fail as e:
            print(f"Elements: {str(e).splitlines()[0]}")
    lines = dev.logs(app, int(time.time() - started) + 5)
    print("\n" + ("\n".join(ln[:300] for ln in lines) if lines else "Log: no errors since the launch."))
    print("\nLook at the screenshot (read_file) — a launch without a look isn't a check. Then `ui`, `tap`, `swipe`, `type`.")
    return 0 if alive is not False else 1


def cmd_launch(args) -> int:
    dev = choose(args)
    print(f"Launched {args.app} on {dev.title()}: {dev.launch(args.app, args.arg, args.lang, args.keep)}")
    after(dev, args)
    return 0


def cmd_stop(args) -> int:
    dev = choose(args)
    dev.stop(args.app)
    print(f"Stopped {args.app} on {dev.title()}")
    return 0


def cmd_uninstall(args) -> int:
    dev = choose(args)
    dev.uninstall(args.app)
    print(f"Removed {args.app} (with its data) from {dev.title()}")
    return 0


def cmd_grant(args) -> int:
    dev = choose(args)
    dev.grant(args.app, args.permission)
    print(f"Granted {args.permission} to {args.app} on {dev.title()}")
    return 0


def cmd_open(args) -> int:
    dev = choose(args)
    dev.open_url(args.url)
    print(f"Opened {args.url} on {dev.title()}")
    after(dev, args)
    return 0


def cmd_shot(args) -> int:
    dev = choose(args)
    take(dev, out=args.out)
    if args.dark:
        was = dev.appearance()
        if not was:
            print("Dark mode: this device doesn't say which appearance it's in, so it wasn't switched.")
        else:
            currently_dark = was == "dark" or (was != "light" and getattr(dev, "is_dark", lambda: False)())
            other = "light" if currently_dark else "dark"
            dev.set_appearance(other)
            time.sleep(1.5)
            try:
                take(dev, other, label="Light mode" if other == "light" else "Dark mode")
            finally:
                dev.set_appearance(was)
                if dev.platform == "android":
                    time.sleep(0.8)
    if args.big_text:
        was_size = dev.text_size()
        dev.set_text_size("big")
        time.sleep(1.5)
        try:
            take(dev, "big-text", label="Large text")
        finally:
            dev.set_text_size(was_size or "normal")
            if dev.platform == "android":
                time.sleep(0.8)
    print("Look at " + ("each one" if args.dark or args.big_text else "it") + " (read_file): layout, cut-off or overlapping "
          "content, unreadable text, empty areas. Coordinates for tap and swipe are the picture's pixels.")
    return 0


def cmd_ui(args) -> int:
    dev = choose(args)
    elements, info = dev.elements()
    if args.find:
        elements = find_elements(elements, args.find, every=True) or find_elements(elements, ident=args.find, every=True)
    if args.json:
        print(json.dumps([{k: v for k, v in e.items() if k != "pt" and v not in ("", [])} for e in elements], ensure_ascii=False, indent=1))
        return 0
    w, h = dev.size()
    keyboard = info.get("keyboard") if not args.all else None
    if keyboard:
        elements = [e for e in elements if "key" not in e["flags"]]
    print(f"## {info.get('app') or 'Screen'} on {dev.title()} — {len(elements)} element{'' if len(elements) == 1 else 's'}, "
          f"coordinates in the {w}×{h} screenshot\n")
    if info.get("note"):
        print("⚠️ " + info["note"] + "\n")
    if keyboard:
        print(f"⌨️ The on-screen keyboard is up and covers everything below y={keyboard} (its keys aren't listed; "
              "`type` enters text, `key enter` presses return).\n")
    limit = len(elements) if args.all or args.find else 70
    for n, el in enumerate(elements[:limit], 1):
        print(element_line(el, n))
    if len(elements) > limit:
        print(f"… {len(elements) - limit} more: --all, or --find TEXT")
    if not elements:
        print("nothing found" + (f" for “{args.find}”" if args.find else ": a blank screen, a game or a video — use the screenshot."))
    if info.get("cut"):
        print("(the list was cut: the screen has a very large number of elements)")
    return 0


def target_element(dev, args) -> dict:
    elements, _info = dev.elements()
    found = find_elements(elements, args.text or "", args.id or "")
    what = f"id “{args.id}”" if args.id else f"“{args.text}”"
    if not found:
        raise Fail(f"Nothing on the screen is named {what}. What's there: {texts(elements, 20) or 'no texts'}\n"
                   "Scroll (`swipe up`) if it's further down, or check the wording with `ui`.")
    n = getattr(args, "nth", 0) or 1
    if n > len(found):
        raise Fail(f"only {len(found)} elements are named {what}.")
    el = found[n - 1]
    if "off-screen" in el.get("flags", []):
        raise Fail(f"{what} is in the list but outside the visible screen at ({el['x']},{el['y']}): scroll to it first.")
    if len(found) > 1 and not getattr(args, "nth", 0):
        print(f"{len(found)} elements are named {what}; using the first, at ({el['x']},{el['y']}). --nth N picks another.")
    return el


def cmd_tap(args) -> int:
    dev = choose(args)
    if args.text or args.id:
        el = target_element(dev, args)
        x, y, what = el["x"], el["y"], f"{el['role']} “{el['label'] or el.get('value') or el.get('id')}” at ({el['x']},{el['y']})"
    elif len(args.xy) == 2:
        x, y = args.xy
        what = f"({x:g},{y:g})"
    else:
        raise Fail("tap what? Give X Y (pixels of the screenshot), or --text \"label\", or --id identifier.")
    dev.tap(x, y, 800 if args.long else 0)
    print(("Held " if args.long else "Tapped ") + what)
    after(dev, args)
    return 0


def cmd_swipe(args) -> int:
    dev = choose(args)
    w, h = dev.size()
    ways = {"up": (0.5, 0.72, 0.5, 0.28), "down": (0.5, 0.28, 0.5, 0.72), "left": (0.82, 0.5, 0.18, 0.5), "right": (0.18, 0.5, 0.82, 0.5)}
    if len(args.where) == 1 and args.where[0] in ways:
        a, b, c, d = ways[args.where[0]]
        points = (a * w, b * h, c * w, d * h)
        what = {"up": "up (the page scrolls down)", "down": "down (the page scrolls up)"}.get(args.where[0], args.where[0])
    elif len(args.where) == 4:
        try:
            points = tuple(float(v) for v in args.where)
        except ValueError:
            raise Fail("swipe takes a direction (up, down, left, right) or four numbers: X1 Y1 X2 Y2.")
        what = "from ({:g},{:g}) to ({:g},{:g})".format(*points)
    else:
        raise Fail("swipe takes a direction (up, down, left, right) or four numbers: X1 Y1 X2 Y2.")
    dev.swipe(*points, ms=args.ms)
    print(f"Swiped {what}")
    after(dev, args)
    return 0


def cmd_type(args) -> int:
    dev = choose(args)
    elements, _info = dev.elements()
    fields = [e for e in elements if dev.is_field(e) and "off-screen" not in e.get("flags", [])]
    if args.into or args.id:
        found = [e for e in find_elements(elements, args.into or "", args.id or "") if dev.is_field(e)]
        if not found and args.into:  # --into with the field's identifier works as well
            found = [e for e in find_elements(elements, ident=args.into) if dev.is_field(e)]
        if not found:
            raise Fail(f"No text field is named “{args.into or args.id}”. Fields on the screen: "
                       + (", ".join(json.dumps(e["label"] or e.get("hint") or e.get("id") or "?", ensure_ascii=False) for e in fields) or "none"))
        el = found[0]
    else:
        focused = [e for e in fields if "focused" in e.get("flags", [])]
        if focused:
            el = focused[0]
        elif len(fields) == 1:
            el = fields[0]
        elif not fields and dev.platform == "android":
            el = None  # a custom input the element list doesn't show: type into whatever has the focus
        elif not fields:
            raise Fail("There's no text field on the screen. Tap the place that opens one first, then type.")
        else:
            raise Fail("Several text fields are on the screen — say which with --into: "
                       + ", ".join(json.dumps(e["label"] or e.get("hint") or e.get("id") or "?", ensure_ascii=False) for e in fields))
    value = dev.set_text(el, args.text)
    name = (el["label"] or el.get("hint") or el.get("id") or el["role"]) if el else "the focused field"
    print(f"Typed into “{name}”: {json.dumps(value[:80], ensure_ascii=False)}"
          + ("" if dev.platform == "android" else "  (the field's whole text was replaced)"))
    after(dev, args)
    return 0


def cmd_key(args) -> int:
    dev = choose(args)
    print(f"Pressed {dev.key(args.name.lower())}")
    after(dev, args)
    return 0


def cmd_wait(args) -> int:
    dev = choose(args)
    deadline = time.time() + args.timeout
    seen: List[dict] = []
    unread = ""
    while True:
        try:
            seen, _info = dev.elements()
            unread = ""
        except Unsupported:
            raise
        except Fail as e:  # an animation, a screen still loading: no answer yet, in either direction
            unread = str(e).splitlines()[0]
        if not unread:
            found = [e for e in find_elements(seen, args.text) if "off-screen" not in e.get("flags", [])]
            if bool(found) != args.gone:
                print(f"“{args.text}” is gone." if args.gone else f"“{args.text}” is on the screen: {element_line(found[0], 1).strip()}")
                after(dev, args)
                return 0
        if time.time() >= deadline:
            break
        time.sleep(0.8)
    if unread:
        print(f"The screen couldn't be read for {args.timeout:g} s, so there's no telling: {unread}")
        return 1
    print(f"“{args.text}” " + ("is still on the screen" if args.gone else "didn't appear") + f" in {args.timeout:g} s. "
          f"On the screen now: {texts(seen, 20) or 'no texts'}")
    return 1


def seconds_of(text: str) -> int:
    m = re.fullmatch(r"(\d+)\s*([smh]?)", text.strip().lower())
    if not m:
        raise Fail("--last takes a time like 30s, 2m or 1h.")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def cmd_logs(args) -> int:
    dev = choose(args)
    lines = dev.logs(args.app or "", seconds_of(args.last), args.all)
    print(f"## Log of {args.app or 'the device'} on {dev.title()}\n")
    print("\n".join(ln[:300] for ln in lines) if lines else "Nothing: no errors and no crash in that time.")
    return 0


def cmd_set(args) -> int:
    dev = choose(args)
    if args.what in ("dark", "light"):
        dev.set_appearance(args.what)
    else:
        dev.set_text_size("big" if args.what == "big-text" else "normal")
    print(f"{dev.title()}: {args.what}")
    after(dev, args)
    return 0


# ----------------------------------------------------------------------------------------------------------- main

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mobile.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--ios", action="store_true", help="use the running iOS simulator")
    target.add_argument("--android", action="store_true", help="use the running Android emulator")
    target.add_argument("--device", default="", metavar="NAME", help="a device by name, UDID or adb serial")
    then = argparse.ArgumentParser(add_help=False)
    then.add_argument("--shot", action="store_true", help="take a screenshot after it")
    then.add_argument("--wait", type=float, default=1.0, metavar="S", help="seconds to wait before that screenshot")

    def add(name, fn, parents=(target,), **kw):
        p = sub.add_parser(name, parents=list(parents), **kw)
        p.set_defaults(fn=fn)
        return p

    add("devices", cmd_devices, parents=(), help="what can run here")
    add("project", cmd_project, parents=(), help="how to build the project").add_argument("dir", nargs="?", default="")
    p = add("boot", cmd_boot, parents=(), help="start a simulator or an emulator")
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--ios", action="store_true")
    p.add_argument("--android", action="store_true")
    p.add_argument("--show", action="store_true", help="with a window the user can watch")
    p.add_argument("--restart", action="store_true", help="restart a simulator that is already running (when it takes no taps)")
    p.add_argument("--project", default="", metavar="DIR", help="without a name: the device this project needs (default: here)")
    p = add("shutdown", cmd_shutdown, help="stop what this tool started")
    p.add_argument("--all", action="store_true", help="every running simulator and emulator")
    for name, fn, text in (("run", cmd_run, "install, launch, screenshot, errors"), ("install", cmd_install, "install a build")):
        p = add(name, fn, help=text)
        p.add_argument("path", nargs="?", default="", help=".app, .apk or a folder with one (default: the project's newest build)")
        p.add_argument("--project", default="", metavar="DIR", help="the project to take the newest build from (default: here)")
        p.add_argument("--grant", action="store_true", help="Android: grant every permission at install")
        if name == "run":
            p.add_argument("--arg", action="append", default=[], help="an argument for the app, repeatable: --arg=-uiTest")
            p.add_argument("--lang", default="", help="the app's language, e.g. ru or en")
            p.add_argument("--wait", type=float, default=3.0, metavar="S", help="seconds between launch and screenshot")
    p = add("launch", cmd_launch, parents=(target, then), help="start an installed app")
    p.add_argument("app", help="bundle id or package name")
    p.add_argument("--arg", action="append", default=[], help="an argument for the app, repeatable: --arg=-uiTest")
    p.add_argument("--lang", default="", help="the app's language, e.g. ru or en; `system` puts the device's back (Android keeps it)")
    p.add_argument("--keep", action="store_true", help="bring it forward without restarting it")
    add("stop", cmd_stop, help="stop an app").add_argument("app")
    add("uninstall", cmd_uninstall, help="remove an app and its data").add_argument("app")
    p = add("grant", cmd_grant, help="grant a permission without the dialog")
    p.add_argument("app")
    p.add_argument("permission", help="iOS: photos, location, contacts… Android: CAMERA, POST_NOTIFICATIONS…")
    add("open", cmd_open, parents=(target, then), help="open a link or a deep link").add_argument("url")
    p = add("shot", cmd_shot, help="a screenshot")
    p.add_argument("--out", default="", metavar="FILE")
    p.add_argument("--dark", action="store_true", help="also in the other appearance (and back)")
    p.add_argument("--big-text", action="store_true", help="also with large text (and back)")
    p = add("ui", cmd_ui, help="what is on the screen")
    p.add_argument("--find", default="", metavar="TEXT")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p = add("tap", cmd_tap, parents=(target, then), help="tap")
    p.add_argument("xy", nargs="*", type=float, metavar="X Y")
    p.add_argument("--text", default="", help="tap the element with this label")
    p.add_argument("--id", default="", help="tap the element with this identifier")
    p.add_argument("--nth", type=int, default=0, metavar="N", help="the N-th match")
    p.add_argument("--long", action="store_true", help="press and hold")
    p = add("swipe", cmd_swipe, parents=(target, then), help="swipe")
    p.add_argument("where", nargs="+", metavar="up|down|left|right | X1 Y1 X2 Y2")
    p.add_argument("--ms", type=int, default=300, help="how long the swipe takes")
    p = add("type", cmd_type, parents=(target, then), help="text into a field")
    p.add_argument("text")
    p.add_argument("--into", default="", metavar="LABEL", help="the field's label or hint")
    p.add_argument("--id", default="")
    add("key", cmd_key, parents=(target, then), help="back, home, enter…").add_argument("name")
    p = add("wait", cmd_wait, parents=(target, then), help="until a text appears")
    p.add_argument("text")
    p.add_argument("--gone", action="store_true", help="until it disappears")
    p.add_argument("--timeout", type=float, default=15.0)
    p = add("logs", cmd_logs, help="errors and crashes")
    p.add_argument("app", nargs="?", default="")
    p.add_argument("--last", default="2m")
    p.add_argument("--all", action="store_true", help="every level, not only errors")
    add("set", cmd_set, parents=(target, then), help="appearance or text size").add_argument(
        "what", choices=["dark", "light", "big-text", "normal-text"])
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)  # each step shows up as it happens, also through a pipe
    except (AttributeError, ValueError):
        pass
    if not getattr(args, "fn", None):
        ap.print_help()
        return 2
    try:
        return args.fn(args)
    except Unsupported as e:
        print(unsupported_hint(str(e)))
        return 3
    except Fail as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
