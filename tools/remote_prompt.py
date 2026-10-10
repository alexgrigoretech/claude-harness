import argparse
import datetime
import json
import math
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claude.hooks._common import atomic_write_text, file_lock


PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
REMOTE_KEYS = {"ssh", "shell", "cwd", "claude", "permission_mode", "max_turns", "timeout", "model"}
HEADLESS_PREAMBLE = "You run headless through claude -p: the session ends when you finish a turn, and every background task, monitor and child process dies with it. Run long commands in the foreground and wait for them there (a shell loop with sleep is fine; the Bash tool timeout goes up to 600000 ms, so split anything longer into foreground chunks). If a job must outlive the turn, start it detached (setsid nohup on Linux, nohup with disown on macOS), redirect its output to a file, and poll that file in the foreground until it holds the final line. Never use Monitor, run_in_background or an ampersand for something whose result you must report. Reply with the measured results, not a promise to keep waiting."


def validate_remote_block(remote):
    if not isinstance(remote, dict):
        raise ValueError("remote must be an object")
    for name, entry in remote.items():
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name) is None:
            raise ValueError(f"invalid remote name {name!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"remote.{name} must be an object")
        unknown = sorted(set(entry) - REMOTE_KEYS)
        if unknown:
            raise ValueError(f"remote.{name} has unknown keys: {', '.join(unknown)}")
        for key in ("ssh", "shell", "cwd", "claude", "model"):
            if key in entry and (not isinstance(entry[key], str) or not entry[key].strip()):
                raise ValueError(f"remote.{name}.{key} must be a non empty string")
        if "shell" in entry and "ssh" not in entry:
            raise ValueError(f"remote.{name}.shell requires ssh")
        if "permission_mode" in entry and entry["permission_mode"] not in PERMISSION_MODES:
            raise ValueError(f"remote.{name}.permission_mode must be one of {', '.join(PERMISSION_MODES)}")
        for key in ("max_turns", "timeout"):
            if key in entry and (type(entry[key]) is not int or entry[key] <= 0):
                raise ValueError(f"remote.{name}.{key} must be a positive integer")


def positive_integer(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer")
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def positive_budget(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive number")
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def parse_args(arguments):
    parser = argparse.ArgumentParser(description="Run a Claude prompt on a named machine.")
    parser.add_argument("name")
    parser.add_argument("prompt", nargs="*")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--no-preamble", action="store_true")
    parser.add_argument("--cwd")
    parser.add_argument("--permission-mode", choices=PERMISSION_MODES)
    parser.add_argument("--max-turns", type=positive_integer)
    parser.add_argument("--max-budget-usd", type=positive_budget)
    parser.add_argument("--timeout", type=positive_integer)
    session = parser.add_mutually_exclusive_group()
    session.add_argument("--continue", dest="continue_session", action="store_true")
    session.add_argument("--resume")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--detach", action="store_true")
    action.add_argument("--collect", nargs="?", const="")
    action.add_argument("--runs", action="store_true")
    action.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--machine-json", type=Path, default=Path.home() / ".claude/local/machine.json")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_intermixed_args(arguments)
    if args.prompt_file is not None and args.prompt:
        parser.error("use positional prompt words or --prompt-file, never both")
    return args


def load_sessions(home):
    path = home / ".claude/local/remote-sessions.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("machines"), dict):
            raise ValueError("invalid sessions schema")
        for record in value["machines"].values():
            if not isinstance(record, dict):
                raise ValueError("invalid machine record")
            if record.get("session_id") is not None and not isinstance(record["session_id"], str):
                raise ValueError("invalid session id")
            if record.get("run") is not None and not isinstance(record["run"], str):
                raise ValueError("invalid run id")
            if "collected" in record and not isinstance(record["collected"], bool):
                raise ValueError("invalid collected flag")
        return value
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        print(f"remote: warning: unreadable or invalid {path}; replacing sessions data", file=sys.stderr)
    return {"schema": 1, "machines": {}}


def record_session(home, name, cwd, session_id=None, run=None, collected=None):
    path = home / ".claude/local/remote-sessions.json"
    with file_lock(path):
        sessions = load_sessions(home)
        previous = sessions["machines"].get(name, {})
        sessions["machines"][name] = {
            "session_id": session_id if session_id is not None else previous.get("session_id"),
            "at": int(time.time()),
            "cwd": cwd,
            "run": run if run is not None else previous.get("run"),
            "collected": collected if collected is not None else previous.get("collected", not bool(previous.get("run"))),
        }
        atomic_write_text(path, json.dumps(sessions, indent=2) + "\n")


def remote_path(value):
    if value == "~":
        return "~"
    if value.startswith("~/"):
        return "~/" + shlex.quote(value[2:])
    return shlex.quote(value)


def session_flags(args, entry, session_id):
    flags = []
    for key in ("permission_mode", "max_turns", "max_budget_usd"):
        value = getattr(args, key)
        if value is None:
            value = entry.get(key)
        if value is not None:
            flags.extend(["--" + key.replace("_", "-"), str(value)])
    if session_id:
        flags.extend(["--resume", session_id])
    return flags


def claude_flags(args, entry, session_id):
    flags = ["-p", "--output-format", "json", "--model", entry.get("model", "claude-opus-5-5").strip(), *session_flags(args, entry, session_id)]
    if not args.no_preamble:
        flags.extend(["--append-system-prompt", HEADLESS_PREAMBLE])
    return flags


def remote_command(entry, cwd, flags):
    inner = f"cd {remote_path(cwd)} && {remote_path(entry.get('claude', 'claude'))} {shlex.join(flags)}"
    return entry["shell"] + " " + shlex.quote(inner) if "shell" in entry else inner


def ssh_argv(entry, command):
    return ["ssh", "-o", "BatchMode=yes", entry["ssh"], command]


def detach_command(entry, command, run):
    root = "~/.claude/local/remote-runs"
    wrapped = command if "shell" in entry else "sh -c " + shlex.quote(command)
    worker = f"{wrapped} < {root}/{run}.prompt > {root}/{run}.json 2> {root}/{run}.err; echo $? > {root}/{run}.done"
    return (
        f"mkdir -p {root} && cat > {root}/{run}.prompt && "
        f"(nohup sh -c {shlex.quote(worker)} > /dev/null 2>&1 &) && echo {run}"
    )


def parse_result(stdout):
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def result_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if math.isfinite(number) else 0


def report_result(name, result, home, cwd, run=None):
    record_session(home, name, cwd, result.get("session_id"), run, collected=True if run is not None else None)
    answer = result.get("result")
    if answer is not None:
        if not isinstance(answer, str):
            answer = str(answer)
        print(answer, end="" if answer.endswith("\n") else "\n")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    duration = result_number(result.get("duration_ms")) / 1000
    cost = result_number(result.get("total_cost_usd"))
    print(
        f"remote {name}: session {result.get('session_id', '?')}, {result.get('num_turns', '?')} turns, "
        f"{duration:.1f} s, ${cost:.4f}, {result.get('terminal_reason', '?')}",
        file=sys.stderr,
    )
    denials = result.get("permission_denials") or []
    if not isinstance(denials, list):
        denials = [denials]
    if denials:
        print(f"permission denied {len(denials)} times:", file=sys.stderr)
        for denial in denials:
            if not isinstance(denial, dict):
                print(str(denial), file=sys.stderr)
                continue
            parts = []
            if "tool_name" in denial:
                parts.append(str(denial["tool_name"]))
            if "tool_input" in denial:
                parts.append(json.dumps(denial["tool_input"])[:120])
            if "tool_use_id" in denial:
                parts.append(str(denial["tool_use_id"]))
            print(" ".join(parts), file=sys.stderr)
    if result.get("result") is None:
        return 3
    return 1 if result.get("is_error") else 0


def dry_run(argv, prompt):
    print(shlex.join(argv))
    print(f"prompt length: {len(prompt)}")
    return 0


def run_process(argv, prompt, timeout, **kwargs):
    return subprocess.run(argv, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, **kwargs)


def validate_run(run):
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run) is None:
        raise ValueError("invalid run id")


def collect(args, entry, cwd, timeout):
    previous = load_sessions(args.home)["machines"].get(args.name, {})
    run = args.collect or previous.get("run")
    if not run:
        raise ValueError(f"{args.name}: no detached run recorded")
    validate_run(run)
    if "ssh" in entry:
        root = "~/.claude/local/remote-runs/" + run
        argv = ssh_argv(entry, f'cat {root}.json; printf "\\n\\036\\n"; cat {root}.err; printf "\\n\\036\\n"; cat {root}.done 2>/dev/null')
        if args.dry_run:
            return dry_run(argv, "")
        completed = run_process(argv, "", timeout)
        stdout, separator, stderr = completed.stdout.decode("utf-8", errors="replace").partition("\n\x1e\n")
        if completed.returncode and not separator:
            print(completed.stderr.decode("utf-8", errors="replace").strip(), file=sys.stderr)
            return completed.returncode
        stderr, _, done = stderr.partition("\n\x1e\n")
    else:
        root = args.home / ".claude/local/remote-runs" / run
        if args.dry_run:
            return dry_run(["cat", *[str(root.with_suffix(suffix)) for suffix in (".json", ".err", ".done")]], "")
        parts = []
        for suffix in (".json", ".err", ".done"):
            try:
                parts.append(root.with_suffix(suffix).read_text(encoding="utf-8", errors="replace"))
            except FileNotFoundError:
                parts.append("")
        stdout, stderr, done = parts
    result = parse_result(stdout)
    if result is None:
        if done.strip():
            print(f"remote {args.name}: run {run} finished with exit {int(done.strip())} and no JSON result", file=sys.stderr)
            record_session(args.home, args.name, previous.get("cwd", cwd), run=run, collected=True)
        else:
            print(f"remote {args.name}: run {run} still running", file=sys.stderr)
        if stderr:
            print("\n".join(stderr.splitlines()[-20:]), file=sys.stderr)
        return 3 if done.strip() else 4
    return report_result(args.name, result, args.home, previous.get("cwd", cwd), run)


def local_environment():
    environment = os.environ.copy()
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        environment.pop(key, None)
    return environment


def load_entry(args):
    machine = json.loads(args.machine_json.expanduser().read_text(encoding="utf-8-sig"))
    if not isinstance(machine, dict):
        raise ValueError("machine file must be an object")
    remote = machine.get("remote", {})
    validate_remote_block(remote)
    if args.name not in remote:
        raise ValueError(f"unknown machine {args.name}")
    return remote[args.name]


def run_worker(args):
    validate_run(args.worker)
    root = args.home / ".claude/local/remote-runs" / args.worker
    code = 2
    with root.with_suffix(".json").open("wb") as stdout, root.with_suffix(".err").open("wb") as stderr:
        try:
            entry = load_entry(args)
            if "ssh" in entry:
                raise ValueError("worker requires a local entry")
            cwd = str(Path(args.cwd if args.cwd is not None else entry.get("cwd", str(Path.cwd()))).expanduser())
            executable = shutil.which(str(Path(entry.get("claude", "claude")).expanduser()))
            if executable is None:
                raise FileNotFoundError("claude not found on PATH")
            argv = [executable] + claude_flags(args, entry, args.resume)
            with root.with_suffix(".prompt").open("rb") as stdin:
                code = subprocess.run(argv, stdin=stdin, stdout=stdout, stderr=stderr, cwd=cwd, env=local_environment()).returncode
        except (OSError, ValueError) as exc:
            stderr.write(f"remote {args.name}: {exc}\n".encode("utf-8"))
    atomic_write_text(root.with_suffix(".done"), f"{code}\n")
    return code


def execute(args, entry, prompt, timeout):
    if "ssh" in entry and args.cwd is not None and (re.match(r"^[A-Za-z]:", args.cwd) or "\\" in args.cwd):
        print(f"remote {args.name}: --cwd {args.cwd} looks like a local Windows path; from Git Bash prefix the command with MSYS_NO_PATHCONV=1 and quote the value", file=sys.stderr)
        return 2
    cwd = args.cwd if args.cwd is not None else entry.get("cwd", "~" if "ssh" in entry else str(Path.cwd()))
    if "ssh" not in entry:
        cwd = str(Path(cwd).expanduser())
    if args.collect is not None:
        return collect(args, entry, cwd, timeout)
    if args.runs:
        argv = ssh_argv(entry, "ls -1 ~/.claude/local/remote-runs/") if "ssh" in entry else []
        if args.dry_run:
            return dry_run(argv, "")
        previous = load_sessions(args.home)["machines"].get(args.name, {})
        if previous.get("run"):
            print(f"{previous['run']} {previous.get('at', '?')}")
        if argv:
            completed = run_process(argv, "", timeout)
            print(completed.stdout.decode("utf-8", errors="replace"), end="")
            if completed.returncode:
                print(completed.stderr.decode("utf-8", errors="replace").strip(), file=sys.stderr)
            return completed.returncode
        root = args.home / ".claude/local/remote-runs"
        if root.is_dir():
            for path in sorted(root.iterdir(), key=lambda path: path.name):
                print(path.name)
        return 0
    session_id = args.resume
    if args.continue_session:
        previous = load_sessions(args.home)["machines"].get(args.name, {})
        if previous.get("run") and previous.get("collected", False) is False:
            print(f"remote {args.name}: run {previous['run']} not collected yet; run --collect first or pass --resume <id>", file=sys.stderr)
            return 2
        session_id = previous.get("session_id")
        if not session_id:
            raise ValueError(f"{args.name}: no session recorded for --continue")
    flags = claude_flags(args, entry, session_id)
    kwargs = {}
    run = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2) if args.detach else None
    if "ssh" in entry:
        command = remote_command(entry, cwd, flags)
        argv = ssh_argv(entry, detach_command(entry, command, run) if args.detach else command)
    elif args.detach:
        worker_flags = session_flags(args, entry, session_id)
        if args.no_preamble:
            worker_flags.append("--no-preamble")
        argv = [
            sys.executable, str(Path(__file__).resolve()), args.name, "--worker", run,
            "--home", str(args.home.resolve()), "--machine-json", str(args.machine_json.expanduser().resolve()),
            "--cwd", str(Path(cwd).resolve()), *worker_flags,
        ]
        kwargs = {"env": local_environment()}
    else:
        executable = shutil.which(str(Path(entry.get("claude", "claude")).expanduser()))
        if executable is None:
            print(f"remote {args.name}: claude not found on PATH", file=sys.stderr)
            return 2
        argv = [executable] + flags
        kwargs = {"cwd": cwd, "env": local_environment()}
    if args.dry_run:
        return dry_run(argv, prompt)
    if args.detach and "ssh" not in entry:
        root = args.home / ".claude/local/remote-runs"
        root.mkdir(parents=True, exist_ok=True)
        prompt_path = root / (run + ".prompt")
        prompt_path.write_bytes(prompt.encode("utf-8"))
        creationflags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if sys.platform == "win32" else 0
        if creationflags:
            kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    else:
        completed = run_process(argv, prompt, timeout, **kwargs)
        if not args.detach:
            result = parse_result(completed.stdout.decode("utf-8", errors="replace"))
            if result is not None:
                return report_result(args.name, result, args.home, cwd)
        if completed.returncode:
            print(completed.stderr.decode("utf-8", errors="replace").strip(), file=sys.stderr)
            return completed.returncode
        if not args.detach:
            print(f"remote {args.name}: no JSON result on stdout", file=sys.stderr)
            return 3
    record_session(args.home, args.name, cwd, session_id, run, collected=False)
    print(f"remote {args.name}: detached run {run}", file=sys.stderr)
    print(run)
    return 0


def main(arguments=None):
    args = parse_args(arguments)
    timeout = args.timeout or 600
    try:
        args.home = args.home.expanduser()
        if args.worker is not None:
            return run_worker(args)
        entry = load_entry(args)
        timeout = args.timeout or entry.get("timeout", 600)
        if args.prompt_file is not None:
            with args.prompt_file.expanduser().open(encoding="utf-8", newline="") as handle:
                prompt = handle.read()
        else:
            prompt = " ".join(args.prompt)
        if not prompt and args.collect is None and not args.runs:
            raise ValueError("prompt must not be empty")
        return execute(args, entry, prompt, timeout)
    except subprocess.TimeoutExpired:
        print(f"remote {args.name}: timed out after {timeout} s (the run may still be going; use --detach for long prompts)", file=sys.stderr)
        return 124
    except (OSError, ValueError) as exc:
        print(f"remote: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
