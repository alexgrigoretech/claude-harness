import contextlib
import json
import math
import os
import stat
import sys
import time
from pathlib import Path

from _common import atomic_write_text, emit_context, file_lock, hooks_home, load_machine, normalize_session_id, nudge_disabled, nudge_stamp, read_payload, utc_timestamp


MAX_TRANSCRIPT_WINDOW = 67108864


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def save_threshold(value):
    if nudge_disabled(value):
        return 0
    return value if finite_number(value) and value > 0 else 150000


def usage_tokens(line):
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("type") != "assistant" or record.get("isSidechain"):
        return None
    message = record.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    values = [usage.get(key, 0) for key in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens",
    )]
    if all(finite_number(value) for value in values):
        tokens = sum(values)
        if finite_number(tokens) and tokens != 0:
            return tokens
    return None


def transcript_tokens(path):
    if not isinstance(path, str) or not Path(path).is_file():
        return None
    with Path(path).open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        window = 262144
        while True:
            offset = max(0, size - window)
            handle.seek(offset)
            lines = handle.read(window).decode("utf-8", errors="replace").split("\n")
            if offset:
                lines = lines[1:]
            for line in reversed(lines):
                tokens = usage_tokens(line)
                if tokens is not None:
                    return tokens
            if offset == 0:
                return None
            if window == MAX_TRANSCRIPT_WINDOW:
                # Stream the whole file at the window cap so older records are still checked.
                handle.seek(0)
                latest = None
                for line in handle:
                    tokens = usage_tokens(line.decode("utf-8", errors="replace"))
                    if tokens is not None:
                        latest = tokens
                return latest
            window = min(window * 2, MAX_TRANSCRIPT_WINDOW)


def read_stamp(stamp):
    try:
        modified = stamp.stat().st_mtime
        fields = []
        try:
            fields = stamp.read_text(encoding="utf-8").split()
            tokens = int(fields[0])
        except (ValueError, IndexError):
            tokens = 0
        state = "saved" if len(fields) >= 3 and fields[2] == "saved" else "nudged"
        return modified, tokens, state
    except OSError:
        return None


def is_save(payload, home):
    if payload.get("tool_name") not in ("Write", "Edit", "MultiEdit"):
        return False
    tool_input = payload.get("tool_input")
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(path, str) or not path:
        return False
    path = path.replace("\\", "/")
    if "handoff" in path.rsplit("/", 1)[-1].lower():
        return True
    if path.startswith("~/"):
        path = home / path[2:]
    elif not os.path.isabs(path) and isinstance(payload.get("cwd"), str):
        path = Path(payload["cwd"]) / path
    path = Path(os.path.abspath(path))
    plans = Path(os.path.abspath(home / ".claude" / "plans"))
    return path != plans and path.is_relative_to(plans)


def diagnose(exc):
    try:
        sys.stderr.write("context-save-nudge: " + " ".join(str(exc).splitlines()) + "\n")
    except Exception:
        pass


def write_stamp(stamp, tokens, state):
    try:
        atomic_write_text(stamp, f"{int(tokens)} {utc_timestamp()} {state}\n")
    except OSError as exc:
        diagnose_stamp_error(stamp, exc, ".write-error")
        return False
    try:
        stamp.with_name(stamp.name + ".write-error").unlink(missing_ok=True)
    except OSError:
        pass
    return True


def housekeeping(directory):
    try:
        cutoff = time.time() - 7 * 24 * 60 * 60
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.name.endswith((".nudged", ".nudged.lock", ".nudged.lock-error", ".nudged.write-error")):
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                            lock_file = entry.name.endswith((".nudged.lock", ".nudged.lock-error", ".nudged.write-error"))
                            suffix = Path(entry.name).suffix
                            stamp_path = Path(entry.path[:-len(suffix)]) if lock_file else Path(entry.path)
                            with contextlib.ExitStack() as stack:
                                try:
                                    stack.enter_context(file_lock(stamp_path, timeout=0, discard=True))
                                except TimeoutError:
                                    continue
                                except OSError:
                                    if lock_file and not stamp_path.exists():
                                        current = Path(entry.path).stat(follow_symlinks=False)
                                        if stat.S_ISREG(current.st_mode) and current.st_mtime < cutoff:
                                            os.unlink(entry.path)
                                    continue
                                if lock_file and stamp_path.exists():
                                    continue
                                if not lock_file or suffix in (".lock-error", ".write-error"):
                                    current = Path(entry.path).stat(follow_symlinks=False)
                                    if stat.S_ISREG(current.st_mode) and current.st_mtime < cutoff:
                                        os.unlink(entry.path)
                except Exception:
                    pass
    except Exception:
        pass


