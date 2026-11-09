import argparse
import os
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

try:
    from claude.hooks._common import hard_wrap_findings
except Exception:
    hard_wrap_findings = None


HARD_WRAP_UNAVAILABLE = "check-writing: hard-wrap rule needs claude/hooks/_common.py next to this checker"


AI_PHRASES = (
    "delve",
    "delves",
    "delving",
    "leverage",
    "leveraging",
    "it's not just",
    "it is not just",
    "not only ... but also",
    "in today's fast-paced",
    "game-changer",
    "game changer",
    "seamless",
    "seamlessly",
    "robust",
    "cutting-edge",
    "at the end of the day",
    "furthermore",
    "moreover",
    "in conclusion",
    "it's worth noting",
    "it is worth noting",
    "dive into",
    "deep dive",
    "navigate the",
    "landscape",
    "tapestry",
    "testament to",
    "elevate",
    "empower",
    "harness the power",
    "let's explore",
    "embark",
    "a journey",
    "unlock",
    "unleash",
    "revolutionize",
    "streamline",
    "synergy",
    "paradigm",
    "holistic",
    "ever-evolving",
    "in the realm of",
    "crucial",
    "pivotal",
    "vital role",
    "plays a key role",
    "it's important to note",
    "as an AI",
    "I hope this helps",
    "certainly!",
    "great question",
)

DEFAULT_RULES = ("em-dash", "ai-phrasing", "ellipsis", "markdown-in-descriptions")
RULES = DEFAULT_RULES + ("hard-wrap",)
DEFAULT_EXTENSIONS = (".md", ".txt", ".yml", ".yaml")
SKIPPED_DIRECTORIES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
}


def comma_list(value):
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_args(arguments):
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules", type=comma_list, default=DEFAULT_RULES)
    parser.add_argument("--ext", type=comma_list, default=DEFAULT_EXTENSIONS)
    parser.add_argument("paths", nargs="+")
    options = parser.parse_args(arguments)
    unknown = [rule for rule in options.rules if rule not in RULES]
    if unknown:
        parser.error(f"unknown rule: {', '.join(unknown)}")
    options.ext = tuple(
        extension.lower() if extension.startswith(".") else "." + extension.lower()
        for extension in options.ext
    )
    return options


def source_files(paths, extensions):
    seen = set()
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = []
            for root, directories, names in os.walk(path):
                directories[:] = sorted(
                    name for name in directories if name not in SKIPPED_DIRECTORIES
                )
                candidates.extend(Path(root) / name for name in sorted(names))
        else:
            print(f"check-writing: path not found: {path}", file=sys.stderr)
            continue
        for candidate in candidates:
            if candidate.suffix.lower() not in extensions:
                continue
            key = os.path.normcase(str(candidate.resolve()))
            if key not in seen:
                seen.add(key)
                yield candidate


def phrase_pattern(phrase):
    escaped = re.escape(phrase)
    start = r"(?<!\w)" if phrase[0].isalnum() else ""
    end = r"(?!\w)" if phrase[-1].isalnum() else ""
    return re.compile(start + escaped + end, re.IGNORECASE)


AI_PATTERNS = {
    phrase: phrase_pattern(phrase)
    for phrase in AI_PHRASES
    if phrase not in {"not only ... but also", "landscape"}
}
AI_PATTERNS["not only ... but also"] = re.compile(
    r"not only\b.{1,80}\bbut also", re.IGNORECASE
)
AI_PATTERNS["landscape"] = re.compile(r"\bthe\s+landscape\s+of\b", re.IGNORECASE)


def ai_phrase(line):
    for phrase in AI_PHRASES:
        for match in AI_PATTERNS[phrase].finditer(line):
            before = line[:match.start()]
            if phrase in {"leverage", "leveraging"}:
                after = line[match.end():]
                if re.search(r"financial\s+$", before, re.IGNORECASE):
                    continue
                if re.match(r"\s+ratio\b", after, re.IGNORECASE):
                    continue
            return phrase
    return None


def markdown_marker(value):
    if "`" in value or "**" in value or "__" in value:
        return True
    if re.search(r"\[[^]]+\]\([^)]+\)", value):
        return True
    stripped = value.lstrip()
    return bool(re.match(r"(?:[-*]\s+|#\s+)", stripped))


def excerpt(line):
    return line.strip()[:100]


def inspect_file(path, enabled):
    if "hard-wrap" in enabled and hard_wrap_findings is None:
        raise RuntimeError(HARD_WRAP_UNAVAILABLE)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    findings = []
    fence_marker = None
    description_indent = None
    yaml_file = path.suffix.lower() in {".yml", ".yaml"}

    for line_number, line in enumerate(lines, 1):
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        code_line = fence_marker is not None or fence is not None

        if "em-dash" in enabled:
            occurrences = line.count("\N{EM DASH}")
            occurrences += len(re.findall(r"(?<=\s)\N{EN DASH}(?=\s)", line))
            occurrences += len(re.findall(r"(?<=\w) -- (?=\w)", line))
            findings.extend(
                (line_number, "em-dash", excerpt(line)) for _ in range(occurrences)
            )

        policy_line = re.search(r"\bno AI-tell phrasing\b", line, re.IGNORECASE)
        if "ai-phrasing" in enabled and not code_line and policy_line is None:
            phrase = ai_phrase(line)
            if phrase is not None:
                findings.append(
                    (line_number, f"ai-phrasing ({phrase})", excerpt(line))
                )

        if "ellipsis" in enabled and not code_line:
            occurrences = line.count("\N{HORIZONTAL ELLIPSIS}")
            if "|" in line:
                occurrences += len(re.findall(r"\.\.\.(?=\s*\|)", line))
                occurrences += len(re.findall(r"\.\.\.(?=[\"'\]\)}])", line))
            findings.extend(
                (line_number, "ellipsis", excerpt(line)) for _ in range(occurrences)
            )

        if "markdown-in-descriptions" in enabled and yaml_file:
            indentation = len(line) - len(line.lstrip(" \t"))
            if description_indent is not None:
                if line.strip() and indentation <= description_indent:
                    description_indent = None
                elif line.strip() and markdown_marker(line.strip()):
                    findings.append(
                        (line_number, "markdown-in-descriptions", excerpt(line))
                    )
            if description_indent is None:
                match = re.match(r"^(\s*)description\s*:\s*(.*)$", line)
                if match:
                    value = match.group(2)
                    if value in {">", "|", ">-", "|-", ">+", "|+"}:
                        description_indent = len(match.group(1))
                    elif markdown_marker(value):
                        findings.append(
                            (line_number, "markdown-in-descriptions", excerpt(line))
                        )

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
    if "hard-wrap" in enabled and path.suffix.lower() in {".md", ".txt"}:
        findings.extend(
            (line_number, "hard-wrap", finding_excerpt)
            for line_number, finding_excerpt in hard_wrap_findings(text)
        )
    findings.sort(key=lambda finding: finding[0])
    return findings


def main(arguments=None):
    options = parse_args(sys.argv[1:] if arguments is None else arguments)
    if "hard-wrap" in options.rules and hard_wrap_findings is None:
        print(HARD_WRAP_UNAVAILABLE, file=sys.stderr)
        return 2
    findings = 0
    files = 0
    try:
        for path in source_files(options.paths, options.ext):
            files += 1
            for line_number, rule, text in inspect_file(path, set(options.rules)):
                findings += 1
                print(f"{path}:{line_number}: {rule}: {text}")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"{findings} findings in {files} files")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
