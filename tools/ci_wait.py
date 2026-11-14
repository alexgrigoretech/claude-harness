"""Wait for a pull request's checks to settle."""

import argparse
import json
import math
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path


EXIT_CODES = {
    "passed": 0,
    "failed": 1,
    "timed out while pending": 2,
    "gh errors": 3,
    "no checks": 5,
    "usage": 6,
    "gh missing": 127,
    "interrupted": 130,
}


def non_negative_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return seconds


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return seconds


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Exit codes: " + ", ".join(f"{code} {name}" for name, code in EXIT_CODES.items()),
    )
    parser.add_argument("pr", nargs="?", metavar="PR")
    parser.add_argument("--repo", metavar="OWNER/REPO")
    parser.add_argument("--user", metavar="LOGIN")
    parser.add_argument("--cwd", metavar="DIR")
    parser.add_argument("--timeout", type=non_negative_seconds, default=3600, metavar="SECONDS")
    parser.add_argument("--interval", type=positive_seconds, default=30, metavar="SECONDS")
    parser.add_argument("--grace", type=non_negative_seconds, default=90, metavar="SECONDS")
    return parser.parse_args(argv)


def check_name(check, names):
    name = check["name"]
    if names[name] > 1:
        return f"{check['workflow']} / {name}"
    return name


def summary(checks, elapsed):
    buckets = Counter(check["bucket"] for check in checks)
    print(f"{buckets['pass']} passed, {buckets['fail'] + buckets['cancel']} failed, "
          f"{buckets['pending']} pending, {buckets['skipping']} skipped after {elapsed:g}s", flush=True)
    names = Counter(check["name"] for check in checks)
    for check in checks:
        if check["bucket"] in ("fail", "cancel"):
            print(f"{check_name(check, names)}: {check['link']}", flush=True)


def no_checks(elapsed):
    print(f"ci_wait: no checks reported after {elapsed:g}s", flush=True)
    return EXIT_CODES["no checks"]


def wait_for_checks(args, command, cwd, clock=time.monotonic, sleep=time.sleep):
    started = clock()
    checks = []
    previous = {}
    first_poll = True
    errors = 0
    while True:
        try:
            result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", check=False)
        except FileNotFoundError:
            print("ci_wait: gh not found on PATH", file=sys.stderr)
            return EXIT_CODES["gh missing"]
        try:
            parsed = json.loads(result.stdout)
        except (ValueError, TypeError):
            parsed = None
        if not isinstance(parsed, list) and "no checks reported" in result.stderr.lower():
            parsed = []
        if first_poll:
            count = len(parsed) if isinstance(parsed, list) else 0
            print(f"waiting on {count} checks for {args.pr or 'current branch'}", flush=True)
            first_poll = False
        elapsed = clock() - started
        if isinstance(parsed, list):
            errors = 0
            checks = parsed
            names = Counter(check["name"] for check in checks)
            occurrences = Counter()
            current = {}
            for check in checks:
                identity = (check["workflow"], check["name"])
                occurrences[identity] += 1
                key = (*identity, occurrences[identity])
                bucket = check["bucket"]
                if key in previous and previous[key] != bucket:
                    print(f"{check_name(check, names)}: {previous[key]} -> {bucket}", flush=True)
                current[key] = bucket
            previous = current
            if any(check["bucket"] in ("fail", "cancel") for check in checks):
                summary(checks, elapsed)
                return EXIT_CODES["failed"]
            if checks and all(check["bucket"] != "pending" for check in checks):
                summary(checks, elapsed)
                return EXIT_CODES["passed"]
            if not checks and elapsed >= args.grace:
                return no_checks(args.grace)
        else:
            errors += 1
            lines = result.stderr.splitlines()
            detail = lines[0] if lines else ""
            print(f"ci_wait: gh failed (exit {result.returncode}): {detail}", file=sys.stderr)
            if errors >= 3:
                return EXIT_CODES["gh errors"]
        remaining = args.timeout - (clock() - started)
        if remaining <= 0:
            elapsed = clock() - started
            if not checks:
                return no_checks(elapsed)
            summary(checks, elapsed)
            return EXIT_CODES["timed out while pending"]
        sleep(min(args.interval, remaining))


def main(argv=None, clock=time.monotonic, sleep=time.sleep):
    try:
        args = parse_args(argv)
        ghx = Path(__file__).resolve().with_name("ghx.py")
        if ghx.is_file():
            command = [sys.executable, str(ghx)]
            if args.user:
                command.extend(["--user", args.user])
            if args.cwd:
                command.extend(["--cwd", args.cwd])
            cwd = None
        else:
            if args.user:
                print("ci_wait: --user needs ghx.py next to this tool", file=sys.stderr)
                return EXIT_CODES["usage"]
            command = ["gh"]
            cwd = args.cwd
        command.extend(["pr", "checks"])
        if args.pr is not None:
            command.append(args.pr)
        if args.repo:
            command.extend(["--repo", args.repo])
        command.extend(["--json", "name,state,bucket,link,workflow"])
        return wait_for_checks(args, command, cwd, clock=clock, sleep=sleep)
    except KeyboardInterrupt:
        return EXIT_CODES["interrupted"]


if __name__ == "__main__":
    sys.exit(main())
