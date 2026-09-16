import re
import sys
from pathlib import Path

from _common import hooks_home, read_payload


HIGH_CONFIDENCE_RULES = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("Azure AccountKey", re.compile(r"AccountKey\s*=\s*[A-Za-z0-9+/=]{40,}")),
    ("Azure shared access signature", re.compile(r"SharedAccessSignature\s*=")),
    ("Slack", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("OpenAI-style key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    (
        "JWT",
        re.compile(
            r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
            r"[A-Za-z0-9_-]{10,}"
        ),
    ),
    (
        "credential-bearing DSN",
        re.compile(
            r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://"
            r"[^:/\s]+:[^@\s]{3,}@",
            re.I,
        ),
    ),
)
GENERIC_ASSIGNMENT = re.compile(
    r"\b(password|passwd|pwd|secret|token|api[_-]?key|client[_-]?secret)"
    r"\s*[:=]\s*([\"'])([^\"'\s]{8,})\2",
    re.I,
)


def placeholder(value):
    lowered = value.lower()
    if value.startswith(("$", "${", "%", "<", "{{")):
        return True
    if lowered in {"changeme", "example", "placeholder", "redacted", "xxx", "todo"}:
        return True
    if lowered.startswith("your-") or "redacted" in lowered:
        return True
    return bool(value) and len(set(value)) == 1


def path_rule(raw_path):
    basename = str(raw_path).replace("\\", "/").rsplit("/", 1)[-1]
    lowered = basename.lower()
    if lowered.startswith(".env") and lowered not in {
        ".env.example", ".env.sample", ".env.template"
    }:
        if lowered == ".env" or lowered.startswith(".env."):
            return ".env file"
    if Path(lowered).suffix in {
        ".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".kdbx"
    }:
        return "credential file extension"
    if lowered in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}:
        return "private key filename"
    if lowered in {".npmrc", ".pypirc", ".netrc"}:
        return "credential configuration filename"
    if lowered == "credentials" or lowered.startswith("credentials."):
        return "credentials filename"
    if re.fullmatch(r"service[-_]?account.*\.json", basename, re.I):
        return "service account filename"
    return None


def exempt_content(raw_path):
    path = str(raw_path).replace("\\", "/")
    home_hooks = str(hooks_home() / ".claude" / "hooks").replace("\\", "/")
    return (
        path.lower().startswith(home_hooks.rstrip("/").lower() + "/")
        or path.lower().endswith("hooks_test.py")
    )


def content_matches(content):
    matches = [name for name, pattern in HIGH_CONFIDENCE_RULES if pattern.search(content)]
    for match in GENERIC_ASSIGNMENT.finditer(content):
        if not placeholder(match.group(3)):
            matches.append("credential assignment")
            break
    return matches


def read_only_command(command):
    stripped = command.strip()
    if re.search(r"[;&|<>]", stripped):
        return False
    return bool(re.match(r"^(?:cat|type|Get-Content)(?:\s|$)", stripped, re.I))


def block(mode, target, matches):
    unique = list(dict.fromkeys(matches))
    sys.stderr.write(
        f"secret-guard: blocked {mode} on {target}. Matched: "
        + "; ".join(unique)
        + ". Move the value to an environment variable or the platform secret "
        "store and reference it. If this is a false positive, say so before "
        "overriding.\n"
    )
    return 2


def main():
    payload = read_payload()
    if payload is None:
        return 0
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    if tool_name in {"Edit", "MultiEdit", "Write", "NotebookEdit"}:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or "unknown path"
        filename_match = path_rule(path)
        if filename_match:
            return block("file write", str(path), [filename_match])
        if exempt_content(path):
            return 0
        parts = [tool_input.get("content", ""), tool_input.get("new_string", "")]
        if tool_name == "MultiEdit" and isinstance(tool_input.get("edits"), list):
            parts.extend(
                edit.get("new_string", "")
                for edit in tool_input["edits"]
                if isinstance(edit, dict)
            )
        content = "\n".join(str(part) for part in parts if isinstance(part, str))
        matches = content_matches(content)
        return block("file write", str(path), matches) if matches else 0

    if tool_name in {"Bash", "PowerShell"}:
        command = tool_input.get("command", "")
        if not isinstance(command, str) or read_only_command(command):
            return 0
        matches = content_matches(command)
        return block("shell command", "shell command", matches) if matches else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
