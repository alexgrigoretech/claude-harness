import re
import sys
import time
from pathlib import Path

from _common import append_jsonl, read_payload, hard_wrap_findings, hooks_home, utc_timestamp


def splice_text(text, spans, start, end, replacement, edit_number):
    shift = len(replacement) - (end - start)
    updated = []
    for left, right, owner in spans:
        if left < start:
            updated.append((left, min(right, start), owner))
        if right > end:
            updated.append((max(left, end) + shift, right + shift, owner))
    if replacement:
        updated.append((start, start + len(replacement), edit_number))
    return text[:start] + replacement + text[end:], updated


def edited_findings(path, edits):
    limit = 2 * 1024 * 1024
    try:
        if path.stat().st_size >= limit:
            return []
        with path.open("rb") as handle:
            content = handle.read(limit)
        if len(content) >= limit:
            return []
        text = content.decode("utf-8", errors="replace").replace("\r\n", "\n")
    except FileNotFoundError:
        text = None
    except (OSError, ValueError):
        return []
    spans = []
    for edit_number, edit in enumerate(edits, 1):
        replacement = edit["new_string"].replace("\r\n", "\n")
        if text is None:
            text = replacement
            spans = [(0, len(text), edit_number)] if text else []
            continue
        original = edit.get("old_string")
        if not isinstance(original, str):
            return []
        original = original.replace("\r\n", "\n")
        start = text.find(original)
        if start < 0:
            return []
        positions = [start]
        if edit.get("replace_all") and original:
            start = text.find(original, start + len(original))
            while start >= 0:
                positions.append(start)
                start = text.find(original, start + len(original))
        for start in reversed(positions):
            text, spans = splice_text(text, spans, start, start + len(original), replacement, edit_number)
    if text is None:
        return []
    line_spans = []
    for start, end, edit_number in spans:
        first = len(text[:start].splitlines()) + (not text[:start] or text[:start].endswith(("\n", "\r")))
        last = len(text[:end].splitlines())
        line_spans.append((first, last, edit_number))
    findings = []
    for line_number, excerpt in hard_wrap_findings(text):
        owners = [owner for first, last, owner in line_spans if first <= line_number <= last]
        if owners:
            findings.append((line_number, excerpt, max(owners)))
    return findings


def main():
    payload = read_payload()
    if payload is None:
        return 0
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if tool_name not in ("Write", "Edit", "MultiEdit") or not isinstance(tool_input, dict):
        return 0
    raw_path = tool_input.get("file_path")
    if not isinstance(raw_path, str) or not raw_path:
        return 0
    path = Path(raw_path.replace("\\", "/"))
    if path.suffix.lower() not in {".md", ".txt"}:
        return 0
    if (
        ".git" in path.parts
        or path.name.startswith(("COMMIT_EDITMSG", "MERGE_MSG"))
        or re.match(r"^commit(?:[._-]|$)", path.name.lower())
        or path.name.lower().startswith(("merge_msg", "merge-msg"))
        or path.name == "CHANGELOG.md"
        or "/node_modules/" in "/" + path.as_posix()
        or "/.venv/" in "/" + path.as_posix()
    ):
        return 0
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or any(
            not isinstance(edit, dict) or not isinstance(edit.get("new_string"), str)
            for edit in edits
        ):
            return 0
    else:
        text = tool_input.get("content" if tool_name == "Write" else "new_string")
        if not isinstance(text, str):
            return 0
        edits = [{"new_string": text, "old_string": tool_input.get("old_string"),
                  "replace_all": tool_input.get("replace_all")}]
    if tool_name == "Write":
        detected = [(line, excerpt, 1) for line, excerpt in hard_wrap_findings(text)]
    else:
        cwd = payload.get("cwd")
        if not path.is_absolute() and isinstance(cwd, str):
            path = Path(cwd) / path
        detected = edited_findings(path, edits)
    findings = []
    for line_number, excerpt, edit_number in detected:
        location = f"line {line_number}"
        if tool_name == "MultiEdit":
            location = f"edit {edit_number}, " + location
        findings.append((location, excerpt))
    if not findings:
        return 0
    home = hooks_home()
    marker = home / ".claude" / "wrap-ok"
    try:
        fresh = marker.is_file() and time.time() - marker.stat().st_mtime < 1800
    except OSError:
        fresh = False
    if fresh:
        try:
            marker_text = marker.read_text(encoding="utf-8", errors="replace").strip()
            append_jsonl(
                home / ".claude" / "wrap-ok-log.jsonl",
                {
                    "ts": utc_timestamp(),
                    "path": raw_path,
                    "marker": marker_text[:200],
                    "cwd": payload.get("cwd"),
                    "guard": "draft_wrap_guard",
                    "findings": len(findings),
                },
            )
        except OSError:
            pass
        return 0
    location, excerpt = findings[0]
    sys.stderr.write(
        f"draft_wrap_guard: {raw_path} looks hard-wrapped at {location} "
        f"({excerpt}). Write each paragraph as one line; "
        "only commit bodies wrap at 72 columns."
        " To write wrapped text on purpose, put one line saying why into "
        "~/.claude/wrap-ok (valid 30 minutes).\n"
    )
    if len(findings) > 1:
        sys.stderr.write(f"draft_wrap_guard: and {len(findings) - 1} more paragraphs\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
