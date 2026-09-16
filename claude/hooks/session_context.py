import contextlib
import os
import platform
import re
import subprocess
import time
from glob import glob
from pathlib import Path

from _common import emit_context, file_lock, hooks_home, load_machine, normalize_session_id, nudge_stamp, read_payload


def reason(exc):
    text = str(exc).strip().replace("\r", " ").replace("\n", " ")
    return text[:160] or exc.__class__.__name__


def git_run(cwd, arguments):
    completed = subprocess.run(
        ["git"] + arguments,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=2,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(message or f"git exited {completed.returncode}")
    return completed.stdout.strip()


def process_context():
    own = {os.getpid(), os.getppid()}
    counts = {"codex": 0, "claude": 0, "node": 0}
    if platform.system().lower() == "windows":
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "tasklist failed")
        for line in completed.stdout.splitlines():
            fields = line.strip().strip('"').split('\",\"')
            if len(fields) < 2:
                continue
            try:
                pid = int(fields[1])
            except ValueError:
                continue
            if pid in own:
                continue
            image = fields[0].lower()
            if image.startswith("codex"):
                counts["codex"] += 1
            elif image.startswith("claude"):
                counts["claude"] += 1
            elif image == "node.exe":
                counts["node"] += 1
    else:
        completed = subprocess.run(
            ["ps", "-eo", "pid,comm,args"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "ps failed")
        for line in completed.stdout.splitlines()[1:]:
            fields = line.strip().split(None, 2)
            if len(fields) < 2:
                continue
            try:
                pid = int(fields[0])
            except ValueError:
                continue
            if pid in own:
                continue
            text = " ".join(fields[1:]).lower()
            if "codex" in text:
                counts["codex"] += 1
            elif "claude" in text:
                counts["claude"] += 1
    present = [f"{name}: {count}" for name, count in counts.items() if count]
    return ", ".join(present) if present else "none"


def expected_identity(machine, cwd, home):
    identities = machine.get("identities")
    if not isinstance(identities, dict):
        return None
    cwd_norm = str(Path(cwd).resolve()).replace("\\", "/").rstrip("/").lower()
    choices = []
    for key, value in identities.items():
        if key == "default" or not isinstance(value, str):
            continue
        expanded = os.path.expanduser(str(key))
        if str(key).startswith("~"):
            expanded = str(home) + str(key)[1:]
        prefix = expanded.replace("\\", "/").rstrip("/").lower()
        if cwd_norm == prefix or cwd_norm.startswith(prefix + "/"):
            choices.append((len(prefix), value))
    if choices:
        return max(choices)[1]
    default = identities.get("default")
    return default if isinstance(default, str) else None


def health_context(health):
    label = str(health.get("label") or "health")
    if health.get("type") != "newest_log":
        raise ValueError(f"unsupported type {health.get('type')}")
    paths = [Path(path) for path in glob(str(health.get("glob", "")))]
    files = [path for path in paths if path.is_file()]
    if not files:
        return f"{label}: no log found"
    newest = max(files, key=lambda path: path.stat().st_mtime)
    age = max(0, int((time.time() - newest.stat().st_mtime) / 3600))
    content = newest.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(str(health.get("error_regex", "")), re.I)
    errors = sum(1 for line in content.splitlines() if pattern.search(line))
    marker = str(health.get("done_marker", ""))
    completion = "done marker present" if marker and marker in content else "no completion marker"
    return (
        f"{label}: {newest.name} ({age}h ago), {errors} error lines, "
        f"{completion}"
    )


def build_context(payload):
    lines = []
    try:
        cwd = str(payload.get("cwd") or os.getcwd())
        lines.append(f"cwd: {cwd}")
    except Exception as exc:
        cwd = os.getcwd()
        lines.append(f"cwd: unavailable ({reason(exc)})")

    is_repo = False
    try:
        is_repo = git_run(cwd, ["rev-parse", "--is-inside-work-tree"]) == "true"
        if is_repo:
            lines.append(f"branch: {git_run(cwd, ['rev-parse', '--abbrev-ref', 'HEAD'])}")
        else:
            lines.append("branch: not a git repo")
    except Exception as exc:
        message = reason(exc).lower()
        if "not a git repository" in message:
            lines.append("branch: not a git repo")
        else:
            lines.append(f"branch: unavailable ({reason(exc)})")

    if is_repo:
        try:
            try:
                output = git_run(
                    cwd,
                    [
                        "reflog", "-3", "--date=relative",
                        "--format=%h %gd %gs (%cr)",
                    ],
                )
            except Exception:
                output = git_run(cwd, ["reflog", "-3"])
            lines.append("reflog: " + " | ".join(output.splitlines()))
        except Exception as exc:
            lines.append(f"reflog: unavailable ({reason(exc)})")
        try:
            dirty = git_run(cwd, ["status", "--porcelain"])
            count = len(dirty.splitlines()) if dirty else 0
            lines.append(f"dirty: {count} files")
        except Exception as exc:
            lines.append(f"dirty: unavailable ({reason(exc)})")

    try:
        lines.append(f"agents: {process_context()}")
    except Exception as exc:
        lines.append(f"agents: unavailable ({reason(exc)})")

    newest = None
    handoff_failed = False
    try:
        handoffs = [path for path in Path(cwd).glob("HANDOFF-*.md") if path.is_file()]
        if handoffs:
            newest = max(handoffs, key=lambda path: path.stat().st_mtime)
            lines.append(f"handoff: {newest}")
        else:
            lines.append("handoff: none")
    except Exception as exc:
        handoff_failed = True
        lines.append(f"handoff: unavailable ({reason(exc)})")

    home = hooks_home()
    if payload.get("source") == "compact":
        if handoff_failed:
            lines.insert(0, "compacted: the context was just compacted; the handoff lookup failed, check the working folder for HANDOFF files before continuing")
        elif newest is not None:
            lines.insert(0, f"compacted: the context was just compacted; read {newest} before continuing, it is the newest handoff in this folder")
        else:
            lines.insert(0, "compacted: the context was just compacted and no handoff exists in this folder; the pre-compaction detail is gone, rely on the summary")
        try:
            session_id = normalize_session_id(payload.get("session_id"))
            if session_id is not None:
                stamp = nudge_stamp(home, session_id)
                with contextlib.ExitStack() as stack:
                    try:
                        stack.enter_context(file_lock(stamp, timeout=2.0, discard=True))
                    except OSError:
                        pass
                    stamp.unlink(missing_ok=True)
                    try:
                        stamp.with_name(stamp.name + ".write-error").unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception:
            pass
    try:
        identity = git_run(cwd, ["config", "user.email"]) if is_repo else "unknown"
        try:
            machine = load_machine(home)
            if machine is None:
                expected = "no machine.json"
                mismatch = ""
            else:
                expected = expected_identity(machine, cwd, home) or "unknown"
                mismatch = " MISMATCH" if identity != "unknown" and expected != "unknown" and identity != expected else ""
            lines.append(f"identity: {identity}; expected: {expected}{mismatch}")
        except Exception as exc:
            lines.append(f"identity: {identity}; expected: unavailable ({reason(exc)})")
    except Exception as exc:
        lines.append(f"identity: unavailable ({reason(exc)})")

    try:
        machine = load_machine(home)
        health = machine.get("health") if machine else None
        if health is not None:
            if not isinstance(health, dict):
                raise ValueError("health is not an object")
            lines.append(f"health: {health_context(health)}")
    except Exception as exc:
        lines.append(f"health: unavailable ({reason(exc)})")
    return "\n".join(lines)


def main():
    payload = read_payload()
    if payload is None:
        return 0
    try:
        context = build_context(payload)
    except Exception as exc:
        context = f"session context: unavailable ({reason(exc)})"
    emit_context("SessionStart", context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
