"""Adapt stdin prompts to the Kimi Code CLI event protocol."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.muse_llm import _find_on_path, is_client_machine, run_prompt


KIMI_MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
READ_ONLY_AGENT = """---
name: review-reader
description: Read-only reviewer
tools:
  - Read
---
You have one tool, Read. Read the file named in the prompt, then answer with exactly the output format that file asks for and nothing else.
"""


def find_kimi(value=None):
    if value and (os.sep in value or (os.altsep is not None and os.altsep in value)):
        candidate = Path(value).resolve()
        return str(candidate) if candidate.is_file() else None
    executable = _find_on_path(value or "kimi")
    if executable:
        return executable
    if value:
        return None
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        if app_data:
            candidate = Path(app_data) / "npm" / "kimi.cmd"
            if candidate.is_file():
                return str(candidate)
    return None


def parse_events(stdout):
    response = None
    session_id = None
    roles = set()
    types = set()
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        role = value.get("role")
        event_type = value.get("type")
        if isinstance(role, str):
            roles.add(role)
        if isinstance(event_type, str):
            types.add(event_type)
        content = value.get("content")
        if isinstance(content, list):
            content = "".join(
                part["text"]
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
        if role == "assistant" and isinstance(content, str):
            if content or (response is None and "tool_calls" not in value):
                response = content
        if role == "meta" and event_type == "session.resume_hint":
            identifier = value.get("session_id")
            if isinstance(identifier, str):
                session_id = identifier
    return response, session_id, roles, types


def _acquire_session_lock(lock_path):
    deadline = time.monotonic() + 5
    while True:
        try:
            return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("session index lock is held by another process")
            try:
                if time.time() - lock_path.stat().st_mtime > 60:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            time.sleep(min(0.2, remaining))


def cleanup_sessions(work_directory, session_id=None):
    replacement = None
    lock_descriptor = None
    lock_path = None
    warning = None
    try:
        base = Path(os.environ.get("CLAUDE_HOOKS_HOME") or Path.home()) / ".kimi-code"
        index = base / "session_index.jsonl"
        lock_path = index.with_name(index.name + ".lock")
        try:
            lock_descriptor = _acquire_session_lock(lock_path)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError) and not base.exists():
                return
            warning = f"session index lock unavailable, index rewrite skipped: {exc}"
        try:
            with index.open(encoding="utf-8", newline="") as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            return
        expected = os.path.normcase(str(Path(work_directory).resolve()))
        sessions = (base / "sessions").resolve()
        retained = []
        removed = False
        for line in lines:
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                retained.append(line)
                continue
            if not isinstance(entry, dict):
                retained.append(line)
                continue
            session_dir = entry.get("sessionDir")
            if not isinstance(session_dir, str) or not session_dir:
                retained.append(line)
                continue
            try:
                work_dir = entry.get("workDir")
                matches = isinstance(work_dir, str) and os.path.normcase(
                    str(Path(work_dir).resolve())
                ) == expected
                target = Path(session_dir).resolve()
            except (OSError, ValueError):
                retained.append(line)
                continue
            if not matches and not (session_id and entry.get("sessionId") == session_id):
                retained.append(line)
                continue
            if not target.is_relative_to(sessions) or target == sessions:
                retained.append(line)
                continue
            shutil.rmtree(target, ignore_errors=True)
            parent = target.parent
            if parent.name.startswith("wd_") and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
            removed = True
        if removed and lock_descriptor is not None:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=base, delete=False
            ) as handle:
                replacement = Path(handle.name)
                handle.writelines(retained)
            os.replace(replacement, index)
            replacement = None
    except (OSError, ValueError) as exc:
        warning = str(exc)
    finally:
        if replacement is not None:
            try:
                replacement.unlink(missing_ok=True)
            except OSError as exc:
                warning = warning or str(exc)
        if lock_descriptor is not None:
            try:
                os.close(lock_descriptor)
            except OSError as exc:
                warning = warning or str(exc)
            finally:
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError as exc:
                    warning = warning or str(exc)
        if warning is not None:
            print(f"kimi session cleanup warning: {warning}", file=sys.stderr)


def model_id(value):
    if re.fullmatch(KIMI_MODEL_PATTERN, value) is None:
        raise argparse.ArgumentTypeError("model must match [A-Za-z0-9][A-Za-z0-9._/-]*")
    return value


def parser():
    argument_parser = argparse.ArgumentParser(
        description="Send a prompt from stdin through a Read-only Kimi Code agent."
    )
    argument_parser.add_argument("--model", type=model_id, default="kimi-code/kimi-for-coding")
    argument_parser.add_argument("--timeout", type=float, default=540)
    argument_parser.add_argument("--raw", action="store_true")
    argument_parser.add_argument("--kimi", help="path to the kimi executable")
    argument_parser.add_argument("--allow-client", action="store_true")
    argument_parser.add_argument(
        "--no-preamble", action="store_true", help="accepted for interface parity, ignored"
    )
    return argument_parser


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.allow_client:
        try:
            client = is_client_machine()
        except ValueError:
            print("kimi refused: machine.json unreadable, nothing leaves this machine", file=sys.stderr)
            return 8
        if client:
            print("kimi refused: client machine, nothing leaves this machine", file=sys.stderr)
            return 8
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    prompt = sys.stdin.read()
    if not prompt:
        print("prompt on stdin is empty", file=sys.stderr)
        return 2

    executable = find_kimi(args.kimi)
    if not executable:
        print("kimi was not found. Install it with: npm install -g @moonshot-ai/kimi-code", file=sys.stderr)
        return 3
    if args.no_preamble:
        print("kimi: no-preamble is ignored, the Read-only agent needs Read", file=sys.stderr)

    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work_directory:
            session_id = None
            try:
                (Path(work_directory) / "review.txt").write_text(prompt, encoding="utf-8", newline="\n")
                (Path(work_directory) / "agent.md").write_text(READ_ONLY_AGENT, encoding="utf-8", newline="\n")
                command = [
                    executable,
                    "--agent-file",
                    "agent.md",
                    "-m",
                    args.model,
                    "-p",
                    "Read review.txt in the current directory and follow its instructions. Reply with the answer only.",
                    "--output-format",
                    "stream-json",
                ]
                started = time.monotonic()
                completed = run_prompt(command, args.timeout, work_directory)
                duration = time.monotonic() - started
                response, session_id, roles, types = parse_events(completed.stdout)
            finally:
                cleanup_sessions(work_directory, session_id)
    except subprocess.TimeoutExpired:
        print(f"kimi timed out after {args.timeout:g} s", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"kimi could not be started: {exc}", file=sys.stderr)
        return 5

    if response is None:
        print("kimi returned no assistant line", file=sys.stderr)
        print("roles: " + json.dumps(sorted(roles), ensure_ascii=False), file=sys.stderr)
        print("types: " + json.dumps(sorted(types), ensure_ascii=False), file=sys.stderr)
        print(f"stderr: {completed.stderr[:400]}", file=sys.stderr)
        return 5
    if completed.returncode != 0:
        print(f"kimi exited with code {completed.returncode}", file=sys.stderr)
        print(completed.stderr[:400], file=sys.stderr)
        return 7
    if not response.strip():
        print("kimi returned an empty response", file=sys.stderr)
        return 6
    if args.raw:
        sys.stdout.write(response)
        return 0

    envelope = {
        "response": response,
        "usage": {},
        "model": args.model,
        "duration_seconds": duration,
    }
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
