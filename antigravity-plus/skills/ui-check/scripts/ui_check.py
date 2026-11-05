#!/usr/bin/env python3
"""Antigravity Plus — /ui-check: look at a web page the way users see it, with headless Chrome (nothing to install).

  ui_check.py URL|FILE [--widths 390,1440] [--height 2400] [--out DIR] [--scan DIR]

For each width it saves a screenshot (open them with read_file and look) and collects the page's console errors.
--scan DIR also lists effects in the project's CSS/JS/HTML that are heavy for phone GPUs (backdrop-filter, big
blurs and shadows, endless animations, fixed backgrounds, WebGL/canvas loops, autoplay video).
Uses Google Chrome, Chromium, Brave or Edge with a throw-away profile, so the user's browser is not touched.
Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks"))
import compat  # noqa: E402  (where browsers live on macOS, Linux and Windows)
CONSOLE = re.compile(r':CONSOLE[^\]]*\] "(.*)", source: (\S*?) \((\d+)\)')
NOISE = re.compile(r"DevTools|Download the React DevTools|\[HMR\]|\[vite\] connect", re.I)
HEAVY = [
    ("backdrop-filter (blur behind elements: one of the heaviest effects on iPhone)", re.compile(r"backdrop-filter\s*:", re.I)),
    ("filter: blur() with a big radius", re.compile(r"filter\s*:[^;]*blur\(\s*(?:[2-9]\d|\d{3,})px", re.I)),
    ("box-shadow / text-shadow with a big blur", re.compile(r"(?:box|text)-shadow\s*:[^;]*\s(?:[4-9]\d|\d{3,})px", re.I)),
    ("endless animation", re.compile(r"animation[^;:]*:[^;]*\binfinite\b|animation-iteration-count\s*:\s*infinite", re.I)),
    ("background-attachment: fixed (repaints on every scroll in mobile Safari)", re.compile(r"background-attachment\s*:\s*fixed", re.I)),
    ("mix-blend-mode", re.compile(r"mix-blend-mode\s*:", re.I)),
    ("will-change on many elements", re.compile(r"will-change\s*:", re.I)),
    ("WebGL / three.js / canvas animation loop", re.compile(r"getContext\(\s*['\"]webgl|THREE\.|requestAnimationFrame\(", re.I)),
    ("autoplaying video", re.compile(r"<video[^>]*\bautoplay", re.I)),
]
SCAN_EXT = (".css", ".scss", ".sass", ".less", ".js", ".mjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html", ".htm")
SKIP = {"node_modules", ".git", "dist", "build", ".next", ".nuxt", "vendor", "coverage", ".venv", "venv"}


def browser() -> Optional[str]:
    for b in compat.browsers():
        p = b if os.path.isabs(b) else shutil.which(b)
        if p and os.access(p, os.X_OK):
            return p
    return None


def shoot(chrome: str, url: str, width: int, height: int, out: str) -> dict:
    profile = tempfile.mkdtemp(prefix="agp-ui-")
    png = os.path.join(out, f"page-{width}px.png")
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
           f"--user-data-dir={profile}", f"--window-size={width},{height}", "--force-device-scale-factor=2",
           "--virtual-time-budget=6000", "--enable-logging=stderr", "--v=0", f"--screenshot={png}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        log = r.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        log = f"chrome failed: {e}"
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    visible = rendered_text(chrome, url, width, height)
    msgs = []
    for m in CONSOLE.finditer(log):
        text = m.group(1)
        if not NOISE.search(text) and text not in [x[0] for x in msgs]:
            msgs.append((text, f"{os.path.basename(m.group(2)) or url}:{m.group(3)}"))
    failed = re.findall(r"net::ERR_\w+", log)
    return {"png": png if os.path.isfile(png) else None, "console": msgs, "net": sorted(set(failed)), "text": visible}


def rendered_text(chrome: str, url: str, width: int, height: int) -> int:
    """How much visible text the page rendered (after scripts ran): 0 means a blank page."""
    profile = tempfile.mkdtemp(prefix="agp-ui-")
    try:
        r = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
                            f"--window-size={width},{height}", "--virtual-time-budget=6000", "--dump-dom", url],
                           capture_output=True, text=True, timeout=60)
        dom = r.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return -1
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    body = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>|<[^>]+>", " ", dom)
    return len(re.sub(r"\s+", "", body))


def scan(root: str) -> List[str]:
    hits = {label: [] for label, _ in HEAVY}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP and not d.startswith(".")]
        for fn in fns:
            if not fn.endswith(SCAN_EXT) or fn.endswith(".min.js"):
                continue
            p = os.path.join(dp, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read(1_000_000)
            except OSError:
                continue
            for label, rx in HEAVY:
                for m in rx.finditer(text):
                    hits[label].append(f"{os.path.relpath(p, root)}:{text.count(chr(10), 0, m.start()) + 1}")
    out = []
    for label, places in hits.items():
        if places:
            out.append(f"- {label}: {len(places)} — " + ", ".join(places[:6]) + (" …" if len(places) > 6 else ""))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="URL (http://localhost:3000/…) or a path to an HTML file")
    ap.add_argument("--widths", default="390,1440", help="viewport widths, px (390 ≈ iPhone, 1440 ≈ laptop)")
    ap.add_argument("--height", type=int, default=2400, help="viewport height, px (tall = more of the page)")
    ap.add_argument("--out", help="folder for screenshots (default: a new folder under ~/.gemini/antigravity-plus/ui/)")
    ap.add_argument("--scan", metavar="DIR", help="also scan DIR for effects that are heavy on phone GPUs")
    a = ap.parse_args(argv)
    url = a.target if re.match(r"^[a-z]+://", a.target) else "file://" + os.path.abspath(os.path.expanduser(a.target))
    chrome = browser()
    if not chrome:
        print("Chrome / Chromium / Brave / Edge not found: install one, or check the page with the user's browser.")
        return 2
    out = a.out or os.path.expanduser(f"~/.gemini/antigravity-plus/ui/{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out, exist_ok=True)
    print(f"## /ui-check — {url}\n")
    for w in [int(x) for x in a.widths.split(",") if x.strip().isdigit()]:
        r = shoot(chrome, url, w, a.height, out)
        print(f"### {w}px" + (" (phone)" if w < 600 else " (tablet)" if w < 1000 else " (desktop)"))
        print(f"Screenshot: {r['png'] or 'failed — is the URL reachable?'}")
        if r["console"]:
            print("Console errors and warnings:")
            for text, where in r["console"][:15]:
                print(f"- {text[:300]} ({where})")
        if r["net"]:
            print("Failed requests: " + ", ".join(r["net"]))
        if not r["console"] and not r["net"]:
            print("Console: clean")
        if 0 <= r["text"] < 20:
            print("⚠️ The page rendered almost no text: it's blank or still loading. For an app (React, Vue…), open it "
                  "through its running dev server (http://localhost:…), not as a file, and check that its scripts load.")
        print()
    if a.scan:
        found = scan(os.path.abspath(os.path.expanduser(a.scan)))
        print("### Heavy effects for phone GPUs (" + os.path.abspath(os.path.expanduser(a.scan)) + ")")
        print("\n".join(found) if found else "none found")
    print("\nOpen each screenshot with read_file and look at it: layout, cut-off or overlapping content, horizontal "
          "overflow, text size, tap targets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
