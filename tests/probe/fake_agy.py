#!/usr/bin/env python3
"""Fake Antigravity CLI (`agy`) for offline tests of delegate.py's CLI backend (agy_cli.py).

Supports `--version`, `models`, and headless `-p PROMPT` / `--input-format stream-json` with
`--model`, `--output-format stream-json`, `--mode`, `--print-timeout Ns`, `--conversation CID`.
Appends one JSON line `{argv, cwd, prompt}` per headless call to `FAKE_AGY_LOG`, persists per-cid
step/turn counters in `FAKE_AGY_STATE`, and chooses its scenario from a marker word in the prompt.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from typing import Dict, List, Optional, Tuple

MODELS: List[Tuple[str, str]] = [
    ("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)"),
    ("gemini-4-hydrogen-medium", "Gemini 4 Hydrogen (Medium)"),
    ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High)"),
    ("gemini-4-neon-low", "Gemini 4 Neon (Low)"),
    ("gemini-4-neon-medium", "Gemini 4 Neon (Medium)"),
    ("gemini-4-neon-high", "Gemini 4 Neon (High)"),
    ("gemini-pro-orchestrator", "Orchestrator (Carbon)"),
    ("claude-sonnet-5-5-low", "Claude Sonnet 5.5 (Low)"),
]
MODEL_IDS = {m[0] for m in MODELS}
DEFAULT_MODEL = "gemini-4-hydrogen-low"


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def parse_timeout(raw: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", raw or "")
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return val * 60.0
    if unit == "h":
        return val * 3600.0
    return val


def load_state(state_dir: str, cid: str) -> Dict[str, int]:
    if not state_dir:
        return {"step": 0, "turns": 0}
    path = os.path.join(state_dir, f"{cid}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {"step": int(data.get("step") or 0), "turns": int(data.get("turns") or 0)}
    except (OSError, ValueError, TypeError):
        pass
    return {"step": 0, "turns": 0}


def save_state(state_dir: str, cid: str, step: int, turns: int) -> None:
    if not state_dir:
        return
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, f"{cid}.json")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"step": step, "turns": turns}, f)
    os.replace(tmp, path)


def log_call(argv: List[str], cwd: str, prompt: str) -> None:
    log_path = os.environ.get("FAKE_AGY_LOG", "").strip()
    if not log_path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "cwd": cwd, "prompt": prompt}, ensure_ascii=False) + "\n")
        f.flush()


def read_stdin_prompt() -> str:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        msg = ev.get("message") if isinstance(ev, dict) else {}
        content = msg.get("content") if isinstance(msg, dict) else ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return ""


def parse_args(argv: List[str]) -> Dict[str, Optional[str]]:
    opts: Dict[str, Optional[str]] = {
        "prompt": None,
        "model": None,
        "output_format": "text",
        "input_format": "text",
        "mode": None,
        "print_timeout": "0s",
        "conversation": None,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-p", "--print", "--prompt") and i + 1 < len(argv):
            opts["prompt"] = argv[i + 1]
            i += 2
        elif a == "--model" and i + 1 < len(argv):
            opts["model"] = argv[i + 1]
            i += 2
        elif a == "--output-format" and i + 1 < len(argv):
            opts["output_format"] = argv[i + 1]
            i += 2
        elif a == "--input-format" and i + 1 < len(argv):
            opts["input_format"] = argv[i + 1]
            i += 2
        elif a == "--mode" and i + 1 < len(argv):
            opts["mode"] = argv[i + 1]
            i += 2
        elif a == "--print-timeout" and i + 1 < len(argv):
            opts["print_timeout"] = argv[i + 1]
            i += 2
        elif a in ("--conversation", "-r", "--resume") and i + 1 < len(argv):
            opts["conversation"] = argv[i + 1]
            i += 2
        else:
            i += 1
    return opts


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = sys.argv[1:]
    if argv == ["--version"] or "--version" in argv:
        sys.stdout.write("1.5.18\n")
        return 0
    if argv[:1] == ["models"]:
        for mid, label in MODELS:
            sys.stdout.write(f"{mid}\t{label}\n")
        return 0

    opts = parse_args(argv)
    cwd = os.getcwd()
    prompt = read_stdin_prompt() if opts["input_format"] == "stream-json" else (opts["prompt"] or "")
    log_call(argv, cwd, prompt)

    model = opts["model"] or DEFAULT_MODEL
    if model not in MODEL_IDS:
        avail = "\n".join(f"  {label}" for _, label in MODELS)
        err_msg = (
            f'invalid model selection (--model "{model}" --effort ""): '
            f"model {model} is not recognized as a known model or custom model in settings\n"
            f"Available models:\n{avail}"
        )
        emit({
            "event": "result",
            "result": {
                "conversation_id": "",
                "status": "ERROR",
                "response": "",
                "error": err_msg,
                "duration_seconds": 0,
                "num_turns": 0,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "cache_read_tokens": 0,
                    "total_tokens": 0,
                },
            },
        })
        sys.stderr.write(f"error: {err_msg}\n")
        sys.stderr.flush()
        return 1

    if "SCENARIO_CRASH" in prompt:
        sys.stdout.write('{"event":"step_update","step_update":{"step_index":0')
        sys.stdout.flush()
        sys.stderr.write("panic: boom\n")
        sys.stderr.flush()
        return 2

    state_dir = os.environ.get("FAKE_AGY_STATE", "").strip()
    cid = opts["conversation"] or str(uuid.uuid4())
    st = load_state(state_dir, cid)
    step = st["step"]
    turns = st["turns"] + 1

    def next_step() -> int:
        nonlocal step
        cur = step
        step += 1
        return cur

    t0 = time.monotonic()
    emit({
        "event": "init",
        "conversation_id": cid,
        "init": {
            "model": model,
            "cwd": cwd,
            "tools": ["run_command", "write_to_file", "view_file"],
            "permission_mode": "request-review",
        },
    })
    emit({
        "event": "step_update",
        "step_update": {
            "conversation_id": cid,
            "step_index": next_step(),
            "state": "DONE",
            "step_type": "user_input",
        },
    })

    if "SCENARIO_ERROR" in prompt:
        save_state(state_dir, cid, step, turns)
        emit({
            "event": "result",
            "result": {
                "conversation_id": cid,
                "status": "ERROR",
                "response": "",
                "error": "stream broke off",
                "duration_seconds": round(time.monotonic() - t0, 2),
                "num_turns": turns,
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "cache_read_tokens": 0,
                    "total_tokens": 1000,
                },
            },
        })
        return 1

    if "SCENARIO_SLOW" in prompt:
        limit = parse_timeout(opts["print_timeout"] or "") or 3.0
        target = limit + 0.3
        while time.monotonic() - t0 < target:
            time.sleep(0.2)
        save_state(state_dir, cid, step, turns)
        elapsed = round(time.monotonic() - t0, 2)
        emit({
            "event": "result",
            "result": {
                "conversation_id": cid,
                "status": "ERROR",
                "response": "",
                "finished_reason": "print_timeout",
                "error": f"print timeout after {int(limit)}s",
                "duration_seconds": elapsed,
                "num_turns": turns,
                "usage": {
                    "input_tokens": 2000,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "cache_read_tokens": 0,
                    "total_tokens": 2000,
                },
            },
        })
        sys.stderr.write(f"[agy] print timeout after {int(limit)}s\n")
        sys.stderr.flush()
        return 3

    sleep_match = re.search(r"SCENARIO_SLEEP=(\d+(?:\.\d+)?)", prompt)
    if sleep_match:
        secs = float(sleep_match.group(1))
        end_at = time.monotonic() + secs
        while time.monotonic() < end_at:
            time.sleep(min(0.2, max(end_at - time.monotonic(), 0.01)))

    cached = 8000 if turns > 1 else 0
    step_usage = {
        "input_tokens": 12000,
        "output_tokens": 250,
        "thinking_tokens": 50,
        "cache_read_tokens": cached,
        "total_tokens": 12250,
    }

    if "SCENARIO_QUESTION" in prompt:
        wire = json.dumps({"to_claude_code": "question", "id": "abc12345", "text": "Which name?"})
        emit({
            "event": "step_update",
            "step_update": {
                "conversation_id": cid,
                "step_index": next_step(),
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": 'python3 "/x/delegate.py" ask "Which name?"'},
                    "output": f"Command exited with code 0.\n\nCommand output:\n{wire}\n",
                },
            },
        })
        resp = "Waiting for Claude Code's answer."
        emit({
            "event": "step_update",
            "step_update": {
                "conversation_id": cid,
                "step_index": next_step(),
                "state": "DONE",
                "step_type": "agent_response",
                "text_delta": resp,
                "duration_seconds": 0.1,
                "usage": step_usage,
            },
        })
        save_state(state_dir, cid, step, turns)
        emit({
            "event": "result",
            "result": {
                "conversation_id": cid,
                "status": "SUCCESS",
                "response": resp,
                "duration_seconds": round(time.monotonic() - t0, 2),
                "num_turns": turns,
                "usage": step_usage,
            },
        })
        return 0

    if "SCENARIO_DENIED" in prompt:
        emit({
            "event": "step_update",
            "step_update": {
                "conversation_id": cid,
                "step_index": next_step(),
                "state": "ERROR",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "shutdown -h now"},
                    "error": {
                        "type": "TOOL_ERROR",
                        "message": "tool call denied by pre-tool hook: guard says no",
                    },
                },
            },
        })
        resp = "The command was denied by a hook."
        emit({
            "event": "step_update",
            "step_update": {
                "conversation_id": cid,
                "step_index": next_step(),
                "state": "DONE",
                "step_type": "agent_response",
                "text_delta": resp,
                "duration_seconds": 0.1,
                "usage": step_usage,
            },
        })
        save_state(state_dir, cid, step, turns)
        emit({
            "event": "result",
            "result": {
                "conversation_id": cid,
                "status": "SUCCESS",
                "response": resp,
                "denied_actions": [{"action": "command", "display_name": "Bash"}],
                "duration_seconds": round(time.monotonic() - t0, 2),
                "num_turns": turns,
                "usage": step_usage,
            },
        })
        return 0

    if "SCENARIO_HUGE" in prompt:
        huge_step = next_step()
        line_prefix = (
            '{"event":"step_update","step_update":{"conversation_id":'
            + json.dumps(cid)
            + f',"step_index":{huge_step},"state":"ACTIVE","step_type":"agent_response","text_delta":"x"}}\n'
        )
        # Emit 20000 ACTIVE text_delta events in chunks for speed.
        chunk = line_prefix * 500
        for _ in range(40):
            sys.stdout.write(chunk)
        sys.stdout.flush()

    # Default success scenario
    file_match = re.search(r"SCENARIO_FILE=([^\s\"']+)", prompt)
    rel_file = file_match.group(1) if file_match else "hello.txt"
    target_path = os.path.abspath(os.path.join(cwd, rel_file))
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write("OK")

    emit({
        "event": "step_update",
        "step_update": {
            "conversation_id": cid,
            "step_index": next_step(),
            "state": "DONE",
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": {
                "name": "run_command",
                "parameters": {"CommandLine": "python3 -m unittest"},
                "output": "Command exited with code 0.\n\nCommand output:\nRan 3 tests\n\nOK\n",
            },
        },
    })
    emit({
        "event": "step_update",
        "step_update": {
            "conversation_id": cid,
            "step_index": next_step(),
            "state": "DONE",
            "step_type": "tool",
            "tool_name": "write_file",
            "tool_info": {
                "name": "write_file",
                "parameters": {"Path": target_path},
                "output": "Wrote 2 bytes",
            },
        },
    })
    resp = f"✅ Done: {rel_file} written; 3 tests pass."
    emit({
        "event": "step_update",
        "step_update": {
            "conversation_id": cid,
            "step_index": next_step(),
            "state": "DONE",
            "step_type": "agent_response",
            "text_delta": resp,
            "duration_seconds": 0.2,
            "usage": step_usage,
        },
    })
    save_state(state_dir, cid, step, turns)
    emit({
        "event": "result",
        "result": {
            "conversation_id": cid,
            "status": "SUCCESS",
            "response": resp,
            "duration_seconds": round(time.monotonic() - t0, 2),
            "num_turns": turns,
            "usage": step_usage,
        },
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
