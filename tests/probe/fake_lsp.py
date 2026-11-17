#!/usr/bin/env python3
"""A small language server for the offline tests of skills/lsp (tests/test_lsp.py): speaks LSP over stdio about a
toy Python, from the files on disk and the documents the client opened — like a real server, an open document wins
over the disk, so a client that doesn't sync gets stale answers.

Definitions are `def`/`class` lines; references are whole words outside comments; files under a folder named
`unresolved` are invisible (like code a real server can't resolve). Diagnostics: `oops` is an error, `TODO` a
warning. Like pyright, it says «Found N source files» in its log FAKE_LSP_READY_DELAY seconds after `initialized`,
and until then answers references from open documents only. FAKE_LSP_PULL=1 → pull diagnostics instead of push;
FAKE_LSP_PUBLISH_DELAY → seconds before a publish (0.05), FAKE_LSP_PUBLISH_JITTER → up to that many more, at random
(so publishes for consecutive versions can arrive out of order, as with a real server); FAKE_LSP_PROGRESS=1 → an indexing progress that never
ends; FAKE_LSP_FLOOD=1 → on a message over 100 KB, before reading its body, it asks the client something and writes a
megabyte of log (a client that answers from its reader while another thread writes would hang here).
FAKE_LSP_LOG=<file> → every method it receives, one per line. Stdlib only.
"""
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from pathlib import Path

ROOT = ""
DOCS = {}
DOCS_LOCK = threading.Lock()
READY = threading.Event()
LOCK = threading.Lock()
PULL = os.environ.get("FAKE_LSP_PULL") == "1"
LOG = os.environ.get("FAKE_LSP_LOG", "")
WORD = re.compile(r"[A-Za-z_]\w*")


def uri_of(path):
    return Path(path).as_uri()


def path_of(uri):
    p = urllib.parse.unquote(urllib.parse.urlparse(uri).path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", p):
        p = p[1:]
    return os.path.realpath(os.path.normpath(p))


def u16(line, index):
    return len(line[:index].encode("utf-16-le")) // 2


def cp(line, units):
    n = 0
    for i, ch in enumerate(line):
        if n >= units:
            return i
        n += 2 if ord(ch) > 0xFFFF else 1
    return len(line)


def send(msg):
    data = json.dumps(msg).encode("utf-8")
    with LOCK:
        sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        sys.stdout.buffer.flush()


def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length is not None:
                break
            continue
        k, _, v = line.partition(b":")
        if k.strip().lower() == b"content-length":
            length = int(v)
    if length > 100000 and os.environ.get("FAKE_LSP_FLOOD") == "1":
        send({"jsonrpc": "2.0", "id": "flood-1", "method": "window/workDoneProgress/create", "params": {"token": "f"}})
        for i in range(300):
            send({"jsonrpc": "2.0", "method": "window/logMessage", "params": {"type": 4, "message": f"{i} " + "x" * 4000}})
    return json.loads(sys.stdin.buffer.read(length))


def files(open_only=False):
    out = {}
    if not open_only:
        for d, dirs, names in os.walk(ROOT):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x != "unresolved"]
            for n in names:
                if n.endswith(".py"):
                    p = os.path.realpath(os.path.join(d, n))
                    with open(p, encoding="utf-8") as f:
                        out[p] = f.read()
    with DOCS_LOCK:
        docs = list(DOCS.items())
    for p, text in docs:
        if "unresolved" not in p.split(os.sep):
            out[p] = text
    return out


def lines(text):
    return re.split(r"\r\n|\r|\n", text)


def word_at(path, pos):
    text = files().get(path, "")
    ls = lines(text)
    if pos["line"] >= len(ls):
        return None
    line = ls[pos["line"]]
    i = cp(line, pos["character"])
    for m in WORD.finditer(line):
        if m.start() <= i <= m.end():
            return m.group(0)
    return None


def definitions(name):
    out = []
    for p, text in files().items():
        for n, line in enumerate(lines(text)):
            m = re.match(r"^(\s*)(def|class)\s+(\w+)", line)
            if m and m.group(3) == name:
                out.append((p, n, line.index(name, m.start(3)), line))
    return out


