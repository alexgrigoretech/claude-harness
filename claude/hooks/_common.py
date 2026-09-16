# This module must not import sibling modules because installed hooks import it flat as _common while tools import it as claude.hooks._common.
import contextlib
import datetime
import errno
import json
import os
import re
import stat
import sys
import time
from pathlib import Path


ATOMIC_WRITE_ATTEMPTS = 6


def read_payload():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return None
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def hooks_home():
    return Path(os.environ.get("CLAUDE_HOOKS_HOME") or Path.home())


def validate_path_tail(tail):
    if tail.startswith(("/", "\\")) or Path(tail).is_absolute() or re.match(r"^[A-Za-z]:", tail):
        raise ValueError(f"anchored path must have a relative tail: {tail!r}")
    if ".." in re.split(r"[/\\]", tail):
        raise ValueError(f"anchored path must not contain parent segments: {tail!r}")
    return tail


def expand_path(value, home, repo=None):
    home = Path(home)
    if not value:
        raise ValueError("path must not be empty")
    if value.startswith("<repo>"):
        if value != "<repo>" and not value.startswith(("<repo>/", "<repo>\\")):
            raise ValueError(f"invalid repository placeholder: {value!r}")
        if repo is None:
            raise ValueError("<repo> is not available here")
        return Path(repo) / validate_path_tail(value[7:]) if value != "<repo>" else Path(repo)
    if value == "~":
        return home
    if value.startswith(("~/", "~\\")):
        return home / validate_path_tail(value[2:])
    if value.startswith("~"):
        raise ValueError(f"unsupported home form {value!r}, use ~ or ~/")
    root = Path(value)
    if os.name == "nt" and root.drive and not root.root:
        return home / validate_path_tail(value[len(root.drive):])
    if os.name == "nt" and not root.drive and value.startswith(("/", "\\")):
        return home / validate_path_tail(value.lstrip("/\\"))
    # A dot names the home itself, and other relative paths start there.
    return root if root.is_absolute() else home / validate_path_tail(value)


def normalize_session_id(value):
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
            return value
    return None


def nudge_stamp(home, session_id):
    return Path(home) / ".claude" / "local" / "context" / f"{session_id}.nudged"


def nudge_disabled(value):
    return isinstance(value, (int, float, bool)) and value == 0


def emit_context(event_name, text):
    output = {"hookSpecificOutput": {
        "hookEventName": event_name,
        "additionalContext": text,
    }}
    sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")


def hard_wrap_findings(text):
    lines = text.splitlines()
    front_matter_end = -1
    if lines and lines[0].strip() == "-" * 3:
        front_matter_end = 0
        for index in range(1, min(len(lines), 60)):
            if lines[index].strip() == "-" * 3:
                front_matter_end = index
                break
            if lines[index].strip() and not (
                re.match(r"^\s*[A-Za-z_][\w.-]*\s*:", lines[index])
                or lines[index][:1].isspace()
                or lines[index].startswith(("- ", "#"))
            ):
                break
    findings = []
    fence_marker = None
    previous = None
    reported = False
    list_context = False
    for index, line in enumerate(lines):
        if index <= front_matter_end:
            continue
        if not line.strip():
            previous = None
            reported = False
            list_context = False
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        code_line = fence_marker is not None or fence is not None
        if fence_marker is None and fence is not None:
            marker = fence.group(1)
            fence_marker = (marker[0], len(marker))
        elif fence_marker is not None:
            closing = re.match(r"^\s*([`~]+)\s*$", line)
            if (
                closing is not None
                and closing.group(1)[0] == fence_marker[0]
                and len(closing.group(1)) >= fence_marker[1]
            ):
                fence_marker = None
        stripped = line.strip()
        list_marker = re.match(r"^(?:[-*+]|[0-9]+\.)(?:\s|$)", stripped)
        if (
            stripped.startswith("#")
            or fence is not None
            or (stripped and not line[:1].isspace() and not list_marker)
        ):
            list_context = False
        if list_marker and not code_line:
            list_context = True
        skipped = (
            code_line
            or not stripped
            or stripped.startswith(("#", "|", ">", "<"))
            or line.startswith(("    ", "\t"))
            or line.endswith(("  ", "\\"))
            or list_marker
            or (list_context and line[:1].isspace())
            or re.fullmatch(r"(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,}|={3,})", stripped)
            or re.match(r"^\[[^]]+\]:\s*\S", stripped)
        )
        if skipped:
            previous = None
            reported = False
            continue
        if previous is not None and not reported:
            line_number, candidate = previous
            ending = re.sub(r"(?:\[\^[^]]+\])+$", "", candidate.rstrip())
            if (
                30 <= len(candidate.rstrip()) <= 110
                and not re.search(r"[.!?:;][\"')\]*_\u201d\u2019]*$", ending)
                and re.match(r"^[a-z]", stripped)
            ):
                findings.append((line_number, candidate.strip()))
                reported = True
        previous = (index + 1, line)
    return findings


