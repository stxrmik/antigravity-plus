#!/usr/bin/env python3
"""Antigravity Plus — PostToolUse hook: keeps each memory folder's MEMORY.md index in step with its files.

After write_file / edit_file on a memory file (see skills/memory/scripts/memory.py for the folders), appends the
file's index line to MEMORY.md if the index doesn't mention it yet. Append-only: curated index lines (Claude Code
keeps a hand-grouped index) are never rewritten. Output is always {}. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import json
import os
import sys

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(PLUGIN, "skills", "memory", "scripts"))


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        call = payload.get("toolCall") or {}
        args = call.get("args") or {}
        if isinstance(args, str):
            args = json.loads(args)
        path = args.get("Path") or args.get("TargetFile") or args.get("path") or ""
        if isinstance(path, str) and len(path) >= 2 and path[0] == path[-1] == '"':
            path = json.loads(path)  # args can arrive JSON-encoded twice
        if not payload.get("error") and isinstance(path, str) and os.path.basename(path) != "MEMORY.md":
            import memory as memstore
            d = memstore.memory_dir_of(path)
            if d and os.path.isfile(os.path.join(d, os.path.basename(path))):
                memstore.sync_index(d)
    except Exception as e:  # never disturb the conversation
        print(f"memory_sync: {e}", file=sys.stderr)
    sys.stdout.write("{}")


if __name__ == "__main__":
    main()