def needs_tokens(previous, saving):
    if previous is None:
        return not saving
    modified, _, state = previous
    return saving or state != "nudged" or time.time() - modified >= 900


def sweep_if_due(directory):
    marker = directory / ".swept"
    try:
        try:
            age = time.time() - marker.stat().st_mtime
            if 0 <= age < 24 * 60 * 60:
                return
        except FileNotFoundError:
            pass
        housekeeping(directory)
        directory.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass


def diagnose_stamp_error(stamp, exc, suffix):
    marker = stamp.with_name(stamp.name + suffix)
    value = str(exc.errno)
    try:
        if marker.read_text(encoding="utf-8") == value:
            return False
    except OSError:
        pass
    diagnose(exc)
    try:
        atomic_write_text(marker, value)
    except OSError:
        # If the marker cannot be written, ENOSPC and EROFS degrade to one diagnostic per tool call.
        return False
    return True


def stamp_action(previous, saving, tokens, threshold):
    if not needs_tokens(previous, saving):
        return None
    if previous is None:
        return "nudged" if tokens >= threshold else None
    _, stamp_tokens, state = previous
    if saving:
        return "saved"
    if tokens < threshold / 2:
        return "remove"
    if state == "saved" and tokens < stamp_tokens + threshold:
        return None
    return "nudged" if tokens >= threshold else None


def decide(stamp, payload, home, threshold):
    previous = read_stamp(stamp)
    saving = is_save(payload, home)
    if not needs_tokens(previous, saving) and not saving:
        return None
    tokens = transcript_tokens(payload.get("transcript_path"))
    if tokens is None or (not saving and stamp_action(previous, saving, tokens, threshold) is None):
        return None
    context = None
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(stamp, timeout=2.0, discard=True))
        except TimeoutError:
            return None
        except OSError as exc:
            diagnose_stamp_error(stamp, exc, ".lock-error")
            return None
        try:
            stamp.with_name(stamp.name + ".lock-error").unlink(missing_ok=True)
        except OSError:
            pass
        current = read_stamp(stamp)
        if previous is not None and current is None:
            return None
        action = stamp_action(current, saving, tokens, threshold)
        if action is None:
            return None
        if action == "remove":
            stamp.unlink(missing_ok=True)
            try:
                stamp.with_name(stamp.name + ".write-error").unlink(missing_ok=True)
            except OSError:
                pass
        else:
            announce = write_stamp(stamp, tokens, action)
            if action == "nudged" and announce:
                context = (
                    f"Context is at {int(tokens)} tokens, past the {int(threshold)} token save threshold. "
                    "Run the save skill with the handoff trigger now: write HANDOFF-YYYY-MM-DD-<topic>.md "
                    "in the working folder with the Write tool (the hook recognises the save by that write), "
                    "then continue the task from where it was without asking. "
                    "The user did not type this; it comes from the context_save_nudge hook."
                )
    housekeeping(stamp.parent)
    return context


def main():
    try:
        payload = read_payload()
        if payload is None or payload.get("agent_id"):
            return 0
        session_id = normalize_session_id(payload.get("session_id"))
        if session_id is None:
            return 0
        home = hooks_home()
        threshold = save_threshold((load_machine(home) or {}).get("context_save_at"))
        stamp = nudge_stamp(home, session_id)
        sweep_if_due(stamp.parent)
        if threshold == 0:
            return 0
        context = decide(stamp, payload, home, threshold)
        if context is not None:
            emit_context("PostToolUse", context)
    except Exception as exc:
        diagnose(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
