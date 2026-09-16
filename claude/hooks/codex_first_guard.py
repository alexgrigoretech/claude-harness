import os
import re
import sys
import time
from pathlib import Path

from _common import append_jsonl, hooks_home, read_payload, utc_timestamp


BLOCKED_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go",
    ".rs", ".java", ".php", ".rb", ".c", ".cc", ".cpp", ".h",
    ".hpp", ".cs", ".kt", ".swift", ".vue", ".svelte", ".scala",
    ".sql", ".ipynb", ".ps1", ".psm1", ".sh", ".yml", ".yaml",
    ".toml",
}


def is_blocked_path(raw_path):
    path = str(raw_path).replace("\\", "/")
    basename = path.rsplit("/", 1)[-1]
    suffix = Path(basename).suffix.lower()
    if suffix not in BLOCKED_EXTENSIONS and not re.match(
        r"^Dockerfile(?:\..+)?$", basename, re.I
    ):
        return False
    exemptions = (
        r"/(?:\.claude|\.codex)/",
        r"AppData/Local/Temp(?:/|$)",
        r"^/tmp/",
        r"^/private/tmp/",
        r"^/var/folders/",
    )
    return not any(re.search(pattern, path, re.I) for pattern in exemptions)


def main():
    payload = read_payload()
    if payload is None:
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path or not is_blocked_path(path):
        return 0

    home = hooks_home()
    marker = home / ".claude" / "direct-edit-ok"
    try:
        fresh = marker.is_file() and time.time() - marker.stat().st_mtime < 1800
    except OSError:
        fresh = False
    if fresh:
        try:
            marker_text = marker.read_text(encoding="utf-8", errors="replace").strip()
            append_jsonl(
                home / ".claude" / "direct-edit-log.jsonl",
                {
                    "ts": utc_timestamp(),
                    "path": str(path),
                    "marker": marker_text[:200],
                    "cwd": payload.get("cwd"),
                },
            )
        except OSError:
            pass
        return 0

    marker_display = str(marker)
    message = (
        "Codex-first rule (global CLAUDE.md): implementation edits to source "
        "code are delegated to Codex CLI, not made directly. Either:\n"
        "1. Delegate: write a brief and run codex exec (see the Code "
        "implementation section of CLAUDE.md), or\n"
        "2. Claim a listed exception (trivial <=10-line single-file edit / "
        "Codex failed after 2 resumes): state which exception applies in your "
        f"reply, then write one line naming the exception into {marker_display} "
        "and retry the edit. The marker stays valid for 30 minutes; its content "
        "is the audit trail.\n"
    )
    sys.stderr.write(message)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
