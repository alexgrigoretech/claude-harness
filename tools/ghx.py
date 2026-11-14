"""Run gh with the stored token for the working folder's account."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claude.hooks._common import hooks_home, load_machine


USAGE = "usage: python tools/ghx.py [--user LOGIN] [--cwd DIR] [--print-user] <gh arguments...>"


def parse_arguments(arguments):
    options = {"--user": None, "--cwd": None}
    print_user = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        option, separator, value = argument.partition("=")
        if argument == "--":
            index += 1
            break
        if argument == "--print-user":
            print_user = True
        elif option in options:
            if not separator:
                index += 1
                value = arguments[index] if index < len(arguments) else ""
            if not value or (not separator and value.startswith("--")):
                raise ValueError("missing option value")
            options[option] = value
        else:
            break
        index += 1
    return options["--user"], options["--cwd"], print_user, arguments[index:]


def normalize_folder(value, home):
    value = str(value).replace("\\", "/")
    if value == "~" or value.startswith("~/"):
        value = home.resolve().as_posix() + value[1:]
    value = os.path.abspath(os.path.normpath(value)).replace("\\", "/").rstrip("/")
    return value.lower() if os.name == "nt" else value


def resolve_user(machine, cwd, home):
    accounts = machine.get("gh_accounts") or {}
    current = normalize_folder(cwd, home)
    matches = []
    for prefix, login in accounts.items():
        if prefix == "default":
            continue
        normalized = normalize_folder(prefix, home)
        if current == normalized or current.startswith(normalized + "/"):
            matches.append((len(normalized), normalized, login))
    if matches:
        return max(matches)[2]
    return accounts.get("default", machine.get("gh_account"))


def main(arguments=None):
    try:
        user, cwd, print_user, gh_arguments = parse_arguments(
            sys.argv[1:] if arguments is None else arguments
        )
    except ValueError:
        print(USAGE, file=sys.stderr)
        return 2
    if cwd is not None and not Path(cwd).is_dir():
        print(f"ghx: no such folder: {cwd}", file=sys.stderr)
        return 2
    if not gh_arguments and not print_user:
        print(USAGE, file=sys.stderr)
        return 2

    home = hooks_home()
    if user is None:
        try:
            user = resolve_user(load_machine(home) or {}, cwd or os.getcwd(), home)
        except (OSError, ValueError, TypeError, AttributeError):
            print("ghx: cannot read account configuration", file=sys.stderr)
            return 2
    if print_user:
        print(user or "active")
        return 0

    gh = shutil.which("gh")
    if gh is None:
        print("ghx: gh not found on PATH", file=sys.stderr)
        return 127
    environment = None
    if user:
        token_arguments = [gh, "auth", "token", "--user", user]
        host = os.environ.get("GH_HOST", "")
        if host:
            token_arguments.extend(["--hostname", host])
        try:
            completed = subprocess.run(
                token_arguments, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10, check=False,
            )
            token = completed.stdout.strip() if not completed.returncode else ""
        except FileNotFoundError:
            print("ghx: gh not found on PATH", file=sys.stderr)
            return 127
        except subprocess.TimeoutExpired:
            print(f"ghx: gh auth token timed out for {user}; the credential store did not answer in 10 seconds", file=sys.stderr)
            return 4
        except (OSError, subprocess.SubprocessError):
            token = ""
        if not token:
            print(f"ghx: no stored token for {user}; run gh auth login for that account", file=sys.stderr)
            return 4
        environment = os.environ.copy()
        if host and host.lower() != "github.com" and not host.lower().endswith(".ghe.com"):
            environment.pop("GITHUB_ENTERPRISE_TOKEN", None)
            environment["GH_ENTERPRISE_TOKEN"] = token
        else:
            environment.pop("GITHUB_TOKEN", None)
            environment["GH_TOKEN"] = token
    try:
        return subprocess.run([gh, *gh_arguments], cwd=cwd, env=environment, check=False).returncode
    except FileNotFoundError:
        print("ghx: gh not found on PATH", file=sys.stderr)
        return 127
    except OSError:
        print("ghx: cannot run gh", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
