"""/release: version sources and mismatches, suggested bump, notes, bump that keeps formatting, CHANGELOG, tag, publish."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys

PLUGIN, SP = sys.argv[1], sys.argv[2]
REL = os.path.join(PLUGIN, "skills", "release", "scripts", "release.py")
WIN = os.name == "nt"
TODAY = datetime.date.today().isoformat()
for _v in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"):
    os.environ.pop(_v, None)
REAL_GIT = shutil.which("git") or "git"

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {str(detail)[:1500]}" if not cond and detail != "" else ""))


def write(root, rel, content, crlf=False):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content.replace("\n", "\r\n") if crlf else content)


def read(root, rel):
    with open(os.path.join(root, *rel.split("/")), encoding="utf-8", newline="") as f:
        return f.read()


def git(cwd, *args):
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else None


def commit(cwd, subject, body="", files=None):
    for rel, content in (files or {}).items():
        write(cwd, rel, content)
    if not files:
        write(cwd, "log.txt", (read(cwd, "log.txt") if os.path.exists(os.path.join(cwd, "log.txt")) else "") + subject + "\n")
    git(cwd, "add", "-A")
    msg = subject + ("\n\n" + body if body else "")
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=cwd, capture_output=True)


def mkrepo(name, files):
    root = os.path.join(SP, name)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, capture_output=True)
    commit(root, "chore: initial", files=files)
    return root


# ── fake gh: auth status, run list (mode from AGP_FAKE_GH_MODE), release create (logged with the notes) ──
FAKE_BIN = os.path.join(SP, "fake_bin")
ONLY_GIT = os.path.join(SP, "only_git")
os.makedirs(FAKE_BIN, exist_ok=True)
os.makedirs(ONLY_GIT, exist_ok=True)
GH_LOG = os.path.join(SP, "gh_log.jsonl")
FAKE_GH = os.path.join(SP, "fake_gh.py")
with open(FAKE_GH, "w", encoding="utf-8") as f:
    f.write(r'''import json, os, sys
argv = sys.argv[1:]
mode = os.environ.get("AGP_FAKE_GH_MODE", "green")
entry = {"argv": argv}
if argv[:2] == ["release", "create"] and "--notes-file" in argv:
    with open(argv[argv.index("--notes-file") + 1], encoding="utf-8") as nf:
        entry["notes"] = nf.read()
with open(os.environ["AGP_FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(entry) + "\n")
if argv[:2] == ["auth", "status"]:
    if "noauth" in mode:
        sys.stderr.write("You are not logged into any GitHub hosts. To log in, run: gh auth login\n")
        sys.exit(1)
    print("Logged in to github.com account example")
elif argv[:2] == ["run", "list"]:
    runs = {"green": [{"workflowName": "CI", "status": "completed", "conclusion": "success", "url": "u"}],
            "red": [{"workflowName": "CI", "status": "completed", "conclusion": "failure", "url": "u"}],
            "running": [{"workflowName": "CI", "status": "in_progress", "conclusion": "", "url": "u"}]}
    print(json.dumps(runs.get(mode.replace("noauth", "").strip(",") or "green", [])))
elif argv[:2] == ["release", "create"]:
    print("https://github.com/example/demo/releases/tag/" + argv[2])
else:
    sys.exit(2)
''')
if WIN:
    with open(os.path.join(FAKE_BIN, "gh.cmd"), "w", encoding="utf-8") as f:
        f.write(f'@echo off\r\n"{sys.executable}" "{FAKE_GH}" %*\r\n')
    with open(os.path.join(ONLY_GIT, "git.cmd"), "w", encoding="utf-8") as f:
        f.write(f'@echo off\r\n"{REAL_GIT}" %*\r\n')
else:
    with open(os.path.join(FAKE_BIN, "gh"), "w", encoding="utf-8") as f:
        f.write(f"#!{sys.executable}\nimport runpy, sys\nrunpy.run_path({FAKE_GH!r}, run_name='__main__')\n")
    os.chmod(os.path.join(FAKE_BIN, "gh"), 0o755)
    with open(os.path.join(ONLY_GIT, "git"), "w", encoding="utf-8") as f:
        f.write(f"#!{sys.executable}\nimport subprocess, sys\nsys.exit(subprocess.call([{REAL_GIT!r}] + sys.argv[1:]))\n")
    os.chmod(os.path.join(ONLY_GIT, "git"), 0o755)
ENV_GH = dict(os.environ, PATH=FAKE_BIN + os.pathsep + os.environ.get("PATH", ""), AGP_FAKE_GH_LOG=GH_LOG)
ENV_NO_GH = dict(os.environ, PATH=ONLY_GIT, AGP_FAKE_GH_LOG=GH_LOG)
if WIN:  # cmd.exe still needs SystemRoot; PATH alone decides what is found
    ENV_NO_GH["PATH"] = ONLY_GIT + os.pathsep + os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")


def rel(cwd, *args, env=None, mode="green"):
    e = dict(env or ENV_GH, AGP_FAKE_GH_MODE=mode)
    r = subprocess.run([sys.executable, REL] + list(args), cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=e)
    return r.returncode, r.stdout + r.stderr


def gh_calls():
    if not os.path.exists(GH_LOG):
        return []
    with open(GH_LOG, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def plan_json(cwd, *extra, env=None, mode="green"):
    code, out = rel(cwd, "plan", "--json", *extra, env=env, mode=mode)
    try:
        return json.loads(out)
    except ValueError:
        return {"_raw": out, "_code": code}


# ── 1. many manifest types, one of them behind ──
PLUGIN_JSON = """{
  // Demo plugin — comments must survive a bump.
  "name": "demo",
  /* "version": "0.0.0" in a comment is not the version */
  "description": "says \\"version\\": \\"9.9.9\\" in a string",
  "version": "1.4.2",
  "nested": { "version": "7.7.7" },
}
"""
PACKAGE_JSON = '{\n    "name": "demo",\n    "version": "1.4.2",\n    "config": {"version": "9.9.9"},\n    "engines": {"node": ">=18"}\n}\n'
PYPROJECT = '[project]\nname = "demo"\nversion = "1.4.2"  # keep this comment\n\n[tool.other]\nversion = "0.0.1"\n'
PBXPROJ = """// !$*UTF8*$!
{
		A1 /* Debug */ = {
			buildSettings = {
				MARKETING_VERSION = 1.4.2;
				PRODUCT_NAME = demo;
			};
		};
		A2 /* Release */ = {
			buildSettings = {
				MARKETING_VERSION = 1.4.2;
			};
		};
		A3 /* Widget */ = {
			buildSettings = {
				MARKETING_VERSION = "$(APP_VERSION)";
			};
		};
}
"""
CHANGELOG = """# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [1.4.2] - 2025-01-10