def symbols(path):
    """[(name, kind, start, end, sel_line, sel_char, parent)] for classes, methods and functions."""
    ls = lines(files().get(path, ""))
    found = []
    for n, line in enumerate(ls):
        m = re.match(r"^(\s*)(def|class)\s+(\w+)", line)
        if m:
            found.append([m.group(3), 5 if m.group(2) == "class" else 12, n, n, n, u16(line, m.start(3)), len(m.group(1)), None])
    for i, s in enumerate(found):
        end = len(ls) - 1
        for t in found[i + 1:]:
            if t[6] <= s[6]:
                end = t[2] - 1
                break
        while end > s[2] and not ls[end].strip():
            end -= 1
        s[3] = end
        for t in reversed(found[:i]):
            if t[6] < s[6] and t[2] < s[2] <= t[3]:
                s[7] = t[0]
                if t[1] == 5:
                    s[1] = 6  # a def inside a class: a method
                break
    return found


def enclosing(path, line_no):
    best = None
    for s in symbols(path):
        if s[1] in (6, 12) and s[2] <= line_no <= s[3]:
            best = s
    return best


def references(name, include_decl, open_only=False):
    out = []
    for p, text in files(open_only).items():
        for n, line in enumerate(lines(text)):
            code = line.split("#", 1)[0]
            for m in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", code):
                is_decl = re.match(r"^\s*(def|class)\s+" + re.escape(name) + r"\b", line) is not None
                if is_decl and not include_decl:
                    continue
                out.append({"uri": uri_of(p), "range": {"start": {"line": n, "character": u16(line, m.start())},
                                                        "end": {"line": n, "character": u16(line, m.end())}}})
    return out


def item(path, s):
    return {"name": s[0], "kind": s[1], "detail": s[7] or "", "uri": uri_of(path),
            "range": {"start": {"line": s[2], "character": 0}, "end": {"line": s[3], "character": 0}},
            "selectionRange": {"start": {"line": s[4], "character": s[5]},
                               "end": {"line": s[4], "character": s[5] + len(s[0])}}}


def diagnostics(path):
    out = []
    with DOCS_LOCK:
        doc_text = DOCS.get(path)
    text = doc_text if doc_text is not None else files().get(path, "")
    for n, line in enumerate(lines(text)):
        for m in re.finditer(r"\boops\b", line):
            out.append({"range": {"start": {"line": n, "character": u16(line, m.start())},
                                  "end": {"line": n, "character": u16(line, m.end())}},
                        "severity": 1, "code": "reportUndefinedVariable", "message": '"oops" is not defined'})
        if "TODO" in line:
            out.append({"range": {"start": {"line": n, "character": 0}, "end": {"line": n, "character": 1}},
                        "severity": 2, "message": "a TODO is left"})
    return out


def publish(path, version):
    if not PULL:
        found = diagnostics(path)  # for this version of the text, sent a little later
        delay = float(os.environ.get("FAKE_LSP_PUBLISH_DELAY") or 0.05) + random.uniform(
            0, float(os.environ.get("FAKE_LSP_PUBLISH_JITTER") or 0))
        threading.Timer(delay, send, args=({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                                            "params": {"uri": uri_of(path), "version": version,
                                                       "diagnostics": found}},)).start()


