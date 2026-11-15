#!/usr/bin/env python3
"""Antigravity Plus — updates: hear about a new release, install it in one step, roll it back.

    update.py status [--json]       what is known, without the network
    update.py check  [--json]       look for a new release now and remember what was found
    update.py run    [--source DIR] install the newest release (the user's /update, or ./install.sh --update)
    update.py rollback              put the version from before the last update back (twice: forward again)
    update.py background            the daily check SessionStart starts; under autoUpdate it also installs

Where a release comes from: the folder the plugin was installed from (modules.json "source").
  * A git clone: `git fetch`, then a fast-forward of its default branch — only when the clone has no uncommitted
    changes, is on that branch and has no commits of its own. Otherwise it stops, says why and touches nothing.
  * No clone (installed from a downloaded archive, or the folder is gone): the default branch's archive from GitHub
    (REPO), unpacked into ~/.gemini/antigravity-plus/source, which is the source from then on.
A release is a higher "version" in antigravity-plus/plugin.json (not every commit); its notes are the subjects of
the release commits ("Antigravity Plus X.Y.Z: …"). The source's own install.py then installs it with the saved
module choice, so nothing is asked, and the usage panel restarts when its server changed. Before that, the installed
plugin, Claude Code's skill and modules.json go to ~/.gemini/antigravity-plus/previous/ for `rollback`.

What it knows lives in ~/.gemini/antigravity-plus/update.json: SessionStart (hooks/session_start.py) starts the
check in the background at most once a day — a conversation never waits for the network — and tells the main agent
about a new release once a day, so it can tell the user and offer /update. Settings: updateCheck (on) — the daily
check; autoUpdate (off) — the background check installs a release itself when that is safe (a clean fast-forward).
Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))
import compat  # noqa: E402

HOME = os.path.expanduser("~")
DATA = os.path.join(HOME, ".gemini", "antigravity-plus")
CACHE = os.path.join(DATA, "update.json")
LOCK = os.path.join(DATA, "update.lock")
PREVIOUS = os.path.join(DATA, "previous")
MANAGED = os.path.join(DATA, "source")
MODULES_FILE = os.path.join(DATA, "modules.json")
DEST = os.path.join(HOME, ".gemini", "config", "plugins", "antigravity-plus")
CLAUDE_SKILL = os.path.join(HOME, ".claude", "skills", "antigravity")
PANEL = "antigravity-plus/usage"
REPO = os.environ.get("AGP_UPDATE_REPO") or "stxrmik/antigravity-plus"
BRANCH = "main"
RAW = os.environ.get("AGP_UPDATE_RAW") or f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/antigravity-plus/plugin.json"
ARCHIVE = os.environ.get("AGP_UPDATE_ARCHIVE") or f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{BRANCH}"
COMMITS = os.environ.get("AGP_UPDATE_COMMITS") or f"https://api.github.com/repos/{REPO}/commits?sha={BRANCH}&per_page=50"
DAY = 86400
RETRY = 3600            # a failed or unfinished background check is retried an hour later at the earliest
STALE_LOCK = 900        # an update that holds the lock longer than this died
MAX_ARCHIVE = 64 * 1024 * 1024
RELEASE = re.compile(r"^Antigravity Plus (\d+(?:\.\d+)+)\s*[:—–-]\s*(.+)$")
VERSION = re.compile(r'"version"\s*:\s*"([^"]+)"')
SKIP = {"__pycache__", ".DS_Store"}


class Stop(Exception):
    """An update that can't go on: the reason is for the user, nothing was changed."""


# ─────────────────────────────── versions and settings ───────────────────────────────