def utc_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# The lock covers read modify write sequences on a shared file; atomic_write_text alone only makes the replace atomic. On POSIX it reopens a removed or replaced lock file before retrying. The lock is not reentrant; a nested lock on the same path in the same process waits for the timeout and raises.
# Discard removes the lock file only when the guarded path is absent, so it is only correct when absence means no owner, as with nudge stamps; a ledger can legitimately be absent and must not use discard.
@contextlib.contextmanager
def file_lock(path, timeout=5.0, *, discard=False):
    path = Path(path)
    lock_path = path.with_name(path.name + ".lock")
    if os.name == "nt":
        import msvcrt
    else:
        import fcntl

    def discard_lock():
        try:
            if discard and not path.exists():
                lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    started = time.monotonic()
    try:
        handle = open(lock_path, "a+b")
    except FileNotFoundError:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
    try:
        handle.seek(0)
        reopens = 0
        while True:
            reopened = False
            try:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK) and not (
                    os.name == "nt" and exc.errno in (errno.EACCES, errno.EDEADLOCK)
                ):
                    raise
            else:
                if os.name == "nt":
                    break
                try:
                    held = os.fstat(handle.fileno())
                    try:
                        current = os.stat(lock_path)
                    except FileNotFoundError:
                        current = None
                    if current is not None and (held.st_ino, held.st_dev) == (current.st_ino, current.st_dev):
                        break
                except BaseException:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    raise
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                handle = open(lock_path, "a+b")
                handle.seek(0)
                reopened = True
                reopens += 1
                if reopens <= 3:
                    continue
            reopens = 0
            if time.monotonic() - started >= timeout:
                if reopened and discard:
                    # A peer may have acquired the fresh inode, so disposal needs one final nonblocking lock attempt.
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        pass
                    else:
                        try:
                            held = os.fstat(handle.fileno())
                            current = os.stat(lock_path)
                            if (held.st_ino, held.st_dev) == (current.st_ino, current.st_dev):
                                discard_lock()
                        except OSError:
                            pass
                        finally:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                raise TimeoutError(f"lock on {path} held by another process")
            time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                handle.close()
                if discard and not path.exists():
                    try:
                        lock_path.unlink(missing_ok=True)
                    except PermissionError:
                        pass
            else:
                try:
                    discard_lock()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def atomic_write_text(path, text):
    path = Path(path)
    if path.is_dir():
        raise PermissionError(f"target is not a writable file: {path}")
    if os.name != "nt" and path.parent.exists() and not os.access(path.parent, os.W_OK):
        raise PermissionError(f"parent directory is not writable: {path.parent}")
    # Windows cannot replace a file with the read-only attribute.
    if os.name == "nt" and path.exists() and path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
        raise PermissionError(f"target has the read-only attribute: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        for attempt in range(1, ATOMIC_WRITE_ATTEMPTS + 1):
            try:
                temporary.replace(path)
                break
            except PermissionError as exc:
                if (
                    getattr(exc, "winerror", None) not in (5, 32, 33)
                    or attempt == ATOMIC_WRITE_ATTEMPTS
                ):
                    raise
                # Callers that read then write a shared file need their own lock.
                time.sleep(0.05 * attempt)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def append_jsonl(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def load_machine(home=None):
    base = Path(home) if home is not None else hooks_home()
    path = base / ".claude" / "local" / "machine.json"
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else None
