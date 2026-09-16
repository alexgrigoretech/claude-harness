import argparse
import contextlib
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claude.hooks._common import hooks_home


PROMPT_FILE_NOTE = (
    "Review the diff on stdin per the instructions it contains. "
    "Reply with JSON only."
)
DEFAULT_REVIEWERS = [
    {
        "name": "agy",
        "cmd": subprocess.list2cmdline(
            [
                sys.executable,
                str(Path(__file__).with_name("agy_llm.py")),
                "--model",
                "gemini-3.8-flash-low",
            ]
        ),
        "stdin": True,
    },
    {
        "name": "codex",
        "cmd": (
            'codex exec - -C "{repo}" -m {codex_model} '
            '-c model_reasoning_effort=medium --sandbox read-only '
            '--output-last-message "{output_file}"'
        ),
        "stdin": True,
    },
    {"name": "kimi", "cmd": 'kimi -p "{prompt}"', "stdin": False},
]
SEVERITIES = {"high", "medium", "low"}


class ReviewError(Exception):
    pass


@dataclass
class ReviewerRun:
    name: str
    status: str
    seconds: float
    findings: list
    usage: dict | None = None
    error: str | None = None

    def public_status(self):
        value = {
            "name": self.name,
            "status": self.status,
            "seconds": self.seconds,
            "findings": len(self.findings),
        }
        if self.usage is not None:
            value["usage"] = self.usage
        if self.error is not None:
            value["error"] = self.error
        return value


def command_tokens(command):
    tokens = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        tokens = [
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'"
            else token
            for token in tokens
        ]
    if not tokens:
        raise ValueError("reviewer command must not be empty")
    return tokens