def vtuple(v: Optional[str]) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def newer(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and vtuple(a) > vtuple(b)


def version_in(text: str) -> str:
    m = VERSION.search(text or "")
    return m.group(1) if m else ""


def version_of(plugin_dir: str) -> str:
    try:
        with open(os.path.join(plugin_dir, "plugin.json"), encoding="utf-8") as f:
            return version_in(f.read())
    except OSError:
        return ""


def installed_version() -> str:
    return version_of(DEST) or str(read_json(MODULES_FILE).get("version") or "")


def read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    compat.replace(tmp, path)


def load_cache() -> dict:
    return read_json(CACHE)


def save_cache(**changes) -> dict:
    data = load_cache()
    data.update(changes)
    try:
        write_json(CACHE, data)
    except OSError:
        pass
    return data


def setting(key: str, default: bool) -> bool:
    if key == "updateCheck" and os.environ.get("AGP_UPDATE_CHECK", "").lower() in ("0", "off", "no", "false"):
        return False
    try:
        import memory
        return bool(memory.settings().get(key, default))
    except Exception:
        return default


# ─────────────────────────────── the source folder ───────────────────────────────

def source_folder() -> str:
    """The folder the plugin was installed from (modules.json), when it still holds the installer."""
    src = str(read_json(MODULES_FILE).get("source") or "")
    return src if src and os.path.isfile(os.path.join(src, "install.py")) else ""


def git(args: List[str], cwd: str, timeout: float = 30) -> Tuple[int, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", LC_ALL="C")
    try:
        r = subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def is_clone(src: str) -> bool:
    return bool(src) and shutil.which("git") is not None and git(["rev-parse", "--show-toplevel"], src)[0] == 0 \
        and git(["remote", "get-url", "origin"], src)[0] == 0


def clone_state(src: str, fetch: bool) -> dict:
    """A git clone's position: its branch and the default one, uncommitted changes, commits ahead of and behind
    origin, the version in the folder and on origin's default branch."""
    st: Dict[str, object] = {"kind": "git", "path": src, "local": version_of(os.path.join(src, "antigravity-plus"))}
    if fetch:
        code, out = git(["fetch", "--quiet", "origin"], src, timeout=60)
        if code != 0:
            st["error"] = "git fetch failed: " + out.splitlines()[-1][:200] if out else "git fetch failed"
    code, head = git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], src)
    default = head.split("/", 1)[1] if code == 0 and "/" in head else BRANCH
    st["default"] = default
    code, branch = git(["symbolic-ref", "--quiet", "--short", "HEAD"], src)
    st["branch"] = branch if code == 0 else ""  # "" — a detached HEAD
    st["dirty"] = [l[3:] for l in git(["status", "--porcelain", "--untracked-files=no"], src)[1].splitlines() if l.strip()]
    code, counts = git(["rev-list", "--left-right", "--count", f"HEAD...origin/{default}"], src)
    if code == 0 and len(counts.split()) == 2:
        st["ahead"], st["behind"] = (int(x) for x in counts.split())
    else:
        st["ahead"], st["behind"] = 0, 0
        st.setdefault("error", f"no origin/{default} to compare with")
    code, text = git(["show", f"origin/{default}:antigravity-plus/plugin.json"], src)
    st["remote"] = version_in(text) if code == 0 else ""
    return st


def release_notes(subjects: List[str], after: str) -> List[str]:
    """«X.Y.Z: what it brought» for each release commit newer than `after`, oldest first."""
    seen: Dict[str, str] = {}
    for s in subjects:
        m = RELEASE.match((s or "").strip())
        if m and newer(m.group(1), after) and m.group(1) not in seen:
            seen[m.group(1)] = m.group(2).strip()
    return [f"{v}: {seen[v]}" for v in sorted(seen, key=vtuple)]


def git_notes(src: str, refs: List[str], after: str) -> List[str]:
    subjects: List[str] = []
    for ref in refs:
        code, out = git(["log", "--format=%s", "-n", "300", ref], src)
        if code == 0:
            subjects += out.splitlines()
    return release_notes(subjects, after)


def fetch(url: str, timeout: float = 15, limit: int = 2_000_000) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "antigravity-plus-update", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — https to GitHub, or file:// in tests
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url}: larger than {limit} bytes")
    return data


def github_notes(after: str) -> List[str]:
    try:
        commits = json.loads(fetch(COMMITS).decode("utf-8"))
        subjects = [str(((c or {}).get("commit") or {}).get("message") or "").split("\n", 1)[0] for c in commits]
    except Exception:
        return []
    return release_notes(subjects, after)


# ─────────────────────────────── check ───────────────────────────────

def check(fetch_remote: bool = True) -> dict:
    """Look for a release newer than the installed one; remember and return what was found."""
    installed, src = installed_version(), source_folder()
    found: Dict[str, object] = {"checked": time.time(), "installed": installed, "source": src, "error": ""}
    if is_clone(src):
        st = clone_state(src, fetch_remote)
        found.update(kind="git", latest=st.get("remote") or "", local=st.get("local") or "", error=st.get("error") or "")
        found["notes"] = git_notes(src, [f"origin/{st['default']}", "HEAD"], installed)
    else:
        found.update(kind="github", local="")
        try:
            found["latest"] = version_in(fetch(RAW).decode("utf-8", "replace"))
        except Exception as e:
            found["latest"], found["error"] = "", f"GitHub didn't answer: {e}"[:200]
        found["notes"] = github_notes(installed) if newer(str(found["latest"]), installed) else []
    best = max([str(found.get("latest") or ""), str(found.get("local") or "")], key=vtuple)
    found["best"] = best
    found["available"] = newer(best, installed)
    return save_cache(**found)


