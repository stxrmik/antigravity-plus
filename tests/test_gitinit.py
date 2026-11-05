"""/git-init: refusals, .gitignore, secrets/databases/huge files kept out of the first commit."""
import os, shutil, subprocess, sys
PLUGIN, SP = sys.argv[1], sys.argv[2]
RI = os.path.join(PLUGIN, "skills", "git-init", "scripts", "repo_init.py")
def run(*args):
    r = subprocess.run([sys.executable, RI] + list(args), capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()
def git(cwd, *a):
    return subprocess.run(["git"] + list(a), cwd=cwd, capture_output=True, text=True).stdout
def mk(root, files):
    shutil.rmtree(root, ignore_errors=True); os.makedirs(root)
    for rel, content in files.items():
        p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        if isinstance(content, int):
            with open(p, "wb") as f: f.truncate(content)
        else:
            open(p, "w").write(content)
ok = True
def check(name, cond, detail=""):
    global ok; ok &= bool(cond); print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if not cond and detail else ""))
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
# 1. fresh Python+Node project with secrets and a huge file
p1 = os.path.join(SP, "gi_proj")
mk(p1, {"main.py": "print('hi')\n", "requirements.txt": "requests\n", ".env": "API_KEY=supersecretvalue123\n",
        ".env.example": "API_KEY=\n", "config.py": f"TOKEN = '{TOKEN}'\n", "server.env": "PASSWORD=hunter2hunter2\n",
        "package.json": "{}\n", "node_modules/left-pad/index.js": "module.exports = 1\n",
        "__pycache__/main.cpython-39.pyc": "x", "data/dump bin.sql": 60 * 1024 * 1024, "README.md": "# demo\n",
        "data/app.db": "SQLite format 3\0users"})
code, out = run("--dry-run", "--path", p1)
check("dry-run changes nothing", code == 0 and "Would create" in out and not os.path.exists(os.path.join(p1, ".git")), out)
code, out = run("--path", p1)
print("   " + out.replace("\n", "\n   "))
tracked = git(p1, "ls-files").split()
check("repository created with a commit", code == 0 and "Committed the current state" in out, out)
check("main.py, README.md, .env.example, .gitignore committed", all(f in tracked for f in ("main.py", "README.md", ".env.example", ".gitignore")), tracked)
check(".env and server.env not committed", ".env" not in tracked and "server.env" not in tracked, tracked)
check("config.py with a token left out and reported", "config.py" not in tracked and "config.py: looks like it holds a secret (GitHub token" in out, out)
check("token value never printed", TOKEN not in out)
check("node_modules and __pycache__ not committed", not any(t.startswith(("node_modules", "__pycache__")) for t in git(p1, "ls-files").splitlines()))
check("huge file left out and ignored", "data/dump bin.sql" not in git(p1, "ls-files") and "dump bin.sql: over 50 MB" in out
      and git(p1, "status", "--porcelain").count("dump") == 0, git(p1, "status", "--porcelain"))
check("local database left out and ignored", "data/app.db" not in git(p1, "ls-files") and "app.db: a local database" in out
      and "app.db" not in git(p1, "status", "--porcelain"), out)
check("branch is main", git(p1, "branch", "--show-current").strip() == "main")
code, out = run("--path", p1)
check("second run: already a repository", code == 0 and "Already a git repository" in out, out)
# 2. existing .gitignore gets only what's missing
p2 = os.path.join(SP, "gi_proj2")
mk(p2, {".gitignore": "*.pyc\n", "app.py": "x = 1\n", "node_modules/a.js": "1\n", "package.json": "{}\n"})
code, out = run("--path", p2)
gi = open(os.path.join(p2, ".gitignore")).read()
check("existing .gitignore kept and extended", gi.startswith("*.pyc\n") and "# Added by git-init" in gi and "node_modules/" in gi and ".env" in gi, gi)
check("…without rules it already had", gi.count("*.pyc") == 1, gi)
# 3. refusals
for d in ("~/Downloads", "~/.gemini/antigravity-plus"): os.makedirs(os.path.expanduser(d), exist_ok=True)
for name, path in [("home folder", os.path.expanduser("~")), ("~/Downloads", os.path.expanduser("~/Downloads")),
                   ("hidden folder in home", os.path.expanduser("~/.gemini/antigravity-plus")), ("missing folder", os.path.join(SP, "nope"))]:
    code, out = run("--path", path)
    check(f"refuses: {name}", code == 2 and "Not creating" in out, out)
p3 = os.path.join(SP, "gi_many")
mk(p3, {"a/x.py": "1\n", "b/y.py": "2\n"}); git(os.path.join(p3, "a"), "init", "-q")
code, out = run("--path", p3)
check("refuses: folder of projects (nested repo)", code == 2 and "other git repositories (a)" in out, out)
check("…and changes nothing", not os.path.exists(os.path.join(p3, ".git")) and not os.path.exists(os.path.join(p3, ".gitignore")))
# 4. empty new project
p4 = os.path.join(SP, "gi_empty"); mk(p4, {})
code, out = run("--path", p4)
check("empty folder: repo with .gitignore only", code == 0 and git(p4, "ls-files").split() == [".gitignore"], out)
for d in (p1, p2, p3, p4): shutil.rmtree(d, ignore_errors=True)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
