import os
import re
import subprocess
import sys
from pathlib import Path

from _common import load_machine


ATTRIBUTION_PATTERNS = (
    re.compile(r"^Co-Authored-By:", re.IGNORECASE),
    re.compile(r"^Claude-Session:", re.IGNORECASE),
    re.compile(r"Generated with \[Claude Code\]", re.IGNORECASE),
    re.compile(r"Generated with Claude", re.IGNORECASE),
    re.compile(r"\N{ROBOT FACE}\s*Generated", re.IGNORECASE),
)


def reject_attribution(line_number, line):
    print(
        f'commit-msg-guard: attribution trailer found on line {line_number}: "{line}". '
        "Remove it; no Co-Authored-By, Claude-Session or Generated-with lines on "
        "any commit (global CLAUDE.md).",
        file=sys.stderr,
    )


def skipped(reason):
    print(f"commit-msg-guard: identity check skipped ({reason})", file=sys.stderr)


def git_value(arguments):
    try:
        completed = subprocess.run(
            ["git", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if completed.returncode != 0:
        reason = completed.stderr.strip() or completed.stdout.strip()
        return None, reason or f"git exited {completed.returncode}"
    return completed.stdout.strip(), None


def normalized_path(value, home):
    raw = str(value).replace("\\", "/")
    if raw == "~" or raw.startswith("~/"):
        raw = str(home).replace("\\", "/") + raw[1:]
    path = os.path.abspath(os.path.normpath(raw))
    if os.name == "nt":
        path = os.path.normcase(path)
    return path.replace("\\", "/").rstrip("/")


def expected_email(machine, top_level, home):
    identities = machine.get("identities")
    if not isinstance(identities, dict):
        return None
    top = normalized_path(top_level, home)
    matches = []
    for key, value in identities.items():
        if key == "default" or not isinstance(value, str):
            continue
        prefix = normalized_path(key, home)
        if top == prefix or top.startswith(prefix + "/"):
            matches.append((len(prefix), value))
    if matches:
        return max(matches, key=lambda item: item[0])[1]
    default = identities.get("default")
    return default if isinstance(default, str) else None


def check_message(path):
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    if not any(line.strip() and not line.startswith("#") for line in lines):
        return 0

    machine = load_machine()
    extra_patterns = []
    if machine is not None:
        configured = machine.get("commit_msg_deny_regex")
        if isinstance(configured, list):
            extra_patterns = [
                (str(pattern), re.compile(str(pattern))) for pattern in configured
            ]

    for line_number, line in enumerate(lines, 1):
        if line.startswith("#"):
            continue
        if any(pattern.search(line) for pattern in ATTRIBUTION_PATTERNS):
            reject_attribution(line_number, line)
            return 1
        for name, pattern in extra_patterns:
            if pattern.search(line):
                print(
                    f'commit-msg-guard: denied by machine pattern "{name}" on line '
                    f'{line_number}: "{line}". Remove the matching text.',
                    file=sys.stderr,
                )
                return 1

    if machine is None:
        skipped("machine.json is missing")
        return 0

    actual, error = git_value(["config", "user.email"])
    if error:
        skipped(error)
        return 0
    top_level, error = git_value(["rev-parse", "--show-toplevel"])
    if error:
        skipped(error)
        return 0
    home = Path(os.environ.get("CLAUDE_HOOKS_HOME") or Path.home())
    expected = expected_email(machine, top_level, home)
    if expected is None:
        skipped("no expected identity is configured")
        return 0
    if expected.startswith("TODO"):
        skipped(f"expected identity is {expected}")
        return 0
    if actual != expected:
        print(
            f"commit-msg-guard: author email {actual} but this folder expects {expected}. "
            "Fix: add an includeIf block for this folder to ~/.gitconfig (lane L4 "
            "template in git/), never set a repo-local user.email.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) == 2 and arguments[0] == "--check-only":
        path = arguments[1]
    elif len(arguments) == 1:
        path = arguments[0]
    else:
        print(
            "usage: commit_msg_guard.py [--check-only] MESSAGE_FILE",
            file=sys.stderr,
        )
        return 1
    try:
        return check_message(path)
    except (OSError, ValueError, TypeError, re.error) as exc:
        print(f"commit-msg-guard: cannot check message ({exc})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