def status() -> dict:
    """What update.json knows, with the installed version read again (an update by hand moves it)."""
    c = load_cache()
    installed = installed_version()
    best = str(c.get("best") or c.get("latest") or "")
    return dict(c, installed=installed, best=best,
                available=newer(best, installed) and best != c.get("rolled_back"))


def describe(s: dict) -> List[str]:
    out = [f"Installed: Antigravity Plus {s.get('installed') or '?'}"]
    if not s.get("checked"):
        out.append("Not checked for updates yet: `update.py check`.")
        return out
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(s["checked"])))
    if s.get("available"):
        where = (f"in the source folder {s.get('source')}" if s.get("kind") == "git" and not newer(s.get("latest"), s.get("installed"))
                 else "on GitHub" if s.get("kind") == "github" else f"on the source's remote ({s.get('source')})")
        out.append(f"New release: {s.get('best')} {where} (checked {when}). Install: /update (`update.py run`).")
        out += [f"  - {n}" for n in s.get("notes") or []]
    else:
        out.append(f"Up to date (checked {when}).")
    if s.get("error"):
        out.append(f"Last check: {s['error']}")
    return out


# ─────────────────────────────── background check ───────────────────────────────

def due() -> bool:
    if not setting("updateCheck", True):
        return False
    c, now = load_cache(), time.time()
    return now - float(c.get("checked") or 0) >= DAY and now - float(c.get("attempt") or 0) >= RETRY


