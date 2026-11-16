"""Run Codex and verify that it produced a report."""

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claude.hooks._common import hooks_home, load_machine


EXIT_HELP = (
    "Exit codes: 0 report written, 1 codex failed or wrong model, "
    "2 quota (including exit 0 with no report), 3 usage, 127 codex missing, 130 interrupted."
)
QUOTA = re.compile(
    r"usage limit|rate limit|quota|insufficient_quota|"
    r"\b(?:http|status|rate|stream error)\b[^\n]*\b429\b|\b429\b\s*[:,-]?\s*too many requests\b",
    re.I,
)
ERROR = re.compile(r"^(?:\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?\s+)?ERROR\b", re.I)
RESET = re.compile(r"(?:try again at|try again in|resets at|reset at)\s+(.+?)(?:\.(?:\s|$)|$)", re.I)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_args(argv):
    parser = ArgumentParser(description=__doc__, epilog=EXIT_HELP)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("exec", "resume"):
        command = commands.add_parser(name, epilog=EXIT_HELP)
        if name == "exec":
            command.add_argument("--repo", required=True, metavar="DIR")
            command.add_argument("--skip-git-repo-check", action="store_true")
        else:
            command.add_argument("session", nargs="?", metavar="SESSION_ID")
            command.add_argument("--last", action="store_true")
        command.add_argument("--prompt", required=True, metavar="FILE")
        command.add_argument("--out", required=True, metavar="FILE")
        command.add_argument("--log", required=True, metavar="FILE")
        command.add_argument("--model", metavar="M")
        command.add_argument("--effort", metavar="E")
        command.add_argument("--sandbox", metavar="MODE")
    args = parser.parse_args(argv)
    if args.command == "resume" and bool(args.session) == args.last:
        parser.error("resume requires either SESSION_ID or --last")
    args.overrides = []
    for name in ("model", "effort", "sandbox"):
        value = getattr(args, name)
        if value is not None:
            args.overrides.extend([f"--{name}", value])
    if args.sandbox is None:
        args.sandbox = "workspace-write"
    return args


def prepare(args):
    machine = load_machine(hooks_home()) or {}
    if args.model is None:
        args.model = machine.get("codex_model")
    if not isinstance(args.model, str) or not args.model.strip():
        raise ValueError("model unresolved, pass --model or configure codex_model")
    if args.effort is None:
        args.effort = machine.get("codex_effort", "high")
    if args.effort is not None and (not isinstance(args.effort, str) or not args.effort):
        raise ValueError("effort must be a non-empty string or null")
    if args.command == "exec" and not Path(args.repo).is_dir():
        raise ValueError(f"repo is not a directory: {args.repo}")
    prompt, out, log = (Path(value).resolve() for value in (args.prompt, args.out, args.log))
    if not prompt.is_file() or prompt.stat().st_size == 0:
        raise ValueError(f"prompt file missing or empty: {args.prompt}")
    paths = (prompt, out, log)
    if out.exists():
        paths += (out.with_name(f"{out.stem}.prev{out.suffix}"),)
    for index, path in enumerate(paths):
        for other in paths[index + 1:]:
            if path == other or (path.exists() and other.exists() and path.samefile(other)):
                raise ValueError("prompt, output and log must be different files")
    out.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    return prompt, out, log


def build_command(args, codex, out):
    command = [codex, "exec"]
    if args.command == "exec":
        command.extend(["-", "-C", args.repo])
    else:
        command.extend(["resume", "--last" if args.last else args.session])
    command.extend(["-m", args.model])
    if args.effort is not None:
        command.extend(["-c", "model_reasoning_effort=" + json.dumps(args.effort)])
    if args.command == "exec":
        command.extend(["--sandbox", args.sandbox])
        if args.skip_git_repo_check:
            command.append("--skip-git-repo-check")
    else:
        command.extend(["-c", "sandbox_mode=" + json.dumps(args.sandbox)])
    command.extend(["--output-last-message", str(out)])
    if args.command == "resume":
        command.append("-")
    return command


def run_codex(command, prompt, log):
    child = subprocess.Popen(command, stdin=prompt, stdout=log, stderr=subprocess.STDOUT)
    try:
        return child.wait()
    except KeyboardInterrupt:
        try:
            child.terminate()
        except OSError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        raise


