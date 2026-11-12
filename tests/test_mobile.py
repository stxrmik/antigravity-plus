"""/mobile: devices, projects, elements, taps and text on a faked adb and simctl; the hooks around it."""
import contextlib, io, json, os, shutil, struct, subprocess, sys, zipfile
PLUGIN, SP = sys.argv[1], sys.argv[2]; HOOKS = os.path.join(PLUGIN, "hooks")
WIN = os.name == "nt"
# A fake Android SDK (adb only has to exist: every call to it is answered by fake_run below) and two emulators.
SDK = os.path.join(SP, "sdk"); AVD = os.path.join(SP, "avd")
os.makedirs(os.path.join(SDK, "platform-tools")); os.makedirs(os.path.join(SDK, "emulator"))
ADB = os.path.join(SDK, "platform-tools", "adb.exe" if WIN else "adb"); open(ADB, "w").close()
open(os.path.join(SDK, "emulator", "emulator.exe" if WIN else "emulator"), "w").close()
for name, cfg in (("pixel", "hw.device.name=pixel_7\nhw.lcd.width=1080\nhw.lcd.height=2400\nhw.lcd.density=420\ntag.id=google_apis\n"
                            "image.sysdir.1=system-images/android-36/google_apis/arm64-v8a/\n"),
                  ("big_tv", "hw.device.name = tv_1080p\nhw.lcd.width = 1920\nhw.lcd.height = 1080\nhw.lcd.density = 320\n"
                             "tag.id = google_apis\nimage.sysdir.1 = system-images/android-35/google_apis/arm64-v8a/\n"),
                  ("tab", "hw.device.name=pixel_tablet\nhw.lcd.width=2560\nhw.lcd.height=1600\nhw.lcd.density=320\ntag.id=google_apis\n")):
    os.makedirs(os.path.join(AVD, name + ".avd"))
    open(os.path.join(AVD, name + ".ini"), "w").write(f"avd.ini.encoding=UTF-8\npath={os.path.join(AVD, name + '.avd')}\n")
    open(os.path.join(AVD, name + ".avd", "config.ini"), "w").write(cfg)
os.environ.update(ANDROID_HOME=SDK, ANDROID_AVD_HOME=AVD)
os.environ.pop("ANDROID_SDK_ROOT", None)
sys.path.insert(0, HOOKS); sys.path.insert(0, os.path.join(PLUGIN, "skills", "mobile", "scripts"))
import mobile, compat
import conversation as conv

ok = True
def check(name, cond, detail=""):
    global ok; ok &= bool(cond); print(f"{'✅' if cond else '❌'} {name}" + (f" — {str(detail)[:600]}" if not cond and detail != "" else ""))
def mk(root, files):
    for rel, content in files.items():
        p = os.path.join(root, *rel.split("/")); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8").write(content)