def spawn_background() -> bool:
    """Start the daily check in a process of its own when it's due; the caller never waits for the network."""
    if not due():
        return False
    save_cache(attempt=time.time())
    kw: Dict[str, object] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                             "close_fds": True, "cwd": DATA if os.path.isdir(DATA) else HOME}
    if compat.WINDOWS:
        kw["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "background"], **kw)  # noqa: S603
        return True
    except OSError:
        return False


def background() -> int:
    if not setting("updateCheck", True) or not take_lock():
        return 0
    try:
        found = check()
        if found.get("available") and setting("autoUpdate", False) and found.get("best") != found.get("rolled_back"):
            before = installed_version()
            try:
                report = install_release()
            except Stop as e:  # not safe to do unattended: the user hears about the release instead
                save_cache(auto_error=str(e))
                return 0
            save_cache(auto_installed={"from": before, "to": report["to"], "notes": report["notes"],
                                       "restart": report["restart"], "at": time.time(), "told": False}, auto_error="")
        return 0
    finally:
        drop_lock()


def notice(sub: bool = False) -> List[str]:
    """For the main agent's session context, at most once a day: a release it should tell the user about, or the
    update autoUpdate installed. Subagents and Claude Code's delegated agents never get it."""
    if sub:
        return []
    c = load_cache()
    auto = c.get("auto_installed") if isinstance(c.get("auto_installed"), dict) else None
    if auto and not auto.get("told"):
        auto["told"] = True
        save_cache(auto_installed=auto)
        notes = "; ".join(auto.get("notes") or []) or "see `update.py status`"
        return [f"Antigravity Plus updated itself from {auto.get('from')} to {auto.get('to')} (auto-update is on): {notes}. "
                "Tell the user in one line at the end of your first reply, in their language"
                + (f" ({auto['restart']})." if auto.get("restart") else ".")]
    s = status()
    if not s.get("available") or not setting("updateCheck", True):
        return []
    told = c.get("told") if isinstance(c.get("told"), dict) else {}
    if told.get("version") == s["best"] and time.time() - float(told.get("at") or 0) < DAY:
        return []
    save_cache(told={"version": s["best"], "at": time.time()})
    local = s.get("kind") == "git" and not newer(s.get("latest"), s.get("installed"))
    what = (f"the source folder {s.get('source')} holds Antigravity Plus {s['best']}, but {s.get('installed')} is "
            "installed" if local else f"Antigravity Plus {s['best']} is out (installed: {s.get('installed') or '?'})")
    notes = "; ".join(s.get("notes") or [])
    return [f"Update: {what}" + (f" — {notes}" if notes else "") + ". At the end of your first reply, tell the user "
            "in one line, in their language, and offer /update (it updates in one step and can roll back). Don't run "
            "it unless they say so."]


# ─────────────────────────────── install a release ───────────────────────────────

def take_lock() -> bool:
    os.makedirs(DATA, exist_ok=True)
    try:
        if time.time() - os.path.getmtime(LOCK) > STALE_LOCK:
            os.remove(LOCK)
    except OSError:
        pass
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except OSError:
        return False


def drop_lock() -> None:
    try:
        os.remove(LOCK)
    except OSError:
        pass


def safe_extract(archive: str, dest: str) -> None:
    """Unpack a .tar.gz, refusing paths that leave `dest`; links and devices are left out (the plugin has none)."""
    root = os.path.realpath(dest)
    with tarfile.open(archive, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                continue
            target = os.path.realpath(os.path.join(root, m.name))
            if target != root and not target.startswith(root + os.sep):
                raise Stop(f"the downloaded archive has a path outside its folder ({m.name}): not installed")
            members.append(m)
        kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tf.extractall(root, members=members, **kw)  # noqa: S202 — every member checked above


def download_source(installed: str) -> str:
    """The default branch's archive from GitHub → MANAGED. Returns the folder with install.py."""
    tmp = tempfile.mkdtemp(prefix="agp-update-")
    try:
        archive = os.path.join(tmp, "source.tar.gz")
        try:
            data = fetch(ARCHIVE, timeout=90, limit=MAX_ARCHIVE)
        except Exception as e:
            raise Stop(f"couldn't download {ARCHIVE}: {e}")
        with open(archive, "wb") as f:
            f.write(data)
        unpacked = os.path.join(tmp, "x")
        os.makedirs(unpacked)
        try:
            safe_extract(archive, unpacked)
        except (tarfile.TarError, OSError, EOFError) as e:
            raise Stop(f"the downloaded archive is damaged: {e}")
        tops = [os.path.join(unpacked, n) for n in os.listdir(unpacked)]
        top = next((t for t in tops if os.path.isfile(os.path.join(t, "install.py"))), "")
        if not top or not version_of(os.path.join(top, "antigravity-plus")):
            raise Stop("the downloaded archive isn't Antigravity Plus (no install.py and plugin.json)")
        new = MANAGED + ".new"
        shutil.rmtree(new, ignore_errors=True)
        shutil.move(top, new)
        old = MANAGED + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(MANAGED):
            os.rename(MANAGED, old)
        os.rename(new, MANAGED)
        shutil.rmtree(old, ignore_errors=True)
        return MANAGED
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ready_source(src: str, installed: str) -> Tuple[str, str]:
    """Bring the source folder to the newest release. Returns (folder, how): a clean fast-forward of a clone, the
    clone as it is when it already holds a newer version, or a fresh download — ("", how) when GitHub has nothing
    newer. Raises Stop when that isn't safe."""
    if is_clone(src):
        st = clone_state(src, fetch=True)
        default = str(st["default"])
        if st.get("error") and not st.get("remote"):
            raise Stop(f"{src}: {st['error']}. Update it by hand: `git pull`, then ./install.sh.")
        if st["dirty"]:
            raise Stop(f"{src} has uncommitted changes ({', '.join(list(st['dirty'])[:5])}): commit or stash them, "
                       "then /update again — or install the folder as it is with ./install.sh.")
        if st["branch"] != default:
            raise Stop(f"{src} is on {'branch ' + str(st['branch']) if st['branch'] else 'a detached HEAD'}, not "
                       f"{default}: switch to {default}, then /update again — or install it as it is with ./install.sh.")
        if int(st["behind"] or 0) and int(st["ahead"] or 0):
            raise Stop(f"{src}: {default} has {st['ahead']} commit(s) of its own and is {st['behind']} behind origin "
                       "— merge or rebase them yourself, then ./install.sh.")
        if int(st["behind"] or 0):
            code, out = git(["merge", "--ff-only", f"origin/{default}"], src, timeout=60)
            if code != 0:
                raise Stop(f"{src}: the fast-forward to origin/{default} failed ({out.splitlines()[-1][:200] if out else '?'}).")
            return src, f"pulled {st['behind']} commit(s) into {src} (fast-forward)"
        return src, f"from {src}"
    if src and os.path.isfile(os.path.join(src, "install.py")) and os.path.abspath(src) != os.path.abspath(MANAGED) \
            and newer(version_of(os.path.join(src, "antigravity-plus")), installed):
        return src, f"from {src}"
    try:
        latest = version_in(fetch(RAW).decode("utf-8", "replace"))
    except Exception as e:
        raise Stop(f"GitHub didn't answer ({e}); try again later.")
    if not newer(latest, installed):
        return "", f"checked github.com/{REPO}"
    return download_source(installed), f"downloaded from github.com/{REPO}"


def file_hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def copy_tree(src: str, dest: str) -> None:
    shutil.copytree(src, dest, symlinks=True, ignore=lambda _d, names: [n for n in names if n in SKIP])


def mirror(src: str, dest: str) -> None:
    """Make `dest` hold exactly `src` (changed files replaced in place, missing ones removed)."""
    for dp, dns, fns in os.walk(src):
        dns[:] = [d for d in dns if d not in SKIP]
        rel = os.path.relpath(dp, src)
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
        for fn in fns:
            if fn in SKIP:
                continue
            s, d = os.path.join(dp, fn), os.path.join(dest, rel, fn)
            if not os.path.exists(d) or file_hash(s) != file_hash(d):
                tmp = d + ".agp-tmp"
                shutil.copy2(s, tmp)
                compat.replace(tmp, d)
    for dp, dns, fns in os.walk(dest, topdown=False):
        rel = os.path.relpath(dp, dest)
        for fn in fns:
            if not os.path.exists(os.path.join(src, rel, fn)):
                os.remove(os.path.join(dp, fn))
        if rel != "." and not os.path.isdir(os.path.join(src, rel)):
            shutil.rmtree(dp, ignore_errors=True)


def claude_skill_ours(path: str = CLAUDE_SKILL) -> bool:
    return os.path.isfile(os.path.join(path, "scripts", "delegate.py"))


def snapshot(into: str) -> None:
    """The installed plugin, Claude Code's skill and modules.json, as they are now, into `into`."""
    shutil.rmtree(into, ignore_errors=True)
    os.makedirs(into)
    if os.path.isdir(DEST):
        copy_tree(DEST, os.path.join(into, "antigravity-plus"))
    if claude_skill_ours():
        copy_tree(CLAUDE_SKILL, os.path.join(into, "claude-skill"))
    if os.path.isfile(MODULES_FILE):
        shutil.copy2(MODULES_FILE, os.path.join(into, "modules.json"))


def restart_panel() -> str:
    """Restart the usage side panel through Antigravity (its server.mjs changed). Returns a line for the report."""
    if os.environ.get("AGP_UPDATE_PANEL", "").lower() == "off":  # tests: never touch a running Antigravity
        return "the usage panel's server changed: restart Antigravity to load it"
    try:
        sys.path.insert(0, os.path.join(PLUGIN, "skills", "doctor", "scripts"))
        import doctor
        ls = doctor.discover_ls()
        if not ls:
            return "the usage panel's server changed: restart Antigravity to load it"
        doctor.rpc(ls, "ManageSidecar", {"sidecarId": PANEL, "action": "SIDECAR_ACTION_RESTART"})
        return "the usage panel restarted with its new server"
    except Exception as e:
        return f"the usage panel's server changed but couldn't be restarted ({e}): restart Antigravity"


def install_release(source: Optional[str] = None) -> dict:
    """Bring the source to the newest release and install it. Returns {"from", "to", "notes", "how", "restart",
    "problems", "skills"}. Raises Stop when it can't go on safely; nothing has changed then. Never installs a version
    older than the installed one."""
    installed = installed_version()
    src = source or source_folder()
    folder, how = ready_source(src, installed)
    target = version_of(os.path.join(folder, "antigravity-plus")) if folder else installed
    if not newer(target, installed):
        save_cache(installed=installed, available=False, checked=time.time(), best=target or installed, latest=target)
        return {"from": installed, "to": installed, "notes": [], "how": how, "restart": "", "problems": [],
                "skills": [], "unchanged": True}
    notes = git_notes(folder, ["HEAD"], installed) if is_clone(folder) else github_notes(installed)
    panel_before = file_hash(os.path.join(DEST, "sidecars", "usage", "server.mjs"))
    skills_before = set(os.listdir(os.path.join(DEST, "skills"))) if os.path.isdir(os.path.join(DEST, "skills")) else set()
    if installed:
        snapshot(PREVIOUS)
    r = subprocess.run([sys.executable, os.path.join(folder, "install.py")], cwd=folder, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=900, stdin=subprocess.DEVNULL,
                       env=dict(os.environ, AGP_UPDATE_CHECK="off", PYTHONUTF8="1"))
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-12:])
        raise Stop(f"the installer failed (exit {r.returncode}); the previous version is kept in {PREVIOUS} "
                   f"(`update.py rollback`). Its last lines:\n{tail}")
    now = installed_version()
    skills_after = set(os.listdir(os.path.join(DEST, "skills"))) if os.path.isdir(os.path.join(DEST, "skills")) else set()
    new_skills = sorted(skills_after - skills_before)
    restart = []
    if panel_before and file_hash(os.path.join(DEST, "sidecars", "usage", "server.mjs")) != panel_before:
        restart.append(restart_panel())
    if new_skills:
        restart.append("restart Antigravity to see " + ", ".join("/" + s for s in new_skills))
    problems = [l.strip() for l in out.splitlines() if l.strip().startswith(("❌", "⚠️"))]
    save_cache(installed=now, available=False, checked=time.time(), best=now, latest=now, notes=[], error="",
               rolled_back="")
    return {"from": installed, "to": now, "notes": notes, "how": how, "restart": "; ".join(restart),
            "problems": problems, "skills": new_skills}


