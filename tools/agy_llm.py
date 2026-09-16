"""Adapt stdin prompts to the Antigravity CLI stream protocol."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


INSTALL_COMMAND = "irm https://antigravity.google/cli/install.ps1 | iex"
NO_TOOLS_PREAMBLE = (
    "You have no tools in this task. Do not call any tool, do not read files, do not run "
    "commands, do not browse. Answer from the text below alone, in exactly the output "
    "format it asks for."
)


def _find_on_path(executable):
    names = [executable]
    if os.name == "nt":
        extensions = [
            extension
            for extension in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
            if extension
        ]
        if Path(executable).suffix.lower() not in {
            extension.lower() for extension in extensions
        }:
            names = [executable + extension for extension in extensions]
    current_directory = Path.cwd().resolve()
    for value in os.environ.get("PATH", "").split(os.pathsep):
        value = value.strip().strip('"')
        if not value:
            continue
        directory = Path(value)
        if not directory.is_absolute():
            continue
        directory = directory.resolve()
        if directory == current_directory:
            continue
        for name in names:
            candidate = directory / name
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)
    return None


def find_agy(value=None):
    if value and (os.sep in value or (os.altsep is not None and os.altsep in value)):
        candidate = Path(value).resolve()
        return str(candidate) if candidate.is_file() else None
    executable = _find_on_path(value or "agy")
    if executable:
        return executable
    if value:
        return None
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidate = Path(local_app_data) / "agy" / "bin" / "agy.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def parse_result(output):
    result = None
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict) and value.get("event") == "result":
            result = value
    return result


def parser():
    argument_parser = argparse.ArgumentParser(
        description=(
            "Send a prompt from stdin through the Antigravity CLI. "
            "A no-tools preamble is added by default."
        )
    )
    argument_parser.add_argument("--model", default="gemini-3.8-flash-medium")
    argument_parser.add_argument("--timeout", type=float, default=300)
    argument_parser.add_argument("--raw", action="store_true")
    argument_parser.add_argument("--agy", help="path to the agy executable")
    argument_parser.add_argument(
        "--no-preamble",
        action="store_true",
        help="send the prompt without the default no-tools preamble",
    )
    return argument_parser


def main(argv=None):
    args = parser().parse_args(argv)
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    prompt = sys.stdin.read()
    if not prompt:
        print("prompt on stdin is empty", file=sys.stderr)
        return 2

    executable = find_agy(args.agy)
    if not executable:
        print(f"agy was not found. Install it with: {INSTALL_COMMAND}", file=sys.stderr)
        return 3

    command = [
        executable,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model",
        args.model,
        "--sandbox",
        "-p=",
    ]
    content = prompt if args.no_preamble else NO_TOOLS_PREAMBLE + "\n\n" + prompt
    user_event = {"event": "user", "message": {"content": content}}
    input_text = json.dumps(user_event, ensure_ascii=False) + "\n"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work_directory:
            completed = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=args.timeout,
                cwd=work_directory,
            )
    except subprocess.TimeoutExpired:
        print(f"agy timed out after {args.timeout:g} s", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"agy could not be started: {exc}", file=sys.stderr)
        return 5

    result_event = parse_result(completed.stdout)
    if result_event is None:
        print("agy returned no result event", file=sys.stderr)
        print(f"stdout: {completed.stdout[:400]}", file=sys.stderr)
        print(f"stderr: {completed.stderr[:400]}", file=sys.stderr)
        return 5

    if completed.returncode != 0:
        print(f"agy exited with code {completed.returncode}", file=sys.stderr)
        print(completed.stderr[:400], file=sys.stderr)
        return 7

    result = result_event.get("result")
    if not isinstance(result, dict):
        result = {}
    denied_actions = result.get("denied_actions")
    if not isinstance(denied_actions, list):
        denied_actions = []
    if result.get("status") != "SUCCESS":
        error = result.get("error")
        if not isinstance(error, str) or not error:
            error = f"agy failed with status {result.get('status')}"
        print(error, file=sys.stderr)
        return 6
    response = result.get("response")
    if not isinstance(response, str) or not response.strip():
        print("agy returned an empty response", file=sys.stderr)
        if denied_actions:
            print(
                "denied actions: "
                + json.dumps(denied_actions, ensure_ascii=False, separators=(",", ":")),
                file=sys.stderr,
            )
        return 6
    if denied_actions:
        print(
            "warning: agy denied actions: "
            + json.dumps(denied_actions, ensure_ascii=False, separators=(",", ":")),
            file=sys.stderr,
        )
    if args.raw:
        sys.stdout.write(response)
        return 0

    usage = result.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    duration = result.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        duration = float(duration)
    else:
        duration = None
    envelope = {
        "response": response,
        "usage": usage,
        "model": args.model,
        "duration_seconds": duration,
    }
    if denied_actions:
        envelope["denied_actions"] = denied_actions
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