def handle(msg):
    global ROOT
    method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if LOG and method:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(method + "\n")
    if method == "initialize":
        ROOT = path_of(params["rootUri"])
        caps = {"definitionProvider": True, "referencesProvider": True, "hoverProvider": True,
                "documentSymbolProvider": True, "workspaceSymbolProvider": True, "callHierarchyProvider": True,
                "renameProvider": {"prepareProvider": True}, "textDocumentSync": 1}
        if PULL:
            caps["diagnosticProvider"] = {"interFileDependencies": False, "workspaceDiagnostics": False}
        return {"capabilities": caps, "serverInfo": {"name": "fake", "version": "1"}}
    if method == "initialized":
        def ready():
            time.sleep(float(os.environ.get("FAKE_LSP_READY_DELAY") or 0.2))
            READY.set()
            send({"jsonrpc": "2.0", "method": "window/logMessage",
                  "params": {"type": 3, "message": f"Found {len(files())} source files"}})
        threading.Thread(target=ready, daemon=True).start()
        if os.environ.get("FAKE_LSP_PROGRESS") == "1":
            send({"jsonrpc": "2.0", "id": "progress-1", "method": "window/workDoneProgress/create", "params": {"token": "idx"}})
            send({"jsonrpc": "2.0", "method": "$/progress", "params": {"token": "idx", "value": {"kind": "begin", "title": "Indexing"}}})
        send({"jsonrpc": "2.0", "id": "cfg-1", "method": "workspace/configuration",
              "params": {"items": [{"section": "python"}]}})
        return None
    if method == "textDocument/didOpen":
        td = params["textDocument"]
        with DOCS_LOCK:
            DOCS[path_of(td["uri"])] = td["text"]
        publish(path_of(td["uri"]), td.get("version"))
        return None
    if method == "textDocument/didChange":
        p = path_of(params["textDocument"]["uri"])
        with DOCS_LOCK:
            DOCS[p] = params["contentChanges"][-1]["text"]
        publish(p, params["textDocument"].get("version"))
        return None
    if method == "textDocument/didClose":
        with DOCS_LOCK:
            DOCS.pop(path_of(params["textDocument"]["uri"]), None)
        return None
    if method in ("workspace/didChangeWatchedFiles", "workspace/didChangeConfiguration", "$/cancelRequest", "exit"):
        if method == "exit":
            sys.exit(0)
        return None
    if method == "shutdown":
        return None
    if rid is None:
        return None
    td = params.get("textDocument") or {}
    path = path_of(td["uri"]) if td.get("uri") else ""
    if method == "textDocument/definition":
        name = word_at(path, params["position"])
        return [{"uri": uri_of(p), "range": {"start": {"line": n, "character": u16(line, c)},
                                             "end": {"line": n, "character": u16(line, c + len(name))}}}
                for p, n, c, line in definitions(name)] if name else None
    if method == "textDocument/references":
        name = word_at(path, params["position"])
        return references(name, params["context"]["includeDeclaration"], open_only=not READY.is_set()) if name else []
    if method == "textDocument/hover":
        name = word_at(path, params["position"])
        d = definitions(name) if name else []
        return {"contents": {"kind": "markdown", "value": f"```python\n{d[0][3].strip()}\n```\nA toy symbol."}} if d else None
    if method == "textDocument/documentSymbol":
        tree, by_name = [], {}
        for s in symbols(path):
            node = {"name": s[0], "kind": s[1], "range": item(path, s)["range"],
                    "selectionRange": item(path, s)["selectionRange"], "children": []}
            (by_name[s[7]]["children"] if s[7] in by_name else tree).append(node)
            by_name[s[0]] = node
        return tree
    if method == "workspace/symbol":
        q = params.get("query", "").lower()
        out = []
        for p in files():
            for s in symbols(p):
                if q in s[0].lower():
                    out.append({"name": s[0], "kind": s[1], "containerName": s[7] or "",
                                "location": {"uri": uri_of(p), "range": item(p, s)["selectionRange"]}})
        return out
    if method == "textDocument/prepareCallHierarchy":
        name = word_at(path, params["position"])
        for p, n, _c, _line in definitions(name) if name else []:
            for s in symbols(p):
                if s[2] == n:
                    return [item(p, s)]
        return None
    if method == "callHierarchy/incomingCalls":
        name = params["item"]["name"]
        calls = {}
        for ref in references(name, False):
            p = path_of(ref["uri"])
            line = lines(files()[p])[ref["range"]["start"]["line"]]
            if not re.search(re.escape(name) + r"\s*\(", line):
                continue
            s = enclosing(p, ref["range"]["start"]["line"])
            if s:
                calls.setdefault((p, s[0]), [item(p, s), []])[1].append(ref["range"])
        return [{"from": it, "fromRanges": rs} for it, rs in calls.values()]
    if method == "callHierarchy/outgoingCalls":
        it = params["item"]
        p = path_of(it["uri"])
        ls = lines(files()[p])
        out = {}
        for n in range(it["range"]["start"]["line"] + 1, it["range"]["end"]["line"] + 1):
            for m in re.finditer(r"(\w+)\s*\(", ls[n]):
                for dp, dn, _c, _l in definitions(m.group(1)):
                    for s in symbols(dp):
                        if s[2] == dn:
                            out.setdefault((dp, s[0]), [item(dp, s), []])[1].append(
                                {"start": {"line": n, "character": u16(ls[n], m.start(1))},
                                 "end": {"line": n, "character": u16(ls[n], m.end(1))}})
        return [{"to": t, "fromRanges": rs} for t, rs in out.values()]
    if method == "textDocument/prepareRename":
        name = word_at(path, params["position"])
        return {"placeholder": name} if name else None
    if method == "textDocument/rename":
        name = word_at(path, params["position"])
        changes = {}
        for ref in references(name, True):
            changes.setdefault(ref["uri"], []).append({"range": ref["range"], "newText": params["newName"]})
        return {"changes": changes}
    if method == "textDocument/diagnostic":
        return {"kind": "full", "items": diagnostics(path)}
    raise KeyError(method)


def main():
    while True:
        msg = read()
        if msg is None:
            return
        if "method" not in msg:
            continue  # the answer to our workspace/configuration
        try:
            result = handle(msg)
            if "id" in msg:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except KeyError as e:
            if "id" in msg:
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"unknown {e}"}})


if __name__ == "__main__":
    main()