def run(source: Optional[str] = None) -> int:
    if not take_lock():
        print("Another update is running right now; try again in a minute.")
        return 1
    try:
        try:
            rep = install_release(source=source)
        except Stop as e:
            print(f"Not updated: {e}")
            return 2
        if rep.get("unchanged"):
            print(f"Antigravity Plus {rep['to'] or '?'} is the latest release ({rep['how']}). Nothing to install.")
            return 0
        print(f"Updated Antigravity Plus {rep['from'] or '?'} → {rep['to']} ({rep['how']}).")
        if rep["notes"]:
            print("What's new:")
            print("\n".join(f"  - {n}" for n in rep["notes"]))
        print("Applies to new conversations" + (f"; {rep['restart']}." if rep["restart"] else "."))
        if rep["problems"]:
            print("The check after installing found:")
            print("\n".join(f"  {p}" for p in rep["problems"]))
        if os.path.isdir(PREVIOUS):
            print("Roll back: /update rollback (`update.py rollback`).")
        return 0
    finally:
        drop_lock()


def rollback() -> int:
    prev = os.path.join(PREVIOUS, "antigravity-plus")
    if not version_of(prev):
        print("No previous version is kept: rollback is possible after an update.")
        return 1
    if not take_lock():
        print("An update is running right now; try again in a minute.")
        return 1
    try:
        old, now = version_of(prev), installed_version()
        swap = PREVIOUS + ".swap"
        snapshot(swap)  # what is installed now becomes the "previous" one: a second rollback goes forward again
        panel_before = file_hash(os.path.join(DEST, "sidecars", "usage", "server.mjs"))
        mirror(prev, DEST)
        if os.path.isdir(os.path.join(PREVIOUS, "claude-skill")) and (claude_skill_ours() or not os.path.exists(CLAUDE_SKILL)):
            mirror(os.path.join(PREVIOUS, "claude-skill"), CLAUDE_SKILL)
        if os.path.isfile(os.path.join(PREVIOUS, "modules.json")):
            shutil.copy2(os.path.join(PREVIOUS, "modules.json"), MODULES_FILE)
        shutil.rmtree(PREVIOUS, ignore_errors=True)
        os.rename(swap, PREVIOUS)
        save_cache(installed=old, rolled_back=now, available=False, told={})
        print(f"Rolled back Antigravity Plus {now} → {old}. Applies to new conversations"
              + ("; " + restart_panel() if file_hash(os.path.join(DEST, "sidecars", "usage", "server.mjs")) != panel_before else "")
              + f". The daily check won't offer {now} again; a newer release will be offered. Forward again: "
              "`update.py rollback` once more.")
        return 0
    finally:
        drop_lock()


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Antigravity Plus updates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "check"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    sub.add_parser("run").add_argument("--source", help="install from this folder (./install.sh --update passes its own)")
    sub.add_parser("rollback")
    sub.add_parser("background")
    a = ap.parse_args(argv)
    if a.cmd in ("status", "check"):
        s = check() if a.cmd == "check" else status()
        if a.cmd == "check":
            s = status()  # the installed version as it is, with rolled_back respected
        print(json.dumps(s, ensure_ascii=False, indent=1) if a.json else "\n".join(describe(s)))
        return 0
    if a.cmd == "run":
        return run(a.source)
    if a.cmd == "rollback":
        return rollback()
    return background()


if __name__ == "__main__":
    sys.exit(main())
