"""Adapt stdin prompts to the Muse Code CLI event protocol."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


INSTALL_COMMAND = (
    "Windows: irm https://dev.meta.ai/install.ps1 | iex\n"
    "macOS or Linux: curl -fsSL https://dev.meta.ai/install.sh | bash"
)
MUSE_MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
MUSE_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
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


def find_muse(value=None):
    if value and (os.sep in value or (os.altsep is not None and os.altsep in value)):
        candidate = Path(value).resolve()
        return str(candidate) if candidate.is_file() else None
    executable = _find_on_path(value or "muse")
    if executable:
        return executable
    if value:
        return None
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidate = Path(local_app_data) / "Programs" / "muse" / "muse.cmd"
            if candidate.is_file():
                return str(candidate)
    return None


def parse_events(stdout):
    model = None
    chunks = []
    terminal = None
    payload_types = set()
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        payload_type = value.get("payload_type")
        if isinstance(payload_type, str):
            payload_types.add(payload_type)
        payload = value.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload_type == "run.model.configured":
            model_id = payload.get("model_id")
            if isinstance(model_id, str) and model_id:
                model = model_id
        elif payload_type == "run.output.delta":
            text = payload.get("text")
            if isinstance(text, str):
                chunks.append(text)
        elif isinstance(payload_type, str) and payload_type.startswith("run.terminal."):
            terminal = payload
    return model, "".join(chunks), terminal, payload_types


def is_client_machine():
    base = Path(os.environ.get("CLAUDE_HOOKS_HOME") or Path.home())
    machine_path = base / ".claude" / "local" / "machine.json"
    try:
        with machine_path.open(encoding="utf-8-sig") as handle:
            machine = json.load(handle)
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("machine.json unreadable") from exc
    if not isinstance(machine, dict):
        raise ValueError("machine.json unreadable")
    return machine.get("client") is True


def model_id(value):
    if re.fullmatch(MUSE_MODEL_PATTERN, value) is None:
        raise argparse.ArgumentTypeError("model must match [A-Za-z0-9][A-Za-z0-9._-]*")
    return value


def positive_integer(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def parser():
    argument_parser = argparse.ArgumentParser(
        description=(
            "Send a prompt from stdin through the Muse Code CLI. "
            "A no-tools preamble is added by default."
        )
    )
    argument_parser.add_argument("--model", type=model_id, default="muse-wonder-full-1.0")
    argument_parser.add_argument(
        "--effort", default="medium", choices=MUSE_EFFORTS
    )
    argument_parser.add_argument("--max-steps", type=positive_integer, default=4)
    argument_parser.add_argument("--timeout", type=float, default=240)
    argument_parser.add_argument("--raw", action="store_true")
    argument_parser.add_argument("--muse", help="path to the muse executable")
    argument_parser.add_argument("--allow-client", action="store_true")
    argument_parser.add_argument(
        "--no-preamble",
        action="store_true",
        help="send the prompt without the default no-tools preamble",
    )
    return argument_parser


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.allow_client:
        try:
            client = is_client_machine()
        except ValueError:
            print("muse refused: machine.json unreadable, nothing leaves this machine", file=sys.stderr)
            return 8
        if client:
            print("muse refused: client machine, nothing leaves this machine", file=sys.stderr)
            return 8
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    prompt = sys.stdin.read()
    if not prompt:
        print("prompt on stdin is empty", file=sys.stderr)
        return 2

    executable = find_muse(args.muse)
    if not executable:
        print(f"muse was not found. Install it with:\n{INSTALL_COMMAND}", file=sys.stderr)
        return 3

    content = prompt if args.no_preamble else NO_TOOLS_PREAMBLE + "\n\n" + prompt
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work_directory:
            prompt_file = Path(work_directory) / "prompt.txt"
            prompt_file.write_text(content, encoding="utf-8")
            command = [
                executable,
                "exec",
                "--json",
                "--model",
                args.model,
                "--reasoning-effort",
                args.effort,
                "--approval-mode",
                "never",
                "--contributor-mode",
                "off",
                "--no-foreign-personal-context",
                "--no-session-log",
                "--disable-web-tools",
                "--disable-reminders",
                "--disable-request-user-input",
                "--disable-shell",
                "--disable-write",
                "--max-model-steps",
                str(args.max_steps),
                "--prompt-file",
                str(prompt_file),
            ]
            started = time.monotonic()
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=args.timeout,
                cwd=work_directory,
            )
            duration = time.monotonic() - started
    except subprocess.TimeoutExpired:
        print(f"muse timed out after {args.timeout:g} s", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"muse could not be started: {exc}", file=sys.stderr)
        return 5

    model, fallback, terminal, payload_types = parse_events(completed.stdout)
    if terminal is None:
        print("muse returned no run.terminal record", file=sys.stderr)
        print("payload types: " + json.dumps(sorted(payload_types), ensure_ascii=False), file=sys.stderr)
        print(f"stderr: {completed.stderr[:400]}", file=sys.stderr)
        return 5
    if terminal.get("terminal") != "completed":
        print(
            f"muse run ended with terminal {terminal.get('terminal')}: {terminal.get('reason')}",
            file=sys.stderr,
        )
        return 6
    if completed.returncode != 0:
        print(f"muse exited with code {completed.returncode}", file=sys.stderr)
        print(completed.stderr[:400], file=sys.stderr)
        return 7
    response = terminal.get("text")
    if not isinstance(response, str) or not response.strip():
        response = fallback
    if not response.strip():
        print("muse returned an empty response", file=sys.stderr)
        return 6
    if args.raw:
        sys.stdout.write(response)
        return 0

    envelope = {
        "response": response,
        "usage": {},
        "model": model or args.model,
        "duration_seconds": duration,
    }
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