def out_of(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = mobile.main(list(argv))
    return code, buf.getvalue()

# ── 1. small pieces ──
check("the SDK is found through ANDROID_HOME", mobile.android_sdk() == SDK and mobile.adb() == ADB, (mobile.android_sdk(), mobile.adb()))
proj = os.path.join(SP, "lp"); mk(proj, {"android/local.properties": "sdk.dir=" + SDK.replace("\\", "\\\\").replace(":", "\\:") + "\n"})
saved = os.environ.pop("ANDROID_HOME"); found = mobile.android_sdk(proj); os.environ["ANDROID_HOME"] = saved
check("…and through a project's local.properties (with Windows escapes)", found == SDK, found)
avds = {v["name"]: v for v in mobile.android_avds()}
check("emulators are read from their files", set(avds) == {"pixel", "big_tv", "tab"} and avds["pixel"]["api"] == "36", avds)
check("a TV emulator on a phone image is still a TV; a tablet is a tablet",
      avds["big_tv"]["form"] == "TV" and avds["tab"]["form"] == "tablet" and avds["pixel"]["form"] == "phone", avds)
check("android_text: spaces become %s; a real «%s» is split so it isn't read as a space",
      mobile.android_text("a b") == ["a%sb"] and mobile.android_text("50% off") == ["50%%soff"] and mobile.android_text("100%sure") == ["100%", "sure"])
check("seconds_of", mobile.seconds_of("30s") == 30 and mobile.seconds_of("2m") == 120 and mobile.seconds_of("1h") == 3600)
sq = mobile.squeeze(["11-10 10:00:01.000 E/App( 123): boom 0x1a2b", "11-10 10:00:02.000 E/App( 123): boom 0x3c4d", "11-10 10:00:03.000 W/App( 123): other"])
check("squeeze: a repeated message becomes one line with a count", len(sq) == 2 and sq[0].endswith("(×2)") and "×" not in sq[1], sq)
png = os.path.join(SP, "p.png"); open(png, "wb").write(b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", 1080, 2400) + b"\0" * 20)
check("png_size", mobile.png_size(png) == (1080, 2400) and mobile.png_size(os.path.join(SP, "missing.png")) == (0, 0))

def axml(package, utf8):
    strings = ["manifest", "package", package, "http://schemas.android.com/apk/res/android"]
    enc = (lambda s: bytes([len(s), len(s.encode())]) + s.encode() + b"\0") if utf8 else \
          (lambda s: struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\0\0")
    blobs = [enc(s) for s in strings]; offs = [sum(len(b) for b in blobs[:i]) for i in range(len(blobs))]
    data = b"".join(blobs); data += b"\0" * (-len(data) % 4)
    start = 28 + 4 * len(strings)
    pool = struct.pack("<HHIIIIII", 1, 28, start + len(data), len(strings), 0, 0x100 if utf8 else 0, start, 0) + struct.pack(f"<{len(strings)}I", *offs) + data
    attr = struct.pack("<IIIHBBI", 0xFFFFFFFF, 1, 2, 8, 0, 3, 2)
    elem = struct.pack("<HHIIIIIHHHHHH", 0x0102, 16, 36 + len(attr), 1, 0xFFFFFFFF, 0xFFFFFFFF, 0, 20, 20, 1, 0, 0, 0) + attr
    return struct.pack("<HHI", 3, 8, 8 + len(pool) + len(elem)) + pool + elem
for utf8 in (False, True):
    apk = os.path.join(SP, f"a{int(utf8)}.apk")
    with zipfile.ZipFile(apk, "w") as z: z.writestr("AndroidManifest.xml", axml("com.example.tv", utf8))
    check(f"apk_package reads a binary manifest ({'UTF-8' if utf8 else 'UTF-16'} strings)", mobile.apk_package(apk) == "com.example.tv", mobile.apk_package(apk))
check("apk_package: not an APK → empty, no crash", mobile.apk_package(png) == "" and mobile.apk_package(os.path.join(SP, "no.apk")) == "")

# ── 2. projects ──
ws = os.path.join(SP, "AniApp")
mk(ws, {"ios/App/Ani.xcodeproj/project.pbxproj": 'SDKROOT = iphoneos;\nIPHONEOS_DEPLOYMENT_TARGET = 17.0;\nPRODUCT_BUNDLE_IDENTIFIER = com.ani.app.widgets;\nPRODUCT_BUNDLE_IDENTIFIER = com.ani.app;\nPRODUCT_BUNDLE_IDENTIFIER = "com.ani.app.UITests";\n',
        "ios/App/Ani.xcodeproj/xcshareddata/xcschemes/Ani.xcscheme": "<Scheme/>", "ios/App/Ani.xcodeproj/xcshareddata/xcschemes/AniUITests.xcscheme": "<Scheme/>",
        "ios/App/Sources/Home.swift": "import SwiftUI\nstruct Home: View { var body: some View { Text(\"hi\") } }\n",
        "ios/App/Pods/Pods.xcodeproj/project.pbxproj": "",
        "android/settings.gradle.kts": 'rootProject.name = "Ani"\ninclude(":core")\ninclude(":mobile")\ninclude(":tv")\n',
        "android/core/build.gradle.kts": "plugins { alias(libs.plugins.android.library) }\n",
        "android/mobile/build.gradle.kts": 'plugins {\n    alias(libs.plugins.android.application)\n}\nandroid {\n    defaultConfig {\n        applicationId = "net.ani"\n    }\n'
                                           '    flavorDimensions += "store"\n    productFlavors {\n        create("site") {\n            dimension = "store"\n        }\n        create("play") {\n            applicationId = "net.ani.play"\n        }\n    }\n    buildTypes {\n        debug { }\n    }\n}\n',
        "android/mobile/src/main/AndroidManifest.xml": '<manifest><application><activity><intent-filter><category android:name="android.intent.category.LAUNCHER"/></intent-filter></activity></application></manifest>',
        "android/tv/build.gradle": "apply plugin: 'com.android.application'\nandroid {\n  defaultConfig {\n    applicationId 'net.ani.tv'\n  }\n}\n",
        "android/tv/src/main/AndroidManifest.xml": '<manifest><category android:name="android.intent.category.LEANBACK_LAUNCHER"/></manifest>'})
d = mobile.detect(ws)
check("detect: the iOS project, its scheme (not the tests) and the app's bundle id (not the extension's)",
      len(d["ios"]) == 1 and d["ios"][0]["scheme"] == "Ani" and d["ios"][0]["schemes"] == ["Ani"] and d["ios"][0]["bundle"] == "com.ani.app"
      and d["ios"][0]["flag"] == "-project", d["ios"])
mods = {p["module"]: p for p in d["android"]}
check("detect: Android app modules, not libraries (version-catalog alias and Groovy `apply plugin`)", set(mods) == {"mobile", "tv"}, d["android"])
check("detect: package names, flavors, the TV app", mods["mobile"]["package"] == "net.ani" and mods["mobile"]["flavors"] == ["site", "play"]
      and mods["tv"]["package"] == "net.ani.tv" and mods["tv"]["tv"] and not mods["mobile"]["tv"] and mods["tv"]["flavors"] == [], mods)
check("gradle_flavors: Groovy blocks", mobile.gradle_flavors("android {\n productFlavors {\n  free {\n   x 1\n  }\n  paid {\n  }\n }\n}") == ["free", "paid"])
fl = os.path.join(SP, "fl"); mk(fl, {"pubspec.yaml": "name: x\nflutter:\n  uses-material-design: true\n", "ios/Runner.xcodeproj/project.pbxproj": "", "android/settings.gradle": "include ':app'\n",
                                     "android/app/build.gradle": "apply plugin: 'com.android.application'\n"})
rn = os.path.join(SP, "rn"); mk(rn, {"package.json": '{"dependencies": {"expo": "~52.0.0", "react-native": "0.76.0"}}'})
mac = os.path.join(SP, "MacTool"); mk(mac, {"MacTool.xcodeproj/project.pbxproj": "SDKROOT = macosx;\nMACOSX_DEPLOYMENT_TARGET = 15.0;\nPRODUCT_BUNDLE_IDENTIFIER = com.me.mactool;\n",
                                            "Sources/Main.swift": "import SwiftUI\nstruct Main: View { var body: some View { Text(\"a\") } }\n"})
check("detect: an Xcode project for the Mac alone is not a mobile app", mobile.detect(mac)["ios"] == [] and mobile.session_lines(mac) == [], mobile.detect(mac))
check("detect: Flutter and Expo", mobile.detect(fl)["flutter"] == fl and mobile.detect(rn)["rn"] == "expo" and not mobile.detect(SDK)["ios"], (mobile.detect(fl), mobile.detect(rn)))
apk_dir = os.path.join(ws, "android", "mobile", "build", "outputs", "apk")
mk(ws, {"android/mobile/build/outputs/apk/site/debug/mobile-site-debug.apk": "old", "android/mobile/build/outputs/apk/site/release/mobile-site-release.apk": "rel"})
os.utime(os.path.join(apk_dir, "site", "debug", "mobile-site-debug.apk"), (1_700_000_000, 1_700_000_000))
built = mobile.built_artifact(ws, "android")
check("built_artifact: the newest debug APK, never a release one", built.endswith("mobile-site-debug.apk"), built)
mk(ws, {"android/mobile/build/outputs/apk/androidTest/site/debug/mobile-site-debug-androidTest.apk": "tests"})
products = os.path.join(SP, "products"); mk(products, {"Ani.app/Info.plist": "x", "AniUITests-Runner.app/Info.plist": "x"})
os.utime(os.path.join(products, "Ani.app"), (1_700_000_000, 1_700_000_000)); os.utime(os.path.join(products, "Ani.app", "Info.plist"), (1_700_000_000, 1_700_000_000))
check("…and never the tests' own APK or the UI tests' runner app, however new",
      mobile.built_artifact(ws, "android").endswith("mobile-site-debug.apk") and mobile.newest(products, ".app").endswith(os.sep + "Ani.app"),
      (mobile.built_artifact(ws, "android"), mobile.newest(products, ".app")))
note = mobile.stale_note(built, ws)
check("an app source changed after the build → a note that this may be the old code", "old code" in note and "after this build" in note and "android" in note, note)
fresh = os.path.join(SP, "fresh.apk"); open(fresh, "w").close(); os.utime(fresh, (4_000_000_000, 4_000_000_000))
check("…and none when the build is the newest thing", mobile.stale_note(fresh, ws) == "")
quiet = os.path.join(SP, "Quiet"); mk(quiet, {"settings.gradle": "include ':app'\n", "app/build.gradle": "apply plugin: 'com.android.application'\n", "app/src/main/java/A.kt": "class A\n"})
old_apk = os.path.join(quiet, "app.apk"); open(old_apk, "w").close()
for folder, _dirs, names in os.walk(quiet):
    for n in names: os.utime(os.path.join(folder, n), (1_700_000_000, 1_700_000_000))
os.utime(old_apk, (1_700_000_100, 1_700_000_100))
mk(quiet, {"app/src/test/java/ATest.kt": "class ATest\n", "app/src/androidTest/java/AUi.kt": "class AUi\n", "server/api.ts": "export {}\n", "README.md": "x\n", "app/src/main/assets/data.json": "{}\n"})
check("a test, the server next door or a README changed after the build: no note (a rebuild wouldn't change the app)", mobile.stale_note(old_apk, quiet) == "", mobile.stale_note(old_apk, quiet))
mk(quiet, {"app/src/main/res/layout/main.xml": "<LinearLayout/>\n"})
check("…a layout of the app changed: the note", "main.xml" in mobile.stale_note(old_apk, quiet), mobile.stale_note(old_apk, quiet))
code, text = out_of("project", ws)
check("project: build commands for both platforms and the per-module run commands",
      code == 0 and ":mobile:assembleSiteDebug" in text and ":tv:assembleDebug" in text and f'run "{apk_dir}"' in text and "TV app" in text
      and "`boot big_tv`" in text and "old code" in text
      and ("xcodebuild -project \"Ani.xcodeproj\" -scheme \"Ani\"" in text if mobile.xcrun() else "only on a Mac with Xcode" in text), text)
was_windows = compat.WINDOWS
compat.WINDOWS = False; unix_line = mobile.build_line("/p/My App", "./gradlew :app:assembleDebug -q")
compat.WINDOWS = True; win_line = mobile.build_line("C:\\p\\My App", ".\\gradlew.bat :app:assembleDebug -q"); compat.WINDOWS = was_windows
check("build commands fit the shell: sh with && and tail, PowerShell with ; and Select-Object",
      unix_line == 'cd "/p/My App" && ./gradlew :app:assembleDebug -q 2>&1 | tail -30'
      and win_line == 'cd "C:\\p\\My App"; .\\gradlew.bat :app:assembleDebug -q 2>&1 | Select-Object -Last 30', (unix_line, win_line))
lines = mobile.session_lines(ws)
check("session context: the app and the three commands (Android here; iOS only on a Mac with Xcode)",
      lines and "Android (:mobile, :tv)" in lines[0] and "the Android emulator" in lines[0] and any("run --project" in ln for ln in lines) and "BypassSandbox" in lines[0], lines)
check("session context: nothing for a project without a mobile app", mobile.session_lines(SDK) == [])

# ── 3. Android on a faked adb ──
UI = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0"><node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="net.ani" content-desc="" clickable="false" enabled="true" focused="false" scrollable="false" bounds="[0,0][1080,2400]">
<node text="Войти" resource-id="net.ani:id/login" class="android.widget.Button" package="net.ani" content-desc="" clickable="true" enabled="true" focused="false" bounds="[100,2000][980,2140]" />
<node text="Email" resource-id="net.ani:id/email" class="android.widget.EditText" package="net.ani" content-desc="" hint="Email" clickable="true" enabled="true" focused="true" bounds="[100,600][980,740]" />
<node text="qwerty" resource-id="net.ani:id/pass" class="android.widget.EditText" package="net.ani" content-desc="" password="true" clickable="true" enabled="true" focused="false" bounds="[100,800][980,940]" />
<node text="" resource-id="" class="android.widget.ImageView" package="net.ani" content-desc="Поиск" clickable="true" enabled="true" bounds="[900,100][1040,240]" />
<node text="Войти через Google" resource-id="" class="android.widget.TextView" package="net.ani" content-desc="" clickable="false" enabled="false" bounds="[100,2200][980,2300]" />
<node text="Ниже экрана" resource-id="" class="android.widget.TextView" package="net.ani" content-desc="" bounds="[0,2500][1080,2600]" />
<node text="" resource-id="" class="android.view.View" package="net.ani" content-desc="" bounds="[0,0][0,0]" />
<node text="" resource-id="" class="android.view.View" package="net.ani" content-desc="" bounds="[0,300][1080,400]" />
</node></hierarchy>UI hierchary dumped to: /dev/tty"""
CRASH = """--------- beginning of crash
11-10 10:04:10.000 E/AndroidRuntime( 111): FATAL EXCEPTION: main
11-10 10:04:10.000 E/AndroidRuntime( 111): Process: com.other, PID: 111
11-10 10:04:10.000 E/AndroidRuntime( 111): java.lang.IllegalStateException: not ours
11-10 10:04:30.000 E/AndroidRuntime( 222): FATAL EXCEPTION: main
11-10 10:04:30.000 E/AndroidRuntime( 222): Process: net.ani, PID: 222
11-10 10:04:30.000 E/AndroidRuntime( 222): java.lang.NullPointerException: title was null
11-10 10:04:30.000 E/AndroidRuntime( 222): 	at net.ani.HomeScreenKt.Home(HomeScreen.kt:42)
11-10 10:04:50.000 E/AndroidRuntime( 333): FATAL EXCEPTION: main
11-10 10:04:50.000 E/AndroidRuntime( 333): Process: net.ani.tv, PID: 333
11-10 10:04:50.000 E/AndroidRuntime( 333): java.lang.IllegalStateException: the TV app's own crash
"""
ACTIVITIES = {("net.ani", "LAUNCHER"): ["net.ani/.MainActivity"], ("net.ani.tv", "LEANBACK"): ["net.ani.tv/.TvActivity"],
              ("net.leaky", "LAUNCHER"): ["net.leaky/leakcanary.internal.activity.LeakLauncherActivity", "net.leaky/.MainActivity"],
              ("net.both", "LAUNCHER"): ["net.both/.PhoneActivity"], ("net.both", "LEANBACK"): ["net.both/.TvActivity"]}
calls, devices, state = [], ["emulator-5554 device product:sdk model:sdk_gphone64 transport_id:1"], {
    "pid": "222", "install": "Performing Streamed Install\nSuccess\n", "now": "11-10 10:05:00", "chars": "emulator", "locale": "", "ui": True}
def fake_run(cmd, timeout=30.0, data=None, binary=False, env=None):
    calls.append(list(cmd)); a = cmd[1:]; line = " ".join(a)
    def r(text="", code=0, err=""): return code, (text.encode() if binary else text), err
    if cmd[0] != ADB:
        return ios_run(cmd, binary, env)
    if a[:2] == ["devices", "-l"]: return r("List of devices attached\n" + "\n".join(devices) + "\n\n")
    if "emu avd name" in line: return r("pixel\nOK\n")
    if "exec-out screencap" in line: return r() if not binary else (0, open(png, "rb").read(), "")
    if "uiautomator dump" in line: return r(UI if state["ui"] else "ERROR: could not get idle state.\n")
    if line.endswith("date '+%m-%d %H:%M:%S'"): return r(state["now"] + "\n")
    if "ro.build.characteristics" in line: return r(state["chars"] + "\n")
    if "set-app-locales" in line: return r(state["locale"])
    if "dumpsys window displays" in line: return r("  Display: mDisplayId=0\n    init=1080x2400 420dpi cur=1080x2400 app=1080x2337 rng=1080x1017-2400x2337\n")
    if "wm size" in line: return r("Physical size: 1080x2400\n")
    if "query-activities" in line or "resolve-activity" in line:
        found = ACTIVITIES.get((line.split()[-1], "LEANBACK" if "LEANBACK_LAUNCHER" in line else "LAUNCHER"), [])
        if "query-activities" in line: return r("\n".join(found) + "\n" if found else "No activities found\n")
        if not found: return r("No activity found\n")  # asked to pick one of several, Android answers with its chooser screen
        return r("priority=0 preferredOrder=0 match=0x108000\n" + (found[0] if len(found) == 1 else "android/com.android.internal.app.ResolverActivity") + "\n")
    if "monkey -p com.oldphone" in line: return r("Events injected: 1\n## Network stats: elapsed time=20ms\n")
    if "am start" in line: return r("Starting: Intent { cmp=x }\nStatus: ok\nLaunchState: COLD\nTotalTime: 523\nWaitTime: 530\nComplete\n")
    if "pidof" in line: return r(state["pid"] + "\n" if state["pid"] else "")
    if "logcat -b crash" in line: return r(CRASH)
    if "logcat" in line: return r("--------- beginning of main\nW/Ani ( 222): slow frame\nE/Ani ( 222): request failed 500\nE/Ani ( 222): request failed 500\n")
    if a[2:3] == ["install"]: return r(state["install"])
    if "cmd uimode night" in line: return r("Night mode: " + state.setdefault("night", "auto") + "\n") if line.endswith("night") else (state.update(night=a[-1].split()[-1]) or r("Night mode: " + state["night"] + "\n"))
    if "pm list packages" in line: return r("")
    if "settings get system font_scale" in line: return r(state.setdefault("font", "null") + "\n")
    if "settings put system font_scale" in line: state["font"] = a[-1].split()[-1]; return r()
    return r()
def ios_run(cmd, binary, env): return 127, (b"" if binary else ""), "not found"
mobile.run = fake_run
def shell_lines(): return [c[-1] for c in calls if len(c) > 3 and c[3] == "shell"]

live = mobile.android_running()
check("android_running: the emulator with its AVD name", len(live) == 1 and live[0]["name"] == "pixel" and live[0]["kind"] == "emulator" and live[0]["running"], live)
code, text = out_of("devices")
check("devices: the running emulator, the other AVDs with their kind, the adb path hint", code == 0 and "▶ pixel — emulator emulator-5554" in text and "big_tv — TV, API 35" in text
      and "tab — tablet" in text and ("adb isn't on PATH" in text or shutil.which("adb")), text)
calls.clear(); code, text = out_of("ui")
check("ui: buttons, fields and labelled icons with pixel coordinates; unlabelled and empty views are left out",
      code == 0 and "Button  “Войти”  id=login  (540,2070)  [tap]" in text and "EditText  hint \"Email\"  id=email  (540,670)" in text and "focused" in text
      and "ImageView  “Поиск”" in text and "off-screen" in text and "6 elements" in text and "net.ani on pixel" in text, text)
check("ui: a password field shows that it holds text, a disabled label is marked", '= "qwerty"' in text and "[disabled]" in text, text)
code, text = out_of("ui", "--find", "войти")
check("ui --find: the exact match first, then the ones that contain it", code == 0 and text.index("“Войти”") < text.index("Войти через Google") and "Поиск" not in text, text)
calls.clear(); code, text = out_of("tap", "--text", "Войти")
check("tap --text: taps the centre of the element", code == 0 and "input tap 540 2070" in shell_lines() and "Tapped Button “Войти” at (540,2070)" in text, (text, shell_lines()))
calls.clear(); code, text = out_of("tap", "--id", "email", "--long")
check("tap --id --long: a press that stays down", code == 0 and "input swipe 540 670 540 670 800" in shell_lines() and text.startswith("Held"), (text, shell_lines()))
code, text = out_of("tap", "--text", "Нет такой")
check("tap --text with nothing by that name: says what is on the screen instead", code == 1 and "Nothing on the screen is named" in text and "Войти" in text, text)
code, text = out_of("tap", "--text", "Ниже экрана")
check("tap on an element outside the screen: scroll first", code == 1 and "scroll to it first" in text, text)
calls.clear(); code, text = out_of("tap", "300", "400", "--shot", "--wait", "0")
check("tap X Y --shot: the tap, then a screenshot", code == 0 and "input tap 300 400" in shell_lines() and "Screenshot: " in text and "1080×2400 px" in text, text)
calls.clear(); code, text = out_of("swipe", "up")
check("swipe up: from the lower part of the screen to the upper", code == 0 and "input swipe 540 1728 540 672 300" in shell_lines() and "scrolls down" in text, shell_lines())
calls.clear(); code, text = out_of("swipe", "10", "20", "30", "40", "--ms", "500")
check("swipe X1 Y1 X2 Y2 --ms", code == 0 and "input swipe 10 20 30 40 500" in shell_lines(), shell_lines())
check("swipe with nonsense: a usage hint, not a traceback", out_of("swipe", "sideways")[0] == 1)
calls.clear(); code, text = out_of("key", "back"); code2, text2 = out_of("key", "center"); code3, text3 = out_of("key", "warp")
check("key: back, the TV remote's centre, and an unknown key", "input keyevent 4" in shell_lines() and "input keyevent 23" in shell_lines() and code3 == 1 and "unknown key" in text3, (shell_lines(), text3))
calls.clear(); code, text = out_of("type", "hello world & more")
typed = [ln for ln in shell_lines() if ln.startswith("input text")]
check("type: into the focused field, with spaces and shell characters made safe", code == 0 and typed == ["input text 'hello%sworld%s&%smore'"] and "Typed into “Email”" in text, (typed, text))
check("…an empty field (its hint is what Android reports as its text) gets no backspaces", not any("keyevent" in ln for ln in shell_lines()), shell_lines())
calls.clear(); code, text = out_of("type", "x", "--into", "pass")
check("type --into a field that holds text: tap it, go to the end, backspace over the old text, type",
      code == 0 and [ln for ln in shell_lines() if "uiautomator" not in ln][:1] == ["input tap 540 870"]
      and "input keyevent 123 && input keyevent " + " ".join(["67"] * 8) in shell_lines()
      and shell_lines()[-1] == "input text x", shell_lines())
code, text = out_of("type", "привет")
check("type with non-Latin text: adb can't, and it says so instead of typing garbage", code == 1 and "only Latin" in text and not any("привет" in ln for ln in shell_lines()), text)
calls.clear(); code, text = out_of("launch", "net.ani.tv")
check("launch: a TV app is found through LEANBACK_LAUNCHER; the start time is reported",
      code == 0 and any("am start -S -W -n net.ani.tv/.TvActivity" in ln for ln in shell_lines()) and "started in 523 ms" in text, (shell_lines(), text))
code, text = out_of("launch", "com.missing")
check("launch of an app that isn't installed: a clear reason", code == 1 and "isn't installed" in text, text)
calls.clear(); code, text = out_of("launch", "com.oldphone")
check("launch on an old system image without `cmd package`: monkey finds the launcher activity", code == 0 and any(ln.startswith("monkey -p com.oldphone") for ln in shell_lines()), (text, shell_lines()))
calls.clear(); code, text = out_of("launch", "net.leaky")
check("launch of an app with two launcher icons (a debug build with LeakCanary): its own activity, never Android's chooser",
      code == 0 and any("am start -S -W -n net.leaky/.MainActivity" in ln for ln in shell_lines()) and not any("ResolverActivity" in ln and "am start" in ln for ln in shell_lines()), shell_lines())
calls.clear(); code, text = out_of("launch", "net.both")
state["chars"] = "tv,nosdcard"; calls2 = len(calls); code2, text2 = out_of("launch", "net.both"); state["chars"] = "emulator"
check("an app with a phone and a TV entry: the phone one on a phone, the TV one on a TV",
      "net.both/.PhoneActivity" in text and "net.both/.TvActivity" in text2, (text, text2))
calls.clear(); code, text = out_of("launch", "net.ani", "--lang", "ru", "--keep")
check("launch --lang --keep: the app's locale is set, the app isn't restarted — and it says the language stays", any("set-app-locales net.ani --locales ru" in ln for ln in shell_lines())
      and any(ln.startswith("am start -W -n net.ani/.MainActivity") for ln in shell_lines()) and "--lang system" in text, (shell_lines(), text))
calls.clear(); code, text = out_of("launch", "net.ani", "--lang", "system")
check("launch --lang system: the per-app language is cleared", code == 0 and "cmd locale set-app-locales net.ani" in shell_lines() and "the system's again" in text, (shell_lines(), text))
state["locale"] = "Unknown command: set-app-locales\n"; code, text = out_of("launch", "net.ani", "--lang", "en"); state["locale"] = ""
check("launch --lang on an Android without per-app languages: says it did nothing", code == 0 and "--lang did nothing" in text, text)
code, text = out_of("logs", "net.ani")
check("logs: asks logcat for this run of the app, by its process, without cutting to the last lines first", any("logcat -d -v brief --pid=222 '*:W'" == ln for ln in shell_lines()), shell_lines())
check("logs: this app's crash (not another app's, not net.ani.tv's), then its warnings and errors with repeats folded",
      code == 0 and "title was null" in text and "HomeScreen.kt:42" in text and "not ours" not in text and "TV app's own crash" not in text
      and "request failed 500  (×2)" in text, text)
state["now"] = "11-10 12:00:00"; code, text = out_of("logs", "net.ani", "--last", "2m"); state["now"] = "11-10 10:05:00"
check("logs: a crash from two hours ago isn't shown as what happened now", code == 0 and "title was null" not in text and "request failed 500" in text, text)
state["now"] = "03-01 00:00:20"; old_crash = CRASH; CRASH = CRASH.replace("11-10 10:04:30", "02-29 23:59:50"); code, text = out_of("logs", "net.ani"); CRASH = old_crash; state["now"] = "11-10 10:05:00"
check("…and one from half a minute ago is, across midnight and a month's end", "title was null" in text, text)
state["pid"] = ""; code, text = out_of("logs", "net.ani"); state["pid"] = "222"
check("logs of an app that isn't running: says so and points at the crash", "isn't running: see the crash above" in text, text)
calls.clear(); code, text = out_of("shot", "--dark")
check("shot --dark: the other appearance, then back to what it was (here: automatic)", code == 0 and "Dark mode: " in text and state["night"] == "auto"
      and shell_lines().count("cmd uimode night yes") == 1, (text, shell_lines()))
state["night"] = "custom_schedule"; code, text = out_of("shot", "--dark")
check("…and back to a schedule, when that is what the device was on", code == 0 and "Dark mode: " in text and state["night"] == "custom_schedule", (text, state["night"])); state["night"] = "auto"
calls.clear(); out_of("ui")
check("the element list is asked for through a file first (every device has that), in one call", [c[3:] for c in calls if "uiautomator" in " ".join(c)][:1]
      == [["shell", "uiautomator dump /data/local/tmp/agp-ui.xml >/dev/null 2>&1; cat /data/local/tmp/agp-ui.xml; rm -f /data/local/tmp/agp-ui.xml"]]
      and sum("uiautomator" in " ".join(c) for c in calls) == 1, [c[3:] for c in calls])
calls.clear(); code, text = out_of("shot", "--big-text")
check("shot --big-text: large text for one screenshot, then the normal size (the setting was never set: «null»)", code == 0 and "Large text: " in text and state["font"] == "1.0"
      and "settings put system font_scale 1.3" in shell_lines(), (text, shell_lines()))
code, text = out_of("set", "big-text"); code2, text2 = out_of("set", "dark")
check("set: text size and appearance stay as set", code == code2 == 0 and state["font"] == "1.3" and state["night"] == "yes", state)
out_of("set", "normal-text"); out_of("set", "light")
check("…and back", state["font"] == "1.0" and state["night"] == "no", state)
calls.clear(); code, text = out_of("open", "ani://title/42?tab=a&b=c")
check("open: a deep link, quoted for the device's shell", code == 0 and any(ln == "am start -W -a android.intent.action.VIEW -d 'ani://title/42?tab=a&b=c'" for ln in shell_lines()), shell_lines())
calls.clear(); code, text = out_of("run", apk_dir)
check("run <folder>: can't read a package from a file that isn't an APK", code == 1 and "couldn't read the app's id" in text, text)
real_apk = os.path.join(apk_dir, "site", "debug", "mobile-site-debug.apk")
with zipfile.ZipFile(real_apk, "w") as z: z.writestr("AndroidManifest.xml", axml("net.ani", True))
calls.clear(); code, text = out_of("run", "--project", ws, "--wait", "0")
check("run: installs the project's newest debug build, launches it, takes a screenshot, shows what's on screen and the errors",
      code == 0 and any(c[3:7] == ["install", "-r", "-t", "-d"] and c[-1] == real_apk for c in calls) and "Launched: net.ani/.MainActivity" in text
      and "Screenshot: " in text and "On screen: Войти" in text and "title was null" in text and "a launch without a look isn't a check" in text, text)
state["pid"] = ""; code, text = out_of("run", real_apk, "--wait", "0"); state["pid"] = "222"
check("run: an app that died on start is reported, with exit code 1", code == 1 and "NOT running" in text, text)
state["install"] = "adb: failed to install x.apk: Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]\n"
code, text = out_of("install", real_apk); state["install"] = "Performing Streamed Install\nSuccess\n"
check("install refused over a different signature: the reason and what to do", code == 1 and "UPDATE_INCOMPATIBLE" in text and "uninstall" in text, text)
code, text = out_of("wait", "Войти", "--timeout", "1")
code2, text2 = out_of("wait", "Не появится", "--timeout", "0.5")
check("wait: found → 0; not found in time → 1 with what is on the screen", code == 0 and "is on the screen" in text and code2 == 1 and "didn't appear" in text2 and "Войти" in text2, (text, text2))
real_sleep = mobile.time.sleep; mobile.time.sleep = lambda seconds: None; state["ui"] = False
code, text = out_of("wait", "Войти", "--gone", "--timeout", "0.3"); state["ui"] = True; mobile.time.sleep = real_sleep
check("wait --gone while the screen can't be read (an animation): that is not «gone»", code == 1 and "couldn't be read" in text and "is gone" not in text, text)
check("run with an .ipa: explains that the simulator needs a simulator build", "real iPhone" in out_of("run", os.path.join(SP, "x.ipa"))[1])
# which device
devices.append("R58M123ABC device usb:1 product:o1s model:SM_G991B transport_id:2")
calls.clear(); code, text = out_of("tap", "1", "2")
check("a phone on a cable is never picked by itself: the emulator is used", code == 0 and calls[-1][:3] == [ADB, "-s", "emulator-5554"], calls[-1])
calls.clear(); code, text = out_of("tap", "1", "2", "--device", "R58M123ABC")
check("…and is used when named", code == 0 and calls[-1][:3] == [ADB, "-s", "R58M123ABC"], calls[-1])
code, text = out_of("devices")
check("devices marks it as a real device", "REAL device" in text, text)
devices[:] = ["emulator-5554 device model:a", "emulator-5556 device model:b"]
code, text = out_of("tap", "1", "2")
check("two emulators running: asks which one instead of guessing", code == 1 and "Several are running" in text and "--device" in text, text)
devices[:] = ["emulator-5554 offline"]
code, text = out_of("tap", "1", "2")
check("nothing running: says how to start one", code == 1 and "boot" in text, text)
code, text = out_of("shutdown")
check("shutdown stops only what this tool started", code == 0 and "Nothing to shut down" in text, text)
devices[:] = ["emulator-5554 device model:a"]
mobile.remember("booted", "emulator-5554", {"pid": 999999999, "avd": "pixel"})
calls.clear(); code, text = out_of("shutdown")
check("…and not an emulator the user started later under the same serial", code == 0 and "Nothing to shut down" in text and not any("kill" in c for c in calls), (text, calls[-2:]))
mobile.remember("booted", "emulator-5554", {"pid": os.getpid(), "avd": "pixel"})
saved_processes = compat.processes; compat.processes = lambda name: [(os.getpid(), "emulator -avd pixel -no-window")] if name == "-avd pixel" else []
calls.clear(); code, text = out_of("shutdown"); compat.processes = saved_processes
check("…but the one it did start is stopped", code == 0 and "Shut down: pixel" in text and calls[-1][-2:] == ["emu", "kill"], (text, calls[-1]))
devices[:] = ["emulator-5554 device product:sdk model:sdk_gphone64 transport_id:1"]

# ── 4. iOS on a faked simctl and helper ──
SIMS = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
            {"udid": "AAAA-1", "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True, "lastUsedAt": "2026-11-10T10:00:00Z"},
            {"udid": "AAAA-2", "name": "iPhone 16", "state": "Shutdown", "isAvailable": True, "lastUsedAt": "2026-11-09T10:00:00Z"},
            {"udid": "AAAA-3", "name": "Gone", "state": "Shutdown", "isAvailable": False}],
        "com.apple.CoreSimulator.SimRuntime.tvOS-26-0": [{"udid": "TV-1", "name": "Apple TV", "state": "Booted", "isAvailable": True}]}}
TREE = {"role": "AXApplication", "label": "Проба", "frame": [0, 0, 402, 874], "pid": 77, "children": [
    {"role": "AXHeading", "label": "Проба", "frame": [16, 120, 100, 40]},
    {"role": "AXTextField", "value": "Имя", "id": "nameField", "frame": [16, 180, 370, 44]},
    {"role": "AXSecureTextField", "value": "", "placeholder": "Пароль", "frame": [16, 232, 370, 44], "focused": True},
    {"role": "AXButton", "label": "Назад", "id": "BackButton", "frame": [8, 60, 60, 44]},
    {"role": "AXGroup", "frame": [0, 0, 402, 874], "children": [{"role": "AXButton", "label": "Поздороваться", "frame": [16, 300, 370, 44]}]},
    {"role": "AXStaticText", "label": "Строка 99", "frame": [16, 900, 370, 44]}]}
KEYBOARD = [{"role": "AXButton", "label": ch, "frame": [4 + 39 * i, 600, 35, 42]} for i, ch in enumerate("qwertyuiop")] + \
           [{"role": "AXButton", "label": ch, "frame": [24 + 39 * i, 654, 35, 42]} for i, ch in enumerate("asdfghjkl")] + \
           [{"role": "AXButton", "label": "return", "id": "Return", "frame": [300, 760, 96, 42]},
            {"role": "AXButton", "label": "Next keyboard", "frame": [20, 820, 44, 44]}]
SEARCH = {"role": "AXApplication", "label": " ", "frame": [0, 0, 402, 874], "children": [{"role": "AXTextField", "id": "SpotlightSearchField", "frame": [20, 500, 362, 44]}]}
ios = {"keyboard": False, "search": False, "dead": False, "curable": False, "booted": [], "stopped": [], "kicks": 0}
def ios_run(cmd, binary, env):
    a = cmd[1:]
    if cmd[0] == "xcode-select": return 0, "/Applications/Xcode.app/Contents/Developer\n", ""
    if cmd[0] == "/fake/simbridge":
        if a[2] == "swipe" and not ios["dead"]: ios["search"] = a[4] == "0.35000"  # pulling the home screen down opens the search
        if a[2] == "ui" and ios["search"]: return 0, json.dumps(SEARCH), ""
        if a[2] == "ui" and ios["keyboard"]: return 0, json.dumps(dict(TREE, children=TREE["children"] + KEYBOARD)), ""
        if a[2] == "ui": return 0, json.dumps(TREE), ""
        if a[2] == "settext": return 0, json.dumps({"value": a[5], "role": "AXTextField"}), ""
        if a[2] == "tap" and a[3] == "0.99999": return 3, "", "simbridge: this Xcode builds touch events in a format this helper doesn't know (inner size 0x90)\n"
        return 0, "", ""
    if a[:1] != ["simctl"]: return 127, "", "not found"
    if a[1:4] == ["list", "devices", "-j"]: return 0, json.dumps(SIMS), ""
    if a[1] == "bootstatus": ios["booted"].append(a[2]); return 0, "Finished\n", ""
    if a[1] == "shutdown": ios["stopped"].append(a[2]); return 0, "", ""
    if a[1] == "spawn" and "kickstart" in a:  # the input service is restarted: that cures a curable simulator
        ios["kicks"] += 1
        if ios["curable"]: ios["dead"] = False
        return 0, "", ""
    if a[1] == "io" and "screenshot" in a:  # a new picture every time: 700 px wide, one pixel taller than the last
        ios["shots"] = ios.get("shots", 0) + 1
        with open(a[-1], "wb") as f: f.write(b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", 700, 1500 + ios["shots"]) + b"\0" * 20)
        return 0, "", ""
    if a[1] == "launch": return 0, f"{a[-1] if a[-1].count('.') else a[-3]}: 4242\n", ""
    if a[1:3] == ["spawn", "AAAA-1"] and "launchctl" in a: return 0, "PID\tStatus\tLabel\n4242\t0\tUIKitApplication:com.ani.app[1a2b][rb-legacy]\n-\t0\tUIKitApplication:com.dead.app[3c4d][rb-legacy]\n", ""
    return 0, "", ""
real_xcrun = mobile.xcrun
def mobile_xcrun_real(): return bool(real_xcrun())
mobile.xcrun = lambda: "/usr/bin/xcrun"; mobile.bridge_bin = lambda: "/fake/simbridge"
sims = mobile.ios_devices()
check("ios_devices: iOS only, available only, the running one first", [d["name"] for d in sims] == ["iPhone 17 Pro", "iPhone 16"] and sims[0]["os"] == "iOS 26.5" and sims[0]["running"], sims)
mobile.remember("screens", "AAAA-1", [736, 1600])
calls.clear(); code, text = out_of("ui", "--ios")
check("ui (iOS): points become pixels of the 736×1600 screenshot; fields show their hint and focus",
      code == 0 and "Проба on iPhone 17 Pro" in text and "Button  “Поздороваться”  (368,589)" in text and 'SecureTextField  hint "Пароль"  (368,465)  [focused]' in text
      and "TextField  = \"Имя\"  id=nameField" in text and "off-screen" in text, text)
calls.clear(); code, text = out_of("tap", "--text", "Поздороваться", "--ios")
check("tap (iOS): the helper gets the point as a share of the screen", code == 0 and calls[-1][3:] == ["tap", "0.50000", "0.36812", "0"], calls[-1])
calls.clear(); code, text = out_of("swipe", "3", "800", "600", "800", "--ios")
check("swipe from the very edge (iOS): sent as the system's edge gesture", code == 0 and calls[-1][3] == "swipe" and calls[-1][-1] == "1", calls[-1])
calls.clear(); code, text = out_of("swipe", "up", "--ios")
check("swipe up in the middle (iOS): an ordinary drag", calls[-1][3] == "swipe" and calls[-1][-1] == "0" and calls[-1][4:8] == ["0.50000", "0.72000", "0.50000", "0.28000"], calls[-1])
calls.clear(); code, text = out_of("type", "Миша", "--into", "nameField", "--ios")
code2, text2 = out_of("type", "Миша", "--id", "nameField", "--ios")
check("type (iOS): any language, into the field found by its id (--id or --into), at its place in points",
      code == code2 == 0 and calls[-1][3:] == ["settext", "201.0", "202.0", "Миша"] and "whole text was replaced" in text2, (text, text2, calls[-1]))
calls.clear(); code, text = out_of("type", "тайна", "--ios")
check("type (iOS) without --into: the focused field", code == 0 and calls[-1][3:6] == ["settext", "201.0", "254.0"], calls[-1])
calls.clear(); code, text = out_of("key", "back", "--ios")
check("key back (iOS): the navigation bar's back button when there is one", code == 0 and "button of the navigation bar" in text and calls[-1][3] == "tap", (text, calls[-1]))
TREE["children"][3]["id"] = ""; calls.clear(); code, text = out_of("key", "back", "--ios"); TREE["children"][3]["id"] = "BackButton"
check("…otherwise a slow swipe from the left edge", code == 0 and calls[-1][3:] == ["swipe", "0.003", "0.5", "0.8", "0.5", "600", "1"], calls[-1])
calls.clear(); code, text = out_of("key", "home", "--ios")
check("key home (iOS): the home screen is brought to the front", code == 0 and calls[-1][1:3] == ["simctl", "launch"] and calls[-1][-1] == "com.apple.springboard", calls[-1])
code, text = out_of("key", "enter", "--ios")
check("key enter (iOS) with no keyboard on the screen: says to tap a field first", code == 1 and "no on-screen keyboard" in text, text)
code, text = out_of("key", "tab", "--ios")
check("a key the simulator has no way to press: what to use instead", code == 1 and "takes no “tab” key" in text, text)
ios["keyboard"] = True
calls.clear(); code, text = out_of("key", "enter", "--ios")
check("key enter (iOS) with the keyboard up: its return key is tapped", code == 0 and "return” key of the on-screen keyboard" in text
      and calls[-1][3] == "tap" and abs(float(calls[-1][4]) - 348 / 402) < 0.002 and abs(float(calls[-1][5]) - 781 / 874) < 0.002, (text, calls[-1]))
code, text = out_of("ui", "--ios")
check("ui (iOS) with the keyboard up: its keys stay out of the list, with a note where it starts",
      code == 0 and "on-screen keyboard is up" in text and "below y=1099" in text and "“q”" not in text and "Next keyboard" not in text
      and "Поздороваться" in text and "6 elements" in text, text)
code, text = out_of("ui", "--all", "--ios")
check("ui --all shows the keys too", "“q”" in text and "id=Return" in text, text)
ios["keyboard"] = False
mobile.time.sleep = lambda seconds: None  # the waits for the simulator to settle: nothing to wait for here
mobile.remember("screens", "AAAA-2", [736, 1600])
calls.clear(); code, text = out_of("boot", "iPhone 16")
check("boot (iOS): boots, then makes sure touches arrive by opening and closing the system search",
      code == 0 and "Booted: iPhone 16" in text and "touches arrive" in text and ios["booted"] == ["AAAA-2"] and not ios["search"], (text, ios))
ios.update(dead=True, curable=True, booted=[], kicks=0)
code, text = out_of("boot", "iPhone 16")
check("boot (iOS) of a simulator that came up taking no touches: its input service is restarted — no second boot — and touches arrive",
      code == 0 and "restarting that service" in text and "touches arrive" in text and ios["booted"] == ["AAAA-2"] and ios["kicks"] >= 1 and not ios["dead"], (text, ios))
ios.update(dead=True, curable=False, booted=[], stopped=[], kicks=0)
code, text = out_of("boot", "iPhone 16"); ios["dead"] = False
check("…when that doesn't help: one full restart, then an honest word about what works and what doesn't",
      code == 0 and ios["booted"] == ["AAAA-2", "AAAA-2"] and ios["stopped"] == ["AAAA-2"] and "touches still don't arrive" in text and "touches arrive" not in text.replace("still don't arrive", ""), (text, ios))
ios.update(booted=[], stopped=[], kicks=0)
code, text = out_of("boot", "iPhone 17 Pro", "--restart")
check("boot --restart of a running simulator that takes touches: nothing is restarted, and it says where to look instead",
      code == 0 and "nothing to restart" in text and ios["booted"] == [] and ios["stopped"] == [] and ios["kicks"] == 0, (text, ios))
ios.update(dead=True, curable=True)
code, text = out_of("boot", "iPhone 17 Pro", "--restart")
check("boot --restart of a running simulator that takes none: the input service alone is restarted, the simulator stays up",
      code == 0 and "restarting its input service" in text and "touches arrive" in text and ios["stopped"] == [] and ios["kicks"] >= 1, (text, ios))
ios.update(dead=False, curable=False)
code, text = out_of("boot", "iPhone 17 Pro")
check("boot of a simulator that already runs: left alone", code == 0 and "Already running" in text, text)
same = os.path.join(SP, "same.png")
code, text = out_of("shot", "--ios", "--out", same); one = mobile.png_size(same)
code2, text2 = out_of("shot", "--ios", "--out", same); two = mobile.png_size(same)
check("two screenshots into one file: the second call shows the second picture, not the first again",
      code == code2 == 0 and one[1] + 1 == two[1] and f"{two[0]}×{two[1]} px" in text2 and not os.path.exists(same + ".full.png"), (one, two, text2))
before = set(os.listdir(os.getcwd()))
code, text = out_of("shot", "--ios", "--out", "~/agp-shots/home.png")
check("--out with ~: the picture lands in the home folder, and no folder called «~» appears here",
      code == 0 and os.path.isfile(os.path.join(os.path.expanduser("~"), "agp-shots", "home.png")) and set(os.listdir(os.getcwd())) == before, text)
# boot without a name: the device the project needs
started = []
real_boot = mobile.boot_emulator; mobile.boot_emulator = lambda avd, show: started.append(avd["name"]) or 0
droid_only = os.path.join(SP, "DroidOnly"); mk(droid_only, {"settings.gradle": "include ':app'\n", "app/build.gradle": "apply plugin: 'com.android.application'\n"})
tv_only = os.path.join(SP, "TvOnly"); mk(tv_only, {"settings.gradle": "include ':tv'\n", "tv/build.gradle": "apply plugin: 'com.android.application'\n",
                                                   "tv/src/main/AndroidManifest.xml": '<category android:name="android.intent.category.LEANBACK_LAUNCHER"/>'})
devices[:] = []
code, text = out_of("boot", "--project", droid_only); code2, text2 = out_of("boot", "--project", tv_only)
check("boot without a name in an Android project: an emulator — a phone for a phone app, the TV one for a TV app — though simulators exist",
      code == code2 == 0 and started == ["pixel", "big_tv"], (started, text, text2))
started.clear(); code, text = out_of("boot", "--project", ws)
check("…in a project with an iOS app too: the simulator", code == 0 and started == [] and "iPhone 17 Pro" in text, (started, text))
started.clear(); code, text = out_of("boot", "--android", "--project", ws)
check("…and --android there: the emulator", code == 0 and started == ["pixel"], (started, text))
code, text = out_of("run", real_apk, "--wait", "0")
check("run of an APK with no emulator running: says to start one with `boot --android`", code == 1 and "No Android emulator is running" in text and "boot --android" in text, text)
mobile.boot_emulator = real_boot; devices[:] = ["emulator-5554 device product:sdk model:sdk_gphone64 transport_id:1"]
# the adb server is started once, with nothing attached to it
spawned = []
real_subprocess_run = mobile.subprocess.run
mobile.subprocess.run = lambda cmd, **kw: spawned.append((cmd, kw)) or None
mobile._adb_started = False; mobile.need_adb(); mobile.need_adb(); mobile.subprocess.run = real_subprocess_run
check("the adb server is started once per run, with no pipes for it to keep open",
      len(spawned) == 1 and spawned[0][0] == [ADB, "start-server"] and spawned[0][1].get("stdout") == mobile.subprocess.DEVNULL
      and spawned[0][1].get("stderr") == mobile.subprocess.DEVNULL and spawned[0][1].get("stdin") == mobile.subprocess.DEVNULL, spawned)
first = mobile.shot_path(s_dev := mobile.Sim(sims[0])) if sys.version_info >= (3, 8) else ""
open(first, "w").close()
check("two screenshots in one second get different names", mobile.shot_path(s_dev) != first and mobile.shot_path(s_dev).endswith("-2.png"), (first, mobile.shot_path(s_dev)))
calls.clear(); code, text = out_of("launch", "com.ani.app", "--lang", "en", "--arg=-uiTest", "--ios")
launch = calls[-1]
check("launch (iOS): restarts the app, keeps its output, passes arguments and the language",
      code == 0 and "pid 4242" in text and "--terminate-running-process" in launch and any(x.startswith("--stdout=") for x in launch)
      and launch[-6:] == ["com.ani.app", "-uiTest", "-AppleLanguages", "(en)", "-AppleLocale", "en"], launch)
s = mobile.Sim(sims[0])
check("alive (iOS): running, dead, never started", s.alive("com.ani.app") is True and s.alive("com.dead.app") is False and s.alive("com.none") is False)
mobile.remember("screens", "AAAA-1", [736, 1600])
code, text = out_of("tap", "735.99", "100", "--ios")
check("a helper that doesn't know this Xcode: exit 3 and what still works", code == 3 and "isn't supported" not in text and "Screenshots, launching, links and logs still work" in text and "deep links" in text, text)
code, text = out_of("tap", "1", "2")
check("an emulator and a simulator both running: asks which", code == 1 and "--ios, --android" in text, text)
noise = ["objc[1]: Class X is implemented in both /a and /b", "2026-11-10 10:00:00.000 E  App[1:2] [com.apple.UIKit:UIKBFeedbackGenerator] haptic",
         "2026-11-10 10:00:00.000 E  App[1:2] (CoreHaptics) no pattern", "2026-11-10 10:00:00.000 E  App[1:2] [net.ani:ui] my own error",
         "2026-11-10 10:00:00.000 E  App[1:2] [com.apple.UIKit:LayoutConstraints] Unable to simultaneously satisfy constraints",
         "2026-11-10 10:00:00.000 F  App[1:2] (libswiftCore.dylib) Fatal error: boom"]
kept = [ln for ln in noise if not mobile.IOS_NOISE.search(ln) and (not mobile.IOS_SYSTEM.search(ln) or mobile.IOS_USEFUL.search(ln))]
check("iOS log filter: the app's own lines, layout problems and fatal errors stay; system chatter goes", [n[-12:] for n in kept] == ["my own error", " constraints", " error: boom"], kept)

# ── 5. the guard ──
import guard
def rule(c):
    r = guard.analyze_command(c); return r[0] if r else None
check("guard: erasing or deleting a simulator, wiping an emulator stop for the user",
      rule("xcrun simctl erase all") == rule("xcrun simctl delete AAAA-1") == rule("emulator -avd pixel -wipe-data") == "simulator-wipe")
check("guard: clean-up of unavailable simulators and ordinary simctl/adb commands pass",
      not any(rule(c) for c in ("xcrun simctl delete unavailable", "xcrun simctl boot AAAA-1", "adb -s emulator-5554 uninstall net.ani", "adb devices -l",
                                "adb -s R58M123ABC shell input tap 1 2", "adb -s 127.0.0.1:6555 shell pm clear net.ani")))
check("guard: changing a real phone stops for the user", rule("adb -s R58M123ABC uninstall net.ani") == rule("adb -d install app.apk") == rule("adb -s R58M shell pm clear net.ani") == "real-device")

# ── 6. the Stop hook: changed screens need a look; a claim needs a run ──
cid = "agp-mobiletest-0001"; hws = os.path.join(SP, "mb_ws"); art = os.path.join(SP, "mb_art"); tp = os.path.join(art, "transcript_full.jsonl")
SCREENS = {"settings.gradle": "include ':app'\n", "app/build.gradle": "apply plugin: 'com.android.application'\n",
           "app/src/main/java/Home.kt": "import androidx.compose.runtime.Composable\n@Composable fun Home() {}\n",
           "app/src/main/java/Repo.kt": "class Repo { fun load() = 1 }\n",
           "app/src/main/res/layout/main.xml": "<LinearLayout/>\n", "app/src/main/res/values/colors.xml": "<resources/>\n",
           "ios/App.xcodeproj/project.pbxproj": "IPHONEOS_DEPLOYMENT_TARGET = 17.0;\n",
           "ios/Home.swift": "import SwiftUI\nstruct Home: View { var body: some View { Text(\"a\") } }\n", "ios/Model.swift": "import Foundation\nstruct M {}\n",
           "rn/package.json": '{"dependencies": {"react-native": "0.76.0"}}',
           "rn/Screen.tsx": "import { View } from 'react-native'\nexport const S = () => <View/>\n", "web/Page.tsx": "export const P = () => <div/>\n"}
MAC_APP = {"MacTool.xcodeproj/project.pbxproj": "SDKROOT = macosx;\nMACOSX_DEPLOYMENT_TARGET = 15.0;\n",
           "Sources/Main.swift": "import SwiftUI\nstruct Main: View { var body: some View { Text(\"a\") } }\n"}
DESKTOP = {"build.gradle.kts": "plugins { kotlin(\"jvm\") }\n", "src/main/kotlin/Main.kt": "import androidx.compose.runtime.Composable\n@Composable fun App() {}\n"}
def setup(steps, final="Готово.", files=SCREENS):
    """A conversation: the user's request, the agent's tool calls with their results, its last words. A command is
    ("run_command", line, outcome): an exit code, or "bg" for one that went on in the background — ("done", n, code)
    is then the message that the n-th such command has finished."""
    for dd in (hws, art): shutil.rmtree(dd, ignore_errors=True); os.makedirs(dd)
    mk(hws, dict(files, **{"AGENTS.md": "# Test project\n"}))
    subprocess.run(["git", "init", "-q"], cwd=hws)
    for f in os.listdir(conv.STATE_DIR):
        if f.startswith(cid): os.remove(os.path.join(conv.STATE_DIR, f))
    rows = [{"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-10T10:00:00Z", "content": "<USER_REQUEST>Поменяй экран</USER_REQUEST>"}]
    tasks = []
    for step in steps:
        if step[0] == "done":
            rows.append({"step_index": len(rows), "type": "SYSTEM_MESSAGE", "content": f'[Message] timestamp=x sender={tasks[step[1]]} priority=1\n'
                         f'Task id "{tasks[step[1]]}" finished with result:\nCommand exited with code {step[2]}.\nCommand output:\nok\n'})
            continue
        name, args, outcome = step[0], step[1], (step[2] if len(step) > 2 else 0)
        rows.append({"step_index": len(rows), "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": name, "args": args}]})
        if name == "run_command" and outcome == "bg":
            tasks.append(f"{cid}/task-{len(rows)}")
            rows.append({"step_index": len(rows), "type": "GENERIC", "content": f"Created At: x\nTool is running as a background task with task id: {tasks[-1]}\nTask Description: x\n"})
        elif name == "run_command":
            rows.append({"step_index": len(rows), "type": "GENERIC", "content": f"Created At: x\nCommand exited with code {outcome}.\nCommand output:\nok\n"})
        else:
            rows.append({"step_index": len(rows), "type": "GENERIC", "content": "The file was written.\n"})
    rows.append({"step_index": len(rows), "type": "PLANNER_RESPONSE", "content": final})
    open(tp, "w", encoding="utf-8").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
def w(n): return ("write_file", {"Path": os.path.join(hws, *n.split("/")), "Content": "x"})
def cmd(c, outcome=0): return ("run_command", {"CommandLine": c}, outcome)
def stop():
    p = {"conversationId": cid, "transcriptPath": tp, "artifactDirectoryPath": art, "workspacePaths": [hws], "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "executionNum": 0}
    r = subprocess.run([sys.executable, os.path.join(HOOKS, "stop.py")], input=json.dumps(p), capture_output=True, text=True, encoding="utf-8")
    if r.stderr.strip(): print("   stderr:", r.stderr.strip()[:300])
    dd = json.loads(r.stdout or "{}"); return dd.get("reason") if dd.get("decision") == "continue" else None
import stop as stophook
mk(hws, SCREENS)
kinds = {n: stophook.mobile_ui(os.path.join(hws, *n.split("/"))) for n in SCREENS if n.endswith((".kt", ".xml", ".swift", ".tsx"))}
check("what counts as a mobile screen: Compose, SwiftUI, React Native, layouts and resources — not models, not a web page",
      kinds == {"app/src/main/java/Home.kt": "code", "app/src/main/java/Repo.kt": "", "app/src/main/res/layout/main.xml": "layout", "app/src/main/res/values/colors.xml": "layout",
                "ios/Home.swift": "code", "ios/Model.swift": "", "rn/Screen.tsx": "code", "web/Page.tsx": ""}, kinds)
TOOL = f'python3 "{os.path.join(PLUGIN, "skills", "mobile", "scripts", "mobile.py")}"'
RUN, SHOT = f'{TOOL} run --project "{hws}"', f"{TOOL} shot"
BUILD, INSTALL = "./gradlew :app:assembleDebug -q", "adb -s emulator-5554 install -r app/build/outputs/apk/debug/app-debug.apk"
HOME_KT, LAYOUT = "app/src/main/java/Home.kt", "app/src/main/res/layout/main.xml"
cases = [
    ("a Compose screen changed, nothing run", [w(HOME_KT)], "screens"),
    ("…a build alone shows nothing", [w(HOME_KT), cmd(BUILD)], "screens"),
    ("…unit tests don't show a screen either", [w(HOME_KT), cmd("./gradlew :app:testDebugUnitTest")], "screens"),
    ("…build, then mobile.py run", [w(HOME_KT), cmd(BUILD), cmd(RUN)], None),
    ("…a run that failed (no build found) showed nothing", [w(HOME_KT), cmd(RUN, 1)], "screens"),
    ("…`run --help` showed nothing", [w(HOME_KT), cmd(RUN + " --help")], "screens"),
    ("…a screenshot of what was installed before the change shows the old screen", [w(HOME_KT), cmd(BUILD), cmd(SHOT)], "screens"),
    ("…install the new build, then a raw adb screenshot", [w(HOME_KT), cmd(BUILD), cmd(INSTALL), cmd("adb exec-out screencap -p > /tmp/a.png")], None),
    ("…a run that went on in the background and ended well", [w(HOME_KT), cmd(RUN, "bg"), ("done", 0, 0)], None),
    ("…while it is still going the hook keeps quiet: it says its word when the command has reported back", [w(HOME_KT), cmd(RUN, "bg")], None),
    ("…one that ended badly", [w(HOME_KT), cmd(RUN, "bg"), ("done", 0, 1)], "screens"),
    ("…a change made while it was going isn't in what it showed", [w(HOME_KT), cmd(RUN, "bg"), w(LAYOUT), ("done", 0, 0)], "screens"),
    ("…UI tests cover the code", [w(HOME_KT), cmd("./gradlew :app:connectedDebugAndroidTest")], None),
    ("…but not a layout file", [w(LAYOUT), cmd("./gradlew :app:connectedDebugAndroidTest")], "screens"),
    ("…a build into a folder called «test» is not a test run", [w(HOME_KT), cmd("xcodebuild -scheme App -derivedDataPath /tmp/test build")], "screens"),
    ("a look, then another change", [w(HOME_KT), cmd(RUN), w(LAYOUT)], "screens"),
    ("mobile.py devices or project is not a look", [w(HOME_KT), cmd(f"{TOOL} project ."), cmd(f"{TOOL} devices")], "screens"),
    ("another script whose name ends in mobile.py, or a grep for screencap, is not a look",
     [w(HOME_KT), cmd("python3 tests/test_mobile.py run"), cmd("grep -rn screencap docs/")], "screens"),
    ("an action with --shot after an install is a look", [w(LAYOUT), cmd(INSTALL), cmd(f'{TOOL} tap --text "Войти" --shot')], None),
    ("app logic only (no screen): the ordinary verify reminder", [w("app/src/main/java/Repo.kt")], "verify"),
    ("a web component stays a web matter", [w("web/Page.tsx"), cmd("npm test")], None),
    ("a React Native screen is a mobile matter, not a web page", [w("rn/Screen.tsx"), cmd("npm test")], "screens"),
    ("…and there a screenshot is enough: a saved file reaches the running app by itself", [w("rn/Screen.tsx"), cmd(SHOT)], None),
]
for name, steps, want in cases:
    setup(steps); got = stop() or ""
    kind = "screens" if "the app's screens" in got else "verify" if "ran no check" in got else None if not got else "other"
    good = kind == want and (want != "screens" or ("mobile.py" in got and "run --project" in got and "BypassSandbox" in got and "read_file" in got))
    check(f"stop: {name}", good, got or "(silent)")
setup([w("Sources/Main.swift"), cmd("xcodebuild -scheme MacTool build")], files=MAC_APP)
check("stop: a SwiftUI view of a Mac app is not sent to the iPhone simulator", stop() is None)
setup([w("src/main/kotlin/Main.kt"), cmd("./gradlew build")], files=DESKTOP)
check("stop: a Compose screen of a desktop app is not sent to the Android emulator", stop() is None)
setup([w("lib/home.dart"), cmd("flutter test")], files={"pubspec.yaml": "name: x\nflutter:\n  uses-material-design: true\n",
                                                          "lib/home.dart": "import 'package:flutter/material.dart';\nclass Home extends StatelessWidget {}\n"})
check("stop: a Flutter screen changed, then `flutter test` (widget tests): covered", stop() is None)
setup([w("lib/api.dart"), cmd("flutter test")], files={"pubspec.yaml": "name: x\nflutter:\n", "lib/api.dart": "class Api {}\n"})
check("stop: Flutter logic changed, then `flutter test`: a check was run", stop() is None)
if compat.MAC and mobile_xcrun_real():
    setup([w("ios/Home.swift"), cmd("xcodebuild -scheme App build")]); got = stop() or ""
    check("stop: a SwiftUI view of an iOS app changed and only built (on a Mac with Xcode)", "the app's screens (ios/Home.swift)" in got, got or "(silent)")
    setup([w("ios/Model.swift"), cmd("xcodebuild -scheme App build")])
    check("stop: a Swift model changed and built: nothing to look at", stop() is None)
setup([w(HOME_KT), cmd(BUILD), cmd(RUN)], "Поменял экран. Проверил в эмуляторе — всё выглядит правильно.")
check("facts: «проверил в эмуляторе» after a real run passes", stop() is None)
setup([w(HOME_KT), cmd(RUN), w(HOME_KT), cmd("./gradlew :app:connectedDebugAndroidTest")], "Поменял экран. Проверил в эмуляторе — всё выглядит правильно.")
got = stop() or ""
check("facts: «проверил в эмуляторе» when the app last ran BEFORE the final change → sent back", "simulator or an emulator" in got and "after your last change" in got, got or "(silent)")
setup([w(HOME_KT), cmd(BUILD), cmd(f"{TOOL} devices"), cmd("adb devices -l"), cmd(INSTALL)], "Поменял экран. Запустил приложение на эмуляторе, всё работает.")
got = stop() or ""
check("facts: listing devices and installing a build don't back «запустил на эмуляторе»", "simulator or an emulator" in got or "the app's screens" in got, got or "(silent)")
setup([cmd("ls")], "Чтобы проверить в симуляторе, соберите проект и запустите его.")
check("facts: advice to the user isn't a claim", stop() is None)
for f in os.listdir(conv.STATE_DIR):
    if f.startswith(cid): os.remove(os.path.join(conv.STATE_DIR, f))
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
