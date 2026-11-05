---
name: ui-check
description: >-
  Looks at a web page the way users see it, with headless Chrome (nothing to install): screenshots at phone and
  desktop width that you then look at, console errors, and effects that make phones slow or hot (backdrop-filter,
  big blurs, endless animations, WebGL). Use when the user asks to check how a page looks or performs («проверь
  вёрстку», «как выглядит на телефоне», «сайт тормозит на iPhone», /ui-check), and on your own after you change web
  UI (HTML, CSS, JS/TS, JSX/TSX, Vue, Svelte), before reporting it done.
---

# /ui-check — see the page, phone and desktop

Helper: `python3 "<skill_dir>/scripts/ui_check.py"`. It runs headless Chrome (or Chromium, Brave, Edge) with a
throw-away profile, so it needs `BypassSandbox: true`. It doesn't touch the user's browser.

## Steps
1. **Find the URL.** An app (React, Vue, a Python backend with templates) needs its running dev server:
   `http://localhost:<port>/…`. Start it the project's way (`AGENTS.md`, `package.json`, compose) if it isn't
   running, and stop it afterwards if you started it. A static page can be opened as a file.
2. **Run** for the pages you changed:
   ```bash
   python3 "<skill_dir>/scripts/ui_check.py" "<url or file>" --widths 390,1440 --scan "<folder with the front-end code>"
   ```
   `390` is an iPhone, `1440` a laptop. Add `768` for a tablet layout.
3. **Look at every screenshot** (read_file on each PNG): layout, cut-off or overlapping blocks, horizontal overflow,
   text size, tap targets, empty or broken areas. A blank page means the app didn't load: check the URL and the
   console errors.
4. **Console errors** from the report: fix the ones your change caused.
5. **Performance**, when the page is slow, heats the phone or scrolls badly: go through the `--scan` list. The usual
   fixes are dropping or narrowing `backdrop-filter` and big blurs, pausing animations off-screen
   (`IntersectionObserver`) or for `prefers-reduced-motion`, replacing `background-attachment: fixed` with a fixed
   pseudo-element, and stopping canvas/WebGL loops when hidden.
6. **Report** in 2–4 lines, with what you saw and the screenshot paths. When you fixed something, run the check
   again and say what changed.

## When you run it on your own
After you change web UI, before you report the work done: check the changed pages at 390 and 1440 px. Say it in one
line first («Проверяю вёрстку: 390 и 1440 px»).
