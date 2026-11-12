#!/usr/bin/env python3
"""Live check of /mobile on a real simulator or emulator: python3 tests/live_mobile.py [--ios] [--android] [--keep]

Not part of run_tests.py: it starts a device and takes a few minutes. It builds the tiny probe app from tests/probe
(a form with two fields, a button that greets and counts, a list on a second screen, a button that crashes),
installs it and goes through mobile.py the way an agent would — the element list, typing, tapping, waiting,
scrolling, back, a link, dark mode, logs, a crash — checking what comes back each time. The probe is removed
afterwards. A device this script started is shut down (unless --keep); one that was already running is left alone.
Needs: for iOS a Mac with Xcode; for Android the SDK (build-tools, a platform, an emulator image) and a JDK.
Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(os.path.dirname(HERE), "antigravity-plus")
MOBILE = os.path.join(PLUGIN, "skills", "mobile", "scripts", "mobile.py")
PROBE = os.path.join(HERE, "probe")
APP = "dev.antigravityplus.probe"
sys.path.insert(0, os.path.dirname(MOBILE))
import mobile  # noqa: E402

failed: List[str] = []


def check(name: str, good, detail: str = "") -> bool:
    print(f"{'✅' if good else '❌'} {name}" + (f"\n     {detail.strip()[:700]}" if not good and detail else ""), flush=True)
    if not good:
        failed.append(name)
    return bool(good)


def tool(*args: str, timeout: float = 420) -> Tuple[int, str]:
    """Run mobile.py the way an agent does: one command, its exit code and what it printed."""
    try:
        r = subprocess.run([sys.executable, MOBILE, *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"no answer in {timeout:.0f} s"
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def must(cmd: List[str], cwd: Optional[str] = None) -> None:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed: " + (r.stdout + r.stderr).decode("utf-8", "replace").strip()[-600:])


def build_ios(tmp: str) -> str:
    app = os.path.join(tmp, "Probe.app")
    os.makedirs(app)
    arch = "arm64" if platform.machine() == "arm64" else "x86_64"
    must(["xcrun", "-sdk", "iphonesimulator", "swiftc", "-target", f"{arch}-apple-ios17.0-simulator", "-parse-as-library", "-O",
          os.path.join(PROBE, "ios", "Probe.swift"), "-o", os.path.join(app, "Probe")])
    with open(os.path.join(app, "Info.plist"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>\n'
                f"<key>CFBundleIdentifier</key><string>{APP}</string>\n<key>CFBundleExecutable</key><string>Probe</string>\n"
                "<key>CFBundleName</key><string>Probe</string>\n<key>CFBundlePackageType</key><string>APPL</string>\n"
                "<key>CFBundleVersion</key><string>1</string>\n<key>CFBundleShortVersionString</key><string>1.0</string>\n"
                "<key>MinimumOSVersion</key><string>17.0</string>\n<key>LSRequiresIPhoneOS</key><true/>\n"
                "<key>UILaunchScreen</key><dict/>\n</dict></plist>\n")
    must(["codesign", "--force", "--sign", "-", app])
    return app


def newest_sub(folder: str) -> str:
    def version(name: str):
        return [int(n) for n in re.findall(r"\d+", name)]
    names = sorted((n for n in os.listdir(folder) if os.path.isdir(os.path.join(folder, n))), key=version)
    if not names:
        raise RuntimeError(f"nothing in {folder}")
    return os.path.join(folder, names[-1])


def build_android(tmp: str) -> str:
    sdk = mobile.android_sdk()
    if not sdk:
        raise RuntimeError("the Android SDK wasn't found")
    tools, jar = newest_sub(os.path.join(sdk, "build-tools")), os.path.join(newest_sub(os.path.join(sdk, "platforms")), "android.jar")

    def bt(name: str) -> str:
        found = next((os.path.join(tools, name + ext) for ext in ("", ".exe", ".bat") if os.path.isfile(os.path.join(tools, name + ext))), "")
        if not found:
            raise RuntimeError(f"{name} isn't in {tools}")
        return found

    javac = shutil.which("javac")
    if not javac:
        raise RuntimeError("javac isn't on PATH (a JDK is needed to build the Android probe)")
    src = os.path.join(PROBE, "android")
    obj, gen, classes, res = (os.path.join(tmp, n) for n in ("obj", "gen", "classes", "res"))
    for folder in (obj, gen, classes, os.path.join(res, "values")):
        os.makedirs(folder)
    shutil.copy(os.path.join(src, "ids.xml"), os.path.join(res, "values", "ids.xml"))
    must([bt("aapt2"), "compile", os.path.join(res, "values", "ids.xml"), "-o", obj])
    flats = [os.path.join(obj, n) for n in os.listdir(obj) if n.endswith(".flat")]
    base = os.path.join(obj, "base.apk")
    must([bt("aapt2"), "link", "-o", base, "--manifest", os.path.join(src, "AndroidManifest.xml"), "-I", jar, "--java", gen, *flats])
    sources = [os.path.join(src, n) for n in os.listdir(src) if n.endswith(".java")]
    must([javac, "-source", "17", "-target", "17", "-nowarn", "-cp", jar, "-d", classes,
          os.path.join(gen, *APP.split("."), "R.java"), *sources])
    compiled = [os.path.join(folder, n) for folder, _dirs, names in os.walk(classes) for n in names if n.endswith(".class")]
    must([bt("d8"), "--min-api", "26", "--lib", jar, "--output", obj, *compiled])
    with zipfile.ZipFile(base, "a") as z:
        z.write(os.path.join(obj, "classes.dex"), "classes.dex")
    aligned, apk = os.path.join(obj, "aligned.apk"), os.path.join(tmp, "probe-debug.apk")
    must([bt("zipalign"), "-f", "4", base, aligned])
    key = os.path.expanduser("~/.android/debug.keystore")
    if not os.path.isfile(key):  # what Android Studio creates on its first build: the same thing, for this run only
        key = os.path.join(tmp, "debug.keystore")
        must(["keytool", "-genkeypair", "-keystore", key, "-storepass", "android", "-keypass", "android", "-alias", "androiddebugkey",
              "-keyalg", "RSA", "-keysize", "2048", "-validity", "30", "-dname", "CN=Android Debug,O=Android,C=US"])
    must([bt("apksigner"), "sign", "--ks", key, "--ks-pass", "pass:android", "--out", apk, aligned])
    return apk


def scenario(kind: str, build: str, keep: bool) -> None:
    flag = "--" + kind
    name = "Тест" if kind == "ios" else "Test"  # adb types Latin only; the simulator takes any language
    running = [d for d in mobile.running(argparse.Namespace(ios=kind == "ios", android=kind == "android")) if d["kind"] != "device"]
    booted = False
    if running:
        print(f"Using the {running[0]['name']} that is already running (it will be left running).", flush=True)
    else:
        code, out = tool("boot", flag, timeout=600)
        booted = check("boot: a device starts", code == 0 and "Booted" in out, out)
        print("     " + "\n     ".join(out.strip().splitlines()[-3:])[:500], flush=True)
        if not booted:
            return
        if kind == "ios":
            check("…and takes touches (after a restart of its input service, if it came up without)", "touches arrive" in out, out)
    try:
        code, out = tool("run", build, flag)
        if not check("run: the build is installed and launched, a screenshot is taken, the screen's texts are listed",
                     code == 0 and "Launched" in out and "Screenshot: " in out and "Проба" in out, out):
            return
        shot = re.search(r"Screenshot: (\S+\.png)", out)
        check("…the screenshot is a real picture", bool(shot) and mobile.png_size(shot.group(1))[0] > 100, out)
        code, out = tool("ui", flag)
        check("ui: the fields and the buttons with their names", code == 0 and all(t in out for t in ("Имя", "Поздороваться", "Дальше", "Упасть", "Нажатий: 0")), out)
        code, out = tool("type", name, "--into", "Имя", flag)
        check("type: text goes into the field named by its hint", code == 0 and name in out, out)
        code, out = tool("tap", "--text", "Поздороваться", flag)
        code2, out2 = tool("wait", f"Привет, {name}", "--timeout", "10", flag)
        code3, out3 = tool("ui", "--find", "Нажатий", flag)
        check("tap by name, then wait: the app greets by the typed name and counts the tap",
              code == code2 == 0 and "Нажатий: 1" in out3, out + out2 + out3)
        tool("tap", "--id", "nameField", flag)
        time.sleep(1.5)
        code, out = tool("key", "enter", flag)
        check("key enter: pressed (on iOS: the on-screen keyboard's return key)", code == 0, out)
        code, out = tool("tap", "--text", "Дальше", flag)
        code2, out2 = tool("wait", "Строка 0", "--timeout", "10", flag)
        check("tap opens the second screen", code == code2 == 0, out + out2)
        code, out = tool("swipe", "up", flag)
        code2, out2 = tool("wait", "Строка 0", "--gone", "--timeout", "10", flag)
        check("swipe up scrolls the list: its first row is gone", code == code2 == 0, out + out2)
        code, out = tool("key", "back", flag)
        code2, out2 = tool("wait", "Поздороваться", "--timeout", "10", flag)
        check("key back returns to the first screen", code == code2 == 0, out + out2)
        if kind == "android":
            code, out = tool("open", "agpprobe://list", flag)
            code2, out2 = tool("wait", "Строка 0", "--timeout", "10", flag)
            check("open: a deep link lands on the list screen", code == code2 == 0, out + out2)
            tool("key", "back", flag)
            tool("wait", "Поздороваться", "--timeout", "10", flag)
        code, out = tool("shot", "--dark", flag)
        shots = re.findall(r": (\S+\.png)", out)
        check("shot --dark: two pictures, in the current appearance and in the other one",
              code == 0 and len(shots) == 2 and all(mobile.png_size(p)[0] > 100 for p in shots) and ("Dark mode" in out or "Light mode" in out), out)
        code, out = tool("logs", APP, "--last", "5m", flag)
        said = "probe error log 1" in out and (kind == "android" or f"greeted {name}" in out)  # print() is kept on iOS only
        check("logs: what the app said in this run is there, the system's chatter isn't a flood", code == 0 and said and len(out.splitlines()) < 60, out)
        tool("tap", "--text", "Упасть", flag)
        time.sleep(2.5)
        code, out = tool("logs", APP, "--last", "5m", flag)
        check("a crash: logs name it and say the app isn't running", code == 0 and "probe crash" in out and "running" in out, out)
    finally:
        code, out = tool("uninstall", APP, flag)
        check("uninstall: the probe is removed", code == 0, out)
        if booted and not keep:
            code, out = tool("shutdown")
            check("shutdown: the device this run started is stopped", code == 0 and "Shut down" in out, out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ios", action="store_true")
    ap.add_argument("--android", action="store_true")
    ap.add_argument("--keep", action="store_true", help="leave the device this run started running")
    ap.add_argument("--build-only", action="store_true", help="only build the probe apps")
    a = ap.parse_args()
    kinds = [k for k in ("ios", "android") if getattr(a, k)] or (["ios"] if mobile.xcrun() else []) + (["android"] if mobile.adb() else [])
    if not kinds:
        print("Neither Xcode's simulators nor the Android SDK is on this computer: nothing to check.")
        return 2
    free = shutil.disk_usage(os.path.expanduser("~")).free / 1024 ** 3
    if free < 2 and not a.build_only:
        print(f"Only {free:.1f} GB is free on the disk: a simulator or an emulator needs more. Free some space and run this again.")
        return 2
    tmp = tempfile.mkdtemp(prefix="agp-probe-")
    try:
        for kind in kinds:
            print(f"\n## {'iOS simulator' if kind == 'ios' else 'Android emulator'}", flush=True)
            try:
                build = build_ios(os.path.join(tmp, kind)) if kind == "ios" else build_android(os.path.join(tmp, kind))
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                check("the probe app builds", False, str(e))
                continue
            check(f"the probe app builds ({os.path.basename(build)}, id {mobile.app_id(build)})", mobile.app_id(build) == APP, build)
            if not a.build_only:
                scenario(kind, build, a.keep)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL OK' if not failed else 'FAILED: ' + '; '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