### Fixed
- Old fix.
"""
a = mkrepo("multi", {
    "package.json": PACKAGE_JSON,
    "plugin/plugin.json": PLUGIN_JSON,
    "Cargo.toml": '[package]\nname = "demo"\nversion = "1.4.1"\n\n[[bin]]\nname = "demo"\n\n[dependencies]\nserde = { version = "1.0" }\n',
    "VERSION": "1.4.2\n",
    "src/demo_pkg/__init__.py": '"""Demo."""\n__version__ = "1.4.2"\n',
    "app/build.gradle.kts": 'android {\n    defaultConfig {\n        versionCode = 42\n        versionName = "1.4.2"\n    }\n}\n',
    "Demo.xcodeproj/project.pbxproj": PBXPROJ,
    "CHANGELOG.md": CHANGELOG,
    ".github/workflows/ci.yml": "on: [push]\njobs: {}\n",
})
write(a, "pyproject.toml", PYPROJECT, crlf=True)
commit(a, "chore: pyproject", files={})
git(a, "tag", "-a", "v1.4.2", "-m", "Release v1.4.2")
commit(a, "fix(parser): handle empty input")
commit(a, "feat: add export command")
commit(a, "docs: fix a typo")
commit(a, "Merge-free plain subject without a prefix")

p = plan_json(a, "--no-ci")
files = sorted({s["file"] for s in p.get("sources", [])})
check("plan finds every kind of version source", files == sorted([
    "package.json", "plugin/plugin.json", "Cargo.toml", "VERSION", "src/demo_pkg/__init__.py", "app/build.gradle.kts",
    "Demo.xcodeproj/project.pbxproj", "pyproject.toml"]), p)
check("the JSONC manifest's version is the top-level one, not a comment, string or nested key",
      any(s["file"] == "plugin/plugin.json" and s["value"] == "1.4.2" for s in p.get("sources", [])), p.get("sources"))
check("both MARKETING_VERSION lines count as one source; $(APP_VERSION) is left alone",
      any(s["file"].endswith("project.pbxproj") and s["count"] == 2 for s in p.get("sources", []))
      and any("$(APP_VERSION)" in n for n in p.get("left_alone", [])), p)
check("the mismatch (Cargo.toml 1.4.1) is flagged against the majority 1.4.2",
      p.get("current") == "1.4.2" and len(p.get("mismatches", [])) == 1 and "Cargo.toml" in p["mismatches"][0], p)
check("last tag and commits since", p.get("last_tag") == "v1.4.2" and p.get("commits_since") == 4, p)
sug = p.get("suggested", {})
check("feat → minor, with the reason", sug.get("part") == "minor" and sug.get("version") == "1.5.0"
      and "add export command" in sug.get("reason", ""), sug)
notes = p.get("notes", "")
check("draft notes grouped features / fixes / other with the scope",
      notes.index("### Features") < notes.index("### Fixes") < notes.index("### Other")
      and "- parser: handle empty input (" in notes and "- add export command (" in notes
      and "Merge-free plain subject" in notes, notes)
check("CHANGELOG.md with [Unreleased] noticed", p.get("changelog") == {"path": "CHANGELOG.md", "unreleased": True}, p)
states = {c["text"].split(" ")[0]: c["ok"] for c in p.get("state", [])}
check("state: clean tree and the default branch pass, no upstream fails",
      p["state"][0]["ok"] is True and p["state"][1]["ok"] is True and p["state"][2]["ok"] is False, p.get("state"))
code, out = rel(a, "plan", "--no-ci")
check("the text plan shows the mismatch, the suggestion and the next steps", code == 0 and "⚠ mismatch" in out
      and "Suggested bump: minor → 1.5.0" in out and "publish only on the user's request" in out, out)

# CI through gh: green, red, gh absent
p = plan_json(a, mode="green")
check("CI green for HEAD through gh run list", p["state"][-1]["ok"] is True and "green" in p["state"][-1]["text"], p.get("state"))
p = plan_json(a, mode="red")
check("CI red for HEAD is a failed check", p["state"][-1]["ok"] is False and "red" in p["state"][-1]["text"], p.get("state"))
p = plan_json(a, env=ENV_NO_GH)
check("gh absent: CI unknown, pointing to /ci", p.get("state") and p["state"][-1]["ok"] is None
      and "gh is not installed" in p["state"][-1]["text"] and "/ci" in p["state"][-1]["text"], p)

# bump: a relative bump refuses on a mismatch, an explicit version fixes every source
before = {r_: read(a, r_) for r_ in ("plugin/plugin.json", "package.json", "pyproject.toml", "Cargo.toml",
                                     "Demo.xcodeproj/project.pbxproj", "CHANGELOG.md")}
code, out = rel(a, "bump", "minor")
check("bump minor refuses while the sources disagree", code == 1 and "bump X.Y.Z" in out
      and read(a, "Cargo.toml") == before["Cargo.toml"], out)
code, out = rel(a, "bump", "1.4.0")
check("bump to a lower version refuses", code == 1 and "not higher" in out, out)
code, out = rel(a, "bump", "1.5.0", "--dry-run")
check("bump --dry-run lists the edits and writes nothing", code == 0 and "Would set" in out and "Cargo.toml" in out
      and all(read(a, r_) == t for r_, t in before.items()), out)
code, out = rel(a, "bump", "1.5.0")
check("bump 1.5.0 succeeds", code == 0 and "Set version" in out, out)
check("plugin.json: only the version changed; comments, strings and nested keys kept",
      read(a, "plugin/plugin.json") == before["plugin/plugin.json"].replace('"version": "1.4.2"', '"version": "1.5.0"'),
      read(a, "plugin/plugin.json"))
check("package.json: top-level version only, indentation kept",
      read(a, "package.json") == PACKAGE_JSON.replace('"version": "1.4.2"', '"version": "1.5.0"'), read(a, "package.json"))
check("pyproject.toml: [project] version only, CRLF and the comment kept",
      read(a, "pyproject.toml") == PYPROJECT.replace('version = "1.4.2"', 'version = "1.5.0"').replace("\n", "\r\n"),
      repr(read(a, "pyproject.toml")))
check("Cargo.toml: [package] version set, the dependency's version untouched",
      'version = "1.5.0"' in read(a, "Cargo.toml") and 'serde = { version = "1.0" }' in read(a, "Cargo.toml"))
pbx = read(a, "Demo.xcodeproj/project.pbxproj")
check("pbxproj: both MARKETING_VERSION lines set, the variable kept",
      pbx.count("MARKETING_VERSION = 1.5.0;") == 2 and '"$(APP_VERSION)"' in pbx, pbx)
check("VERSION, __version__, versionName set", read(a, "VERSION") == "1.5.0\n"
      and '__version__ = "1.5.0"' in read(a, "src/demo_pkg/__init__.py")
      and 'versionName = "1.5.0"' in read(a, "app/build.gradle.kts") and "versionCode = 42" in read(a, "app/build.gradle.kts"))
cl = read(a, "CHANGELOG.md")
check("CHANGELOG: [Unreleased] stays on top, the new section below it with the generated notes, old ones intact",
      cl.index("## [Unreleased]") < cl.index(f"## [1.5.0] - {TODAY}") < cl.index("add export command")
      < cl.index("## [1.4.2] - 2025-01-10") and cl.endswith("- Old fix.\n"), cl)
p = plan_json(a, "--no-ci")
check("after the bump every source agrees", p.get("current") == "1.5.0" and not p.get("mismatches"), p)
check("plan before the commit says the tree is dirty", p["state"][0]["ok"] is False, p.get("state"))

# tag: dirty refuses, clean creates an annotated tag, twice refuses
code, out = rel(a, "tag")
check("tag refuses on a dirty tree", code == 1 and "uncommitted" in out and git(a, "tag", "-l", "v1.5.0") == "", out)
commit(a, "chore(release): v1.5.0", files={})
code, out = rel(a, "tag", "--dry-run")
check("tag --dry-run shows the command only", code == 0 and "git tag -a v1.5.0" in out and git(a, "tag", "-l", "v1.5.0") == "", out)
code, out = rel(a, "tag", "--version", "1.6.0")
check("tag refuses a version the sources don't say", code == 1 and "say 1.5.0" in out, out)
code, out = rel(a, "tag")
check("tag creates a local annotated tag", code == 0 and git(a, "cat-file", "-t", "v1.5.0") == "tag"
      and "git tag -d v1.5.0" in out, out)
code, out = rel(a, "tag")
check("tag refuses when the tag exists", code == 1 and "already exists" in out, out)
code, out = rel(a, "bump", "1.5.0")
check("bump refuses a version that is already tagged", code == 1, out)

# notes: HEAD is the tag → since the previous tag; release commit skipped; --out
code, out = rel(a, "notes")
check("notes at the tagged commit cover the commits since the previous tag",
      code == 0 and "add export command" in out and "chore(release)" not in out and "chore: initial" not in out, out)
code, out = rel(a, "notes", "--since", "HEAD~2", "--out", os.path.join(SP, "n.md"))
with open(os.path.join(SP, "n.md"), encoding="utf-8") as f:
    n_md = f.read()
check("notes --since --out writes the file", code == 0 and "Merge-free plain subject" in n_md
      and "add export command" not in n_md, n_md)

# publish: no --yes refuses, gh absent refuses, unpushed commit refuses, red CI refuses, --yes pushes and releases
origin = os.path.join(SP, "origin.git")
subprocess.run(["git", "init", "-q", "--bare", origin], capture_output=True)
git(a, "remote", "add", "origin", origin)
code, out = rel(a, "publish", "--yes")
check("publish refuses while the tagged commit isn't on the remote", code == 1 and "isn't on any origin branch" in out, out)
git(a, "push", "-q", "-u", "origin", "main")
n_calls = len(gh_calls())
code, out = rel(a, "publish")
check("publish without --yes refuses (exit 3), shows the commands, pushes nothing",
      code == 3 and "--yes" in out and "gh release create v1.5.0" in out
      and git(origin, "tag", "-l") == "" and not any(c["argv"][:2] == ["release", "create"] for c in gh_calls()), out)
code, out = rel(a, "publish", "--yes", env=ENV_NO_GH)
check("publish with gh absent refuses before pushing", code == 1 and "gh is not installed" in out
      and git(origin, "tag", "-l") == "", out)
code, out = rel(a, "publish", "--yes", mode="noauth")
check("publish with gh signed out refuses before pushing", code == 1 and "gh auth login" in out
      and git(origin, "tag", "-l") == "", out)
code, out = rel(a, "publish", "--yes", mode="red")
check("publish refuses on red CI", code == 1 and "Fix CI" in out and git(origin, "tag", "-l") == "", out)
code, out = rel(a, "publish", "--notes-file", os.path.join(SP, "missing_notes.md"), "--yes")
check("publish refuses missing --notes-file before pushing",
      code == 1 and "Notes file not found" in out and git(origin, "tag", "-l") == "", out)
code, out = rel(a, "publish", "--yes")
created = [c for c in gh_calls() if c["argv"][:2] == ["release", "create"]]
check("publish --yes pushes the tag and creates the release with generated notes",
      code == 0 and git(origin, "tag", "-l") == "v1.5.0" and len(created) == 1
      and created[0]["argv"][2] == "v1.5.0" and "--verify-tag" in created[0]["argv"]
      and created[0]["argv"][created[0]["argv"].index("--title") + 1] == "v1.5.0"
      and "add export command" in created[0].get("notes", "") and "releases/tag/v1.5.0" in out, (out, created))

# ── 2. breaking changes → major; plain subjects → patch with a reason; prerelease bumps ──
b = mkrepo("breaking", {"package.json": '{"name": "b", "version": "2.3.4"}\n'})
git(b, "tag", "v2.3.4")
commit(b, "feat(api)!: drop the v1 endpoints")
p = plan_json(b, "--no-ci")
check("`!` → major", p.get("suggested", {}).get("part") == "major" and p["suggested"]["version"] == "3.0.0"
      and "### Breaking changes" in p.get("notes", ""), p)
c = mkrepo("breaking_body", {"package.json": '{"name": "c", "version": "0.4.0"}\n'})
git(c, "tag", "0.4.0")
commit(c, "refactor: new config format", body="BREAKING CHANGE: the old keys are gone")
p = plan_json(c, "--no-ci")
check("BREAKING CHANGE in the body → major, with the 0.x note; tags without v are understood",
      p.get("suggested", {}).get("part") == "major" and p["suggested"]["version"] == "1.0.0"
      and "before 1.0" in p["suggested"]["reason"] and p.get("tag_prefix") == "" and p.get("last_tag") == "0.4.0", p)
d = mkrepo("plain", {"VERSION": "v0.9.0\n", "CHANGELOG.md": "# History\n\n## v0.9.0 (2025-03-01)\n\n- First.\n"})
git(d, "tag", "v0.9.0")
commit(d, "Update the readme")
p = plan_json(d, "--no-ci")
check("no conventional prefixes → patch, and it says why", p.get("suggested", {}).get("part") == "patch"
      and "patch by default" in p["suggested"]["reason"], p)
code, out = rel(d, "bump", "patch")
cl = read(d, "CHANGELOG.md")
check("bump keeps the v in VERSION and mirrors the changelog's heading style above the old one",
      code == 0 and read(d, "VERSION") == "v0.9.1\n" and cl.index(f"## v0.9.1 ({TODAY})") < cl.index("## v0.9.0 (2025-03-01)")
      and "Update the readme" in cl, out + "\n" + cl)
e = mkrepo("unreleased", {"pyproject.toml": '[tool.poetry]\nname = "e"\nversion = "1.0.0-rc.2"\n',
                          "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n### Added\n- Manual entry.\n\n## [0.9.0] - 2025-01-01\n- Old.\n"})
git(e, "tag", "v0.9.0")
commit(e, "feat: something")
code, out = rel(e, "bump", "patch")
cl = read(e, "CHANGELOG.md")
check("a pre-release bumps to its release; [Unreleased]'s own text becomes the release section",
      code == 0 and 'version = "1.0.0"' in read(e, "pyproject.toml")
      and cl.index("## [Unreleased]") < cl.index(f"## [1.0.0] - {TODAY}") < cl.index("- Manual entry.") < cl.index("## [0.9.0]")
      and "feat: something" not in cl and "something (" not in cl, cl)

# ── 3. edge cases ──
f_ = mkrepo("noversion", {"README.md": "# x\n"})
code, out = rel(f_, "bump", "patch")
check("bump with no version source refuses", code == 1 and "No version source" in out, out)
code, out = rel(f_, "plan", "--no-ci")
check("plan with no version source still reports the state", code == 0 and "none found" in out and "State:" in out, out)
plain_dir = os.path.join(SP, "not_git")
os.makedirs(plain_dir)
write(plain_dir, "package.json", '{"version": "1.0.0"}\n')
code, out = rel(plain_dir, "tag")
check("tag outside a git repository exits 2", code == 2, out)
code, out = rel(plain_dir, "bump", "minor")
check("bump works without git", code == 0 and read(plain_dir, "package.json") == '{"version": "1.1.0"}\n', out)
git(a, "remote", "add", "gh", "git@github.com:example/demo.git")
git(a, "remote", "add", "gh2", "https://user:secret@github.com/example/demo2")
sys.path.insert(0, os.path.dirname(REL))
import release as rtool  # noqa: E402
check("github_repo reads OWNER/REPO from ssh and https remotes, without credentials",
      rtool.github_repo(a, "gh") == "example/demo" and rtool.github_repo(a, "gh2") == "example/demo2"
      and rtool.github_repo(a, "origin") == "", (rtool.github_repo(a, "gh"), rtool.github_repo(a, "gh2")))
check("semver order: a pre-release sorts before its release, rc.10 after rc.2",
      rtool.vkey(rtool.parse_version("1.0.0-rc.2")) < rtool.vkey(rtool.parse_version("1.0.0-rc.10"))
      < rtool.vkey(rtool.parse_version("1.0.0")))

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