def parse_override(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("reviewer override must be name=cmd")
    name, command = value.split("=", 1)
    name = name.strip()
    command = command.strip()
    if not name or not command:
        raise argparse.ArgumentTypeError("reviewer override must be name=cmd")
    return {"name": name, "cmd": command, "stdin": "{prompt}" not in command}


def load_reviewer_configs(overrides=None, home=None):
    base = Path(home) if home is not None else hooks_home()
    machine_path = base / ".claude" / "local" / "machine.json"
    configured = None
    if machine_path.is_file():
        try:
            with machine_path.open(encoding="utf-8-sig") as handle:
                machine = json.load(handle)
            value = machine.get("pr_review_reviewers") if isinstance(machine, dict) else None
            if isinstance(value, list) and value:
                configured = value
        except (OSError, ValueError, TypeError):
            configured = None

    source = configured if configured is not None else DEFAULT_REVIEWERS
    reviewers = {}
    for item in source:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        command = item.get("cmd")
        if not isinstance(name, str) or not name or not isinstance(command, str) or not command:
            continue
        reviewers[name] = {
            "name": name,
            "cmd": command,
            "stdin": bool(item.get("stdin", "{prompt}" not in command)),
        }
    for item in overrides or []:
        reviewers[item["name"]] = dict(item)
    return reviewers


def select_reviewers(configs, names=None):
    if names is None:
        return list(configs.values()), []
    requested = [name.strip() for name in names.split(",") if name.strip()]
    if len(requested) == 1 and requested[0].lower() == "none":
        return [], []
    selected = []
    missing = []
    for name in requested:
        if name in configs:
            selected.append(configs[name])
        else:
            missing.append(name)
    return selected, missing


def read_diff(repo, base, diff_file=None):
    if diff_file is not None:
        return Path(diff_file).read_text(encoding="utf-8-sig")
    attempts = (["git", "diff", f"{base}...HEAD"], ["git", "diff", base])
    errors = []
    for index, command in enumerate(attempts):
        try:
            if index == 1:
                merge_base = subprocess.run(
                    ["git", "merge-base", base, "HEAD"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if merge_base.returncode == 0 and merge_base.stdout.strip():
                    command = ["git", "diff", merge_base.stdout.strip()]
            completed = subprocess.run(
                command,
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise ReviewError(f"could not run git: {exc}") from exc
        if completed.returncode == 0:
            if completed.stdout.strip():
                print(f"pr-review: diff from {' '.join(command)}", file=sys.stderr)
                return completed.stdout
            continue
        errors.append(completed.stderr.strip() or f"exit {completed.returncode}")
    if len(errors) == len(attempts):
        raise ReviewError(f"git diff failed: {errors[-1]}")
    return ""


def read_body(repo, body_file=None):
    if body_file is not None:
        return Path(body_file).read_text(encoding="utf-8-sig")
    if shutil.which("gh") is None:
        return ""
    try:
        completed = subprocess.run(
            ["gh", "pr", "view", "--json", "body", "-q", ".body"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def truncate_diff(diff_text, max_bytes):
    raw = diff_text.encode("utf-8")
    if len(raw) <= max_bytes:
        return diff_text, False
    boundaries = [match.start() + 1 for match in re.finditer(br"\ndiff --git ", raw)]
    boundary = max((position for position in boundaries if position <= max_bytes), default=0)
    kept = raw[:boundary].decode("utf-8", errors="ignore")
    note = (
        f"[Runner note: the diff exceeded {max_bytes} bytes and was truncated at a "
        "file boundary. Do not report its size or truncation as a finding.]"
    )
    return (kept.rstrip() + "\n\n" + note).lstrip(), True


def build_prompt(body, diff_text, template_path=None):
    path = Path(template_path) if template_path is not None else Path(__file__).with_name(
        "pr_review_prompts"
    ) / "review.txt"
    template = path.read_text(encoding="utf-8-sig")
    before_body, remainder = template.split("{body}", 1)
    between, after_diff = remainder.split("{diff}", 1)
    return before_body + body + between + diff_text + after_diff


def strip_code_fence(text):
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else stripped


def structured_values(text):
    clean = strip_code_fence(text)
    decoder = json.JSONDecoder()
    for index, character in enumerate(clean):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(clean[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (list, dict)):
            yield value


def findings_from_value(value):
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return None
    findings = value.get("findings")
    if isinstance(findings, list):
        return findings
    response = value.get("response")
    if isinstance(response, str):
        return extract_findings(response)[0]
    if isinstance(response, (list, dict)):
        return findings_from_value(response)
    return None


def normalized_token_key(key):
    return re.sub(r"[^a-z]", "", str(key).lower())


def numeric_token(mapping, keys):
    normalized = {normalized_token_key(key): value for key, value in mapping.items()}
    for key in keys:
        value = normalized.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


def extract_usage(value):
    if not isinstance(value, dict):
        return None
    input_keys = ("inputtokens", "prompttokens", "prompttokencount", "input", "prompt")
    output_keys = (
        "outputtokens",
        "completiontokens",
        "candidatestokencount",
        "candidatetokens",
        "output",
        "completion",
        "candidates",
    )
    pending = []
    usage = value.get("usage")
    if isinstance(usage, dict):
        pending.append(usage)
    pending.append(value)
    while pending:
        current = pending.pop(0)
        input_tokens = numeric_token(current, input_keys)
        output_tokens = numeric_token(current, output_keys)
        if input_tokens is not None and output_tokens is not None:
            return {"input_tokens": input_tokens, "output_tokens": output_tokens}
        pending.extend(item for item in current.values() if isinstance(item, dict))
    return None


def normalize_finding(value):
    if not isinstance(value, dict):
        return None
    file_name = value.get("file")
    line = value.get("line")
    severity = value.get("severity")
    claim = value.get("claim")
    evidence = value.get("evidence")
    confidence = value.get("confidence")
    if not isinstance(file_name, str) or not file_name.strip():
        return None
    if line is not None and (not isinstance(line, int) or isinstance(line, bool)):
        return None
    if severity not in SEVERITIES or not isinstance(claim, str) or not isinstance(evidence, str):
        return None
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return None
    return {
        "file": file_name.replace("\\", "/"),
        "line": line,
        "severity": severity,
        "claim": claim.strip(),
        "evidence": evidence.strip(),
        "confidence": max(0.0, min(1.0, float(confidence))),
    }


def extract_findings(text):
    for value in structured_values(text):
        findings = findings_from_value(value)
        if findings is not None:
            normalized = [finding for item in findings if (finding := normalize_finding(item))]
            return normalized, extract_usage(value)
    raise ValueError("reviewer output does not contain a JSON findings array")


def configured_codex_model(home=None):
    base = Path(home) if home is not None else hooks_home()
    machine_path = base / ".claude" / "local" / "machine.json"
    try:
        with machine_path.open(encoding="utf-8-sig") as handle:
            machine = json.load(handle)
    except (OSError, ValueError, TypeError):
        return "gpt-5.6-sol"
    model = machine.get("codex_model") if isinstance(machine, dict) else None
    return model.strip() if isinstance(model, str) and model.strip() else "gpt-5.6-sol"


def resolve_reviewer_executable(executable):
    if os.sep in executable or (os.altsep is not None and os.altsep in executable):
        return executable
    names = [executable]
    if os.name == "nt":
        extensions = [
            extension
            for extension in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
            if extension
        ]
        if Path(executable).suffix.lower() not in {
            extension.lower() for extension in extensions
        }:
            names = [executable + extension for extension in extensions]
    current_directory = Path.cwd().resolve()
    for value in os.environ.get("PATH", "").split(os.pathsep):
        value = value.strip().strip('"')
        if not value:
            continue
        directory = Path(value)
        if not directory.is_absolute():
            continue
        directory = directory.resolve()
        if directory == current_directory:
            continue
        for name in names:
            candidate = directory / name
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)
    return None


def prepare_command(config, prompt, repo=None, home=None, output_file=None):
    tokens = command_tokens(config["cmd"])
    repo_path = Path(repo if repo is not None else ".").resolve().as_posix()
    output_path = (
        Path(output_file).resolve().as_posix() if output_file is not None else "{output_file}"
    )
    codex_model = configured_codex_model(home) if "{codex_model}" in config["cmd"] else ""
    prepared = [
        token.replace("{prompt_file_note}", PROMPT_FILE_NOTE)
        .replace("{repo}", repo_path)
        .replace("{codex_model}", codex_model)
        .replace("{output_file}", output_path)
        .replace("{prompt}", prompt)
        for token in tokens
    ]
    prepared[0] = resolve_reviewer_executable(prepared[0])
    return prepared


def run_reviewer(config, prompt, timeout=300, repo=None, home=None):
    with contextlib.ExitStack() as stack:
        output_file = None
        if "{output_file}" in config["cmd"]:
            output_directory = stack.enter_context(tempfile.TemporaryDirectory())
            output_file = Path(output_directory) / "last-message.txt"
        command = prepare_command(
            config, prompt, repo=repo, home=home, output_file=output_file
        )
        if command[0] is None:
            executable = command_tokens(config["cmd"])[0]
            return ReviewerRun(
                config["name"],
                "error",
                0.0,
                [],
                error=f"executable not found on PATH: {executable}",
            )
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                input=prompt if config.get("stdin", True) else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ReviewerRun(
                config["name"],
                "error",
                round(time.monotonic() - started, 3),
                [],
                error="timeout",
            )
        except (OSError, ValueError) as exc:
            return ReviewerRun(
                config["name"],
                "error",
                round(time.monotonic() - started, 3),
                [],
                error=f"{exc.__class__.__name__}: {exc}",
            )
        seconds = round(time.monotonic() - started, 3)
        if completed.returncode != 0:
            diagnostic = " ".join(completed.stderr[:300].split())
            error = f"exit {completed.returncode}"
            if diagnostic:
                error += f": {diagnostic}"
            return ReviewerRun(config["name"], "error", seconds, [], error=error)
        output = completed.stdout
        fallback_error = None
        if output_file is not None:
            try:
                file_output = output_file.read_text(encoding="utf-8-sig")
            except OSError:
                file_output = ""
            if file_output.strip():
                output = file_output
            else:
                fallback_error = "output file missing or empty, parsed stdout fallback"
        try:
            findings, usage = extract_findings(output)
        except ValueError as exc:
            error = str(exc)
            if fallback_error:
                error += f": {fallback_error}"
            return ReviewerRun(config["name"], "error", seconds, [], error=error)
        return ReviewerRun(
            config["name"],
            "ok",
            seconds,
            findings,
            usage=usage,
            error=fallback_error,
        )


def run_reviewers(prompt, configs, timeout=300, repo=None, home=None):
    return [
        run_reviewer(config, prompt, timeout=timeout, repo=repo, home=home)
        for config in configs
    ]


def matching_group(groups, finding, reviewer):
    for group in groups:
        if reviewer in group["reviewers"] or group["finding"]["file"] != finding["file"]:
            continue
        line = finding["line"]
        if line is None and group["null_line"]:
            return group
        if line is not None and any(abs(line - other) <= 3 for other in group["lines"]):
            return group
    return None


def dedupe_findings(runs):
    groups = []
    for run in runs:
        if run.status != "ok":
            continue
        for finding in run.findings:
            group = matching_group(groups, finding, run.name)
            if group is None:
                groups.append(
                    {
                        "finding": dict(finding),
                        "reviewers": [run.name],
                        "lines": [] if finding["line"] is None else [finding["line"]],
                        "null_line": finding["line"] is None,
                    }
                )
            else:
                group["reviewers"].append(run.name)
                if finding["line"] is not None:
                    group["lines"].append(finding["line"])
    merged = []
    for group in groups:
        finding = group["finding"]
        finding["reviewers"] = group["reviewers"]
        merged.append(finding)
    return merged


def result_document(base, runs):
    return {
        "base": base,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "reviewers": [run.public_status() for run in runs],
        "findings": dedupe_findings(runs),
    }


def print_summary(runs):
    print(f"{'reviewer':<20} {'status':<8} {'findings':>8} {'seconds':>8}")
    for run in runs:
        print(f"{run.name:<20} {run.status:<8} {len(run.findings):>8} {run.seconds:>8.3f}")


def positive_integer(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def build_parser():
    parser = argparse.ArgumentParser(description="Run configured command line reviewers on a PR diff.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", default="main")
    parser.add_argument("--diff")
    parser.add_argument("--body")
    parser.add_argument("--reviewers")
    parser.add_argument("--reviewer", action="append", default=[], type=parse_override)
    parser.add_argument("--out")
    parser.add_argument("--max-diff-bytes", type=positive_integer, default=400000)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        diff_text = read_diff(repo, args.base, args.diff)
    except (OSError, UnicodeError, ReviewError) as exc:
        print(f"PR review failed: {exc}", file=sys.stderr)
        return 1
    if not diff_text.strip():
        print("PR review stopped: the diff is empty.", file=sys.stderr)
        return 1

    diff_text, _ = truncate_diff(diff_text, args.max_diff_bytes)
    configs = load_reviewer_configs(args.reviewer)
    selected, missing = select_reviewers(configs, args.reviewers)
    runs = [ReviewerRun(name, "error", 0.0, [], error="reviewer is not configured") for name in missing]
    if selected:
        try:
            body = read_body(repo, args.body)
            prompt = build_prompt(body, diff_text)
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"PR review failed: could not build the prompt: {exc}", file=sys.stderr)
            return 1
        runs.extend(run_reviewers(prompt, selected, repo=repo))
    document = result_document(args.base, runs)
    output = Path(args.out) if args.out else repo / "pr-review-findings.json"
    if not output.is_absolute():
        output = repo / output
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"PR review failed: could not write {output}: {exc}", file=sys.stderr)
        return 1

    print_summary(runs)
    if not runs:
        print("No reviewers are configured or selected.", file=sys.stderr)
        return 1
    if all(run.status == "error" for run in runs):
        print("Every reviewer failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