def read_run(text, prompt_text=None):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    removed_prompt = False
    if prompt_text is not None:
        prompt_text = prompt_text.replace("\r\n", "\n").replace("\r", "\n").rstrip()
        if prompt_text and prompt_text in text:
            text = text.replace(prompt_text, "", 1)
            removed_prompt = True
    header = {}
    separators = 0
    prompt = False
    prompt_seen = False
    quota = False
    reset = "unknown"
    for line in text.splitlines():
        if separators < 2:
            if line == "-" * 8:
                separators += 1
            elif separators == 1:
                key, separator, value = line.partition(":")
                if separator and key in ("session id", "model", "provider"):
                    header[key] = value.strip() or "unknown"
        if not removed_prompt and separators == 2 and line == "user" and not prompt_seen:
            prompt = True
            prompt_seen = True
            continue
        if prompt:
            if line not in ("codex", "exec", "thinking"):
                continue
            prompt = False
        if "rmcp::" in line or not (ERROR.search(line) or "stream error" in line.lower()):
            continue
        if QUOTA.search(line):
            quota = True
            match = RESET.search(line)
            if match and reset == "unknown":
                reset = match.group(1).strip()
    return header, quota, reset


def resume_log(path):
    path = str(path)
    split = max(path.rfind("/"), path.rfind("\\")) + 1
    directory, name = path[:split], Path(path[split:])
    match = re.search(r"-resume(\d+)$", name.stem)
    number = int(match.group(1)) + 1 if match else 1
    stem = name.stem[:match.start()] if match else name.stem
    candidate = f"{directory}{stem}-resume{number}{name.suffix}"
    while Path(candidate).exists():
        number += 1
        candidate = f"{directory}{stem}-resume{number}{name.suffix}"
    return candidate


def report(args, out, log, code, previous=None):
    prompt_text = Path(args.prompt).read_text(encoding="utf-8", errors="replace")
    header, quota, reset = read_run(log.read_text(encoding="utf-8", errors="replace"), prompt_text)
    session, model, provider = (header.get(key, "unknown") for key in ("session id", "model", "provider"))
    print(f"codex_run: session {session}, model {model}, provider {provider}")
    written = out.is_file() and out.stat().st_size > 0
    if code == 0 and written:
        if "model" in header and model.casefold() != args.model.casefold():
            print(f"wrong model: asked {args.model}, got {model}")
            outcome = 1
            next_action = f"read the last 40 lines of {args.log}"
        else:
            with out.open(encoding="utf-8", errors="replace") as handle:
                lines = sum(1 for _ in handle)
            print(f"done, report {args.out} ({lines} lines)")
            if "model" not in header:
                print("note: the log carries no model line; model not confirmed")
            outcome = 0
            next_action = f"read {args.out}, then check CHANGED FILES against git diff --stat and rerun VERIFIED"
        if quota:
            print("note: a quota or rate limit line appeared during the run; codex recovered")
    elif code == 0 or quota:
        print(f"quota exhausted, reset {reset}")
        arguments = [
            "python", Path(__file__).resolve().as_posix(), "resume", "--last" if session == "unknown" else session,
            "--prompt", args.prompt, "--out", args.out, "--log", resume_log(args.log),
        ]
        arguments.extend(args.overrides)
        command = subprocess.list2cmdline(arguments) if sys.platform == "win32" else shlex.join(arguments)
        if session == "unknown":
            print("session id not found in the log; --last resumes the newest session in this folder, check it is this one")
        outcome = 2
        next_action = f"resume after the reset with {command}"
    else:
        print(f"failed, codex exit {code}, see {args.log}")
        outcome = 1
        next_action = f"read the last 40 lines of {args.log}"
    if outcome != 0 and previous is not None:
        print(f"previous report kept at {previous}")
    print(f"next: {next_action}")
    return outcome


def main(argv=None):
    try:
        args = parse_args(argv)
        prompt, out, log = prepare(args)
        codex = shutil.which("codex")
        if codex is None:
            print("codex_run: codex not found on PATH", file=sys.stderr)
            print("next: install codex or fix PATH")
            return 127
        previous = None
        if out.exists():
            previous = out.with_name(f"{out.stem}.prev{out.suffix}")
            out.replace(previous)
        command = build_command(args, codex, out)
        with prompt.open("rb") as source, log.open("wb") as target:
            try:
                code = run_codex(command, source, target)
            except KeyboardInterrupt:
                code = 130
            except FileNotFoundError:
                print("codex_run: codex not found on PATH", file=sys.stderr)
                print("next: install codex or fix PATH")
                return 127
            except OSError as error:
                target.write((f"cannot run codex: {error}\n").encode("utf-8"))
                code = 1
            target.write(f"\nexit={code}\n".encode("ascii"))
        if code == 130:
            return 130
        return report(args, out, log, code, previous)
    except (OSError, ValueError) as error:
        detail = " ".join(str(error).splitlines())
        print(f"codex_run: {detail}", file=sys.stderr)
        print("next: fix the arguments and run again")
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
