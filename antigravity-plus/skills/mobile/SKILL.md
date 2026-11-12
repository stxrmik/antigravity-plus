---
name: mobile
description: >-
  Runs a mobile app in the iOS simulator or the Android emulator and uses it the way a person would: installs and
  launches a build, takes screenshots that you then look at, lists what is on the screen (buttons, texts, fields),
  taps, swipes, types text, opens links, shows the app's errors and crashes, checks dark mode and large text.
  Nothing to install besides Xcode or the Android SDK. Use when the user asks to run, open, show or check an app on
  a simulator, an emulator, «на айфоне», «на андроиде» («запусти в симуляторе», «проверь на эмуляторе», «сделай
  скриншот приложения», «нажми…», «почему падает при запуске», /mobile), and on your own after you change a mobile
  app's screens (SwiftUI, UIKit, storyboards, Jetpack Compose, Android layouts and resources, Flutter, React
  Native), before reporting the work done.
---

# /mobile — the app in a simulator or an emulator

Helper: `python3 "<skill_dir>/scripts/mobile.py"` (below: `mobile.py`). Every command needs `BypassSandbox: true`:
it talks to the simulator and to adb. Only simulators and emulators are used; a phone on a cable is touched only
when the user asks and you name it with `--device`.

## The loop
1. **What's here.** `mobile.py devices` — simulators, emulators, what is running. `mobile.py project` — the mobile
   project in the current folder: the exact build command, the app's id, where the build lands.
2. **A device.** If none is running: `mobile.py boot` — it starts what the project needs (an emulator for an
   Android app, a TV one for a TV app, otherwise a simulator); `--ios`, `--android` or a name (`boot "iPhone 17 Pro"`,
   `boot Pixel_9`) choose. It starts without a window; add `--show` when the user wants to watch. It takes
   from half a minute to a couple of minutes and checks that the simulator takes touches.
3. **Build** with the command `project` printed. `boot`, a build and `run` outlast a normal command, so they go on
   in the background and their result arrives as a message: do other work meanwhile, don't poll.
4. **Run:** `mobile.py run` — installs the newest build of the project, launches it, takes a screenshot, lists the
   texts on the screen and the app's errors. `run "<path to .app or .apk>"` for a particular build. If it warns
   that a source file is newer than the build, build again first.
5. **Look.** Open the screenshot with read_file: layout, cut-off or overlapping content, unreadable text, empty
   areas, a blank screen. A launch without a look isn't a check.
6. **Go to the screen you changed and use it:**
   ```bash
   mobile.py ui                              # what is on the screen, with coordinates
   mobile.py tap --text "Войти" --shot       # tap by label (or: tap 368 650, tap --id loginButton); --shot = show the result
   mobile.py type "user@example.com" --into "Email"
   mobile.py swipe up                        # scroll the page down; also down, left, right, or X1 Y1 X2 Y2
   mobile.py key back                        # also: home, enter; on Android up, down, left, right, center (TV remote)
   mobile.py wait "Добро пожаловать" --timeout 20
   mobile.py open "myapp://settings"         # a deep link straight to a screen
   ```
   Coordinates are pixels of the latest screenshot. Prefer `--text` and `--id`: they don't break when the layout
   moves. Add `--shot` to the last action of a step instead of a separate `shot`.
7. **Check what phones differ in:** `mobile.py shot --dark --big-text` — the same screen in the other appearance
   and with large text, then back.
8. **When something is wrong:** `mobile.py logs <app id>` — the app's own output and errors, and the crash if it
   crashed (with the line of code). `logs <app id> --all` for every level.
9. **Report** in 2–4 lines what you saw, with the screenshot paths. After a fix: build, `run`, look again.

## Good to know
- Several devices running → add `--ios`, `--android` or `--device NAME` to the command.
- iOS: `type` replaces the whole text of a field (any language). `key enter` presses return on the on-screen
  keyboard (it's up while a field has the focus; `ui` says so); to submit a form, its own button is surer. Touches
  need the built-in helper; if it reports that this Xcode isn't supported, screenshots, launching, links and logs
  still work.
- Taps that change nothing at all (two different taps, the same screenshot): `mobile.py boot --restart` checks
  whether the simulator takes touches and repairs it if it doesn't (its apps restart). `boot` does the same check
  when it starts one.
- Android: `type` knows only Latin letters, digits and punctuation (an adb limit). A TV app is driven with
  `key up|down|left|right|center|back`. Coordinates match `adb shell input` and the `android` CLI, if you use them.
- A permission dialog in the way: `mobile.py grant <app id> photos` (iOS) or `run --grant` (Android), or tap its
  button by coordinates from the screenshot.
- A login the app needs: ask the user for a test account once, or say that the screen behind it wasn't checked.
- A screenshot shows what is installed. After you change code, `shot` alone shows the old screen: build and `run`
  first (React Native aside, where a saved file reaches the running app).
- Another language: `mobile.py launch <app id> --lang en`. Android keeps it for the app until `--lang system`.
- `mobile.py shutdown` stops only what this tool started. Leave running what the user started or asked to see.
- The emulator or a build fails with «not enough disk space»: tell the user; what to delete is their call.

## When you run it on your own
After you change a mobile app's screens, before you report the work done: build, `run`, open the screenshot, go
to the changed screen and look at it — and at `--dark` when you touched colours. Say it in one line first
(«Проверяю в симуляторе: собираю и запускаю»). Start a device yourself if none is running, without a window. If
the app can't be shown here (no simulator for this platform, it needs an account or a server that isn't running),
say so in one line instead of claiming it was checked.
