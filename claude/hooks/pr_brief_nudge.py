import re
import sys

from _common import emit_context, hooks_home, read_payload


PR_URL = re.compile(
    r"https://[^/\s\"'<>]+/(?:[^/\s\"'<>?#]+/)+"
    r"(?P<repo>[^/\s\"'<>?#]+)/(?:pull|-/merge_requests)/(?P<number>[0-9]+)"
)
CREATION_GATE = re.compile(r"(?:pr|mr)\s+create|merge_request\.create|merge_requests")
GH_TOOL = r"(?:gh(?:\.exe)?|ghx\.py)"
GH_OPTIONS = r"(?:\s+-\S+(?:\s+(?:\"[^\"]*\"|'[^']*'|[^\s-]\S*))?)*"
GH_CREATE = re.compile(
    r"(?:^|[\s(=`])(?:\"(?:[^\"\r\n]*[/\\])?" + GH_TOOL + r"\""
    r"|'(?:[^'\r\n]*[/\\])?" + GH_TOOL + r"'"
    r"|(?:[^\s\"'()=`]*[/\\])?" + GH_TOOL + r")"
    + GH_OPTIONS + r"\s+pr\s+create\b"
)


def is_creation(command):
    for part in re.split(r"[;\n|]|&&", command):
        if re.match(r"\s*(?:grep|echo|cat|sed|Select-String)\b", part, re.IGNORECASE):
            continue
        if re.search(r"-{2}help\b", part):
            continue
        if GH_CREATE.search(part) or re.search(r"\bglab\s+mr\s+create\b", part):
            return True
        if re.search(r"\bgit\s+push\b", part) and re.search(
            r"(?:-o\s*|-{2}push-option(?:=|\s+))['\"]?merge_request\.create\b", part,
        ):
            return True
        if (
            re.search(r"\b(?:curl|Invoke-RestMethod|Invoke-WebRequest)\b", part, re.IGNORECASE)
            and re.search(r"https?://[^\s\"'?]+/merge_requests(?=[\"'\s?]|$)", part)
            and re.search(r"(?:-X\s*POST\b|-{2}request\s+POST\b|-Method\s+Post\b|-{2}(?:data|form)\b|-[dF]\s)", part, re.IGNORECASE)
        ):
            return True
    return False


def main():
    try:
        payload = read_payload()
        if payload is None or payload.get("agent_id") or payload.get("tool_name") not in ("Bash", "PowerShell"):
            return 0
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str) or not CREATION_GATE.search(command):
            return 0
        if not is_creation(command):
            return 0
        response = payload.get("tool_response")
        if isinstance(response, dict):
            response = "\n".join(response[key] for key in ("stdout", "stderr", "output", "content") if isinstance(response.get(key), str))
        if not isinstance(response, str):
            return 0
        match = PR_URL.search(response)
        if match is None:
            return 0
        home = hooks_home()
        if not (home / ".claude" / "skills" / "pr-brief" / "SKILL.md").is_file():
            return 0
        filename = f"{match['repo']}-pr{match['number']}.pdf"
        if (home / ".claude" / "plans" / "pr-briefs" / filename).exists():
            return 0
        emit_context("PostToolUse", (
            f"A pull request was opened: {match.group()}. Run the pr-brief skill for it now and write "
            f"{filename} to ~/.claude/plans/pr-briefs/, print the PDF path, then continue the task from where it was without asking. "
            "The user did not type this; it comes from the pr_brief_nudge hook."
        ))
    except Exception as exc:
        try:
            sys.stderr.write("pr-brief-nudge: " + " ".join(str(exc).splitlines()) + "\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
