"""Require measured confirmation counts before destructive shell commands run.

DESTRUCTIVE_GUARD_TEMP_ROOTS is meant for the test suite; in a real session it replaces the temp exemption (empty means no exemption), and both configured and default temp roots ignore relative paths, drive or filesystem roots, the home folder and its ancestors; a home folder inside a temp root drops that root so the guard asks for counts there.
"""

import os
import re
import sys

from _common import read_payload


CANDIDATE = re.compile(
    r"\b(?:rm|ri|rd|rmdir|del|erase|Remove-Item)\b|"
    r"\bgit\b[\s\S]*\b(?:clean|reset|branch|push)\b|\bfind\b[\s\S]*\s-delete\b", re.I
)
TOKENS = re.compile(r"confirm-(delete|discard|drop|overwrite)=(\S+)")
NESTED = {"ssh", "bash", "sh", "zsh", "powershell", "pwsh", "cmd", "wsl"}
KINDS = ("delete", "discard", "drop", "overwrite")


def segments(command, tool_name="Bash", comments=None):
    escape = "\\" if tool_name == "Bash" else "`"
    quote = None
    start = 0
    index = 0
    while index < len(command):
        char = command[index]
        if char == escape and quote != "'":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or command[index - 1].isspace()):
            yield command[start:index].strip()
            newline = command.find("\n", index)
            if comments is not None:
                comments.append(command[index + 1:newline if newline >= 0 else len(command)])
            if newline < 0:
                return
            index = newline
            start = index + 1
        elif char in ";&|\n":
            background = (
                char == "&" and tool_name == "Bash"
                and (index == 0 or command[index - 1] not in "><")
                and command[index + 1:index + 2] != ">"
            )
            if char != "&" or command[index:index + 2] == "&&" or background:
                yield command[start:index].strip()
                if command[index:index + 2] in {"&&", "||"}:
                    index += 1
                start = index + 1
        index += 1
    yield command[start:].strip()


def resolve_target(target, cwd):
    if cwd is None or any(char in target for char in ("$", "%", "`")):
        return None
    target = os.path.expanduser(target)
    if os.name == "nt":
        target = re.sub(r"^/([a-zA-Z])(?:/|$)", lambda m: m[1].upper() + ":/", target)
    return os.path.realpath(os.path.join(cwd, target))


def safe_roots(candidates):
    roots = []
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    for root in candidates:
        root = root.strip()
        if not root or not os.path.isabs(root):
            continue
        root = os.path.normcase(os.path.realpath(root))
        if os.path.dirname(root) == root:
            continue
        try:
            if os.path.commonpath([home, root]) == root:
                continue
        except ValueError:
            pass
        roots.append(root)
    return roots


def is_temp(target):
    import tempfile

    configured = os.environ.get("DESTRUCTIVE_GUARD_TEMP_ROOTS")
    if configured is not None:
        roots = configured.split(os.pathsep)
    else:
        roots = [tempfile.gettempdir()]
        if os.name != "nt":
            roots.append("/tmp")
        if os.environ.get("LOCALAPPDATA"):
            roots.append(os.path.join(os.environ["LOCALAPPDATA"], "Temp"))
    target = os.path.normcase(os.path.realpath(target))
    roots = safe_roots(roots)
    if target in roots or target in {os.path.join(root, "claude") for root in roots}:
        return False
    for root in roots:
        try:
            if os.path.commonpath([target, root]) == root:
                return True
        except ValueError:
            pass
    return False


def add_counts(left, right):
    if "unmeasured" in (left, right):
        return "unmeasured"
    total = 0
    capped = new = known = False
    for value in (left, right):
        text = str(value)
        new = new or text.endswith("new")
        text = "" if text == "new" else text.removesuffix(",new").removesuffix("+new")
        capped = capped or text.endswith("+")
        if text:
            known = True
            try:
                total += int(text.rstrip("+"))
            except ValueError:
                return "unmeasured"
    if capped:
        return f"{total}+" + (",new" if new else "")
    if new:
        return f"{total}+new" if known else "new"
    return total


def delete_targets(words, tool_name="Bash"):
    name = words[0].lower()
    recursive = False
    targets = []
    literal = False
    for word in words[1:]:
        lower = word.lower()
        if literal:
            targets.append(word)
        elif word == "--":
            literal = True
        elif lower.startswith("-"):
            switch, _, value = word.partition(":")
            switch = switch.lower()
            if len(switch) >= 3 and "-whatif".startswith(switch):
                if not value or value.lower() in {"true", "$true"}:
                    return None
                continue
            if (len(switch) >= 2 and "-recurse".startswith(switch)) or (
                name == "rm" and tool_name == "Bash" and (
                    lower == "--recursive"
                    or (not word.startswith("--") and "r" in word[1:].lower())
                )
            ):
                recursive = True
            if switch in {"-path", "-literalpath"} and value:
                targets.append(unquote(value))
        elif (
            (name in {"rd", "rmdir"} and tool_name != "PowerShell")
            or (name in {"del", "erase"} and tool_name == "cmd")
        ) and lower in {"/s", "/q", "/f"}:
            recursive = recursive or lower == "/s"
        else:
            targets.append(word)
    return targets if recursive else None


def brace_targets(targets):
    expanded = []
    for target in targets:
        choices = [""]
        while target:
            opening = target.find("{")
            closing = target.find("}")
            if opening < 0:
                if closing >= 0:
                    return None
                choices = [choice + target for choice in choices]
                break
            if closing < opening or "{" in target[opening + 1:closing]:
                return None
            body = target[opening + 1:closing]
            parts = body.split(",") if "," in body else ["{" + body + "}"]
            choices = [choice + target[:opening] + part for choice in choices for part in parts]
            target = target[closing + 1:]
        expanded.extend(choices)
    return expanded


def measure_delete(targets, cwd, recursive_glob=False):
    import fnmatch
    import glob
    import time

    try:
        cap = int(os.environ.get("DESTRUCTIVE_GUARD_CAP", "20000"))
    except ValueError:
        cap = 20000
    if cap < 1:
        cap = 20000
    deadline = time.monotonic() + 1.0
    count = 0
    resolved = [resolve_target(target, cwd) for target in targets if target]
    if None in resolved:
        return "unmeasured", ", ".join(targets)
    paths = ", ".join(path.replace("\\", "/") for path in resolved)

    def walk_error(error):
        raise error

    try:
        for path in resolved:
            if is_temp(path):
                continue
            pattern = None
            if recursive_glob and glob.has_magic(path):
                directory, pattern = os.path.split(path)
                if glob.has_magic(directory):
                    return "unmeasured", paths
                matches = [directory]
            else:
                matches = glob.glob(path) if "*" in path or "?" in path else [path]
            for match in matches:
                if is_temp(match):
                    continue
                if os.path.isfile(match) or os.path.islink(match):
                    count += 1
                elif os.path.isdir(match):
                    for _, _, files in os.walk(match, followlinks=False, onerror=walk_error):
                        count += sum(fnmatch.fnmatch(name, pattern) for name in files) if pattern else len(files)
                        if count >= cap:
                            return f"{cap}+", paths
                        if time.monotonic() >= deadline:
                            return "unmeasured", paths
                if count >= cap:
                    return f"{cap}+", paths
                if time.monotonic() >= deadline:
                    return "unmeasured", paths
    except OSError:
        return "unmeasured", paths
    return count, paths


class GitFailure(Exception):
    pass


class NotRepository(Exception):
    pass


def git_run(cwd, arguments, git_options=()):
    import subprocess

    try:
        result = subprocess.run(
            ["git", *git_options, *arguments], cwd=cwd, capture_output=True, text=True,
            timeout=1.5, check=False,
        )
    except Exception as error:
        raise GitFailure() from error
    if result.returncode and "not a git repository" in result.stderr.lower():
        raise NotRepository()
    return result


def git_output(cwd, arguments, git_options=()):
    result = git_run(cwd, arguments, git_options)
    if result.returncode:
        raise GitFailure()
    return result.stdout.strip()


def ref_exists(cwd, ref, git_options=()):
    result = git_run(cwd, ["show-ref", "--verify", "--quiet", ref], git_options)
    if result.returncode not in (0, 1):
        raise GitFailure()
    return result.returncode == 0


def short_flag(word, flag):
    return word.startswith("-") and not word.startswith("--") and flag in word[1:]


def operands(arguments, value_options=()):
    result = []
    literal = False
    words = iter(arguments)
    for word in words:
        if literal:
            result.append(word)
        elif word == "--":
            literal = True
        elif word in value_options:
            next(words, None)
        elif not word.startswith("-"):
            result.append(word)
    return result


def reset_counts(args, cwd, detail, git_options):
    refs = operands(args)
    ref = refs[0] if refs else "HEAD"
    try:
        count = len(git_output(cwd, ["diff", "--name-only", ref], git_options).splitlines())
    except (GitFailure, ValueError):
        count = "unmeasured"
    drop = 0
    if refs:
        try:
            branch = git_output(cwd, ["rev-parse", "--abbrev-ref", "HEAD"], git_options)
            excluded = [] if branch == "HEAD" else [f"--exclude={branch}"]
            drop = int(git_output(cwd, ["rev-list", "--count", "HEAD", "--not", ref]
                                  + excluded + ["--branches", "--remotes"], git_options))
        except (GitFailure, ValueError):
            drop = "unmeasured"
    return [
        ("discard", count, f"{detail}: changes {count} tracked files"),
        ("drop", drop, f"{detail}: leaves behind {drop} commits"),
    ]


def git_arguments(words, cwd):
    arguments = words[1:]
    git_options = []
    while arguments:
        option = arguments[0]
        if option == "-C" and len(arguments) >= 2:
            cwd = resolve_target(arguments[1], cwd)
            arguments = arguments[2:]
        elif option in {"-c", "--git-dir", "--work-tree", "--namespace"} and len(arguments) >= 2:
            git_options.extend(arguments[:2])
            arguments = arguments[2:]
        elif option in {"--no-pager", "-p", "--paginate"}:
            arguments = arguments[1:]
        elif option in {"--bare", "--no-replace-objects", "--literal-pathspecs"} or option.startswith(
            ("--git-dir=", "--work-tree=", "--namespace=")
        ):
            git_options.append(option)
            arguments = arguments[1:]
        else:
            break
    return arguments, cwd, git_options


def git_kinds(subcommand, args):
    options = args[:args.index("--")] if "--" in args else args
    kinds = []
    if subcommand in {"clean", "push"} and any(
        word == "--dry-run" or short_flag(word, "n") for word in options
    ):
        return []
    if subcommand == "clean":
        if any(word == "--force" or short_flag(word, "f") for word in options):
            kinds = ["delete"]
    elif subcommand == "reset" and "--hard" in args:
        kinds = ["discard", "drop"] if operands(args) else ["discard"]
    elif subcommand == "branch":
        delete = any(word == "--delete" or short_flag(word, "d") for word in args)
        force = any(word == "--force" or short_flag(word, "f") for word in args)
        if any(short_flag(word, "D") for word in args) or (delete and force):
            kinds = ["drop"]
    elif subcommand == "push" and "--delete" not in args and "-d" not in args:
        if any(
            word in {"--force", "--mirror"} or short_flag(word, "f")
            or word.split("=", 1)[0] == "--force-with-lease" or word.startswith("+")
            for word in args
        ):
            kinds = ["overwrite"]
    return kinds


def measure_git(words, cwd, remote=False):
    arguments, cwd, git_options = git_arguments(words, None if remote else cwd)
    if not arguments:
        return []
    subcommand, *args = arguments
    subcommand = subcommand.lower()
    kinds = git_kinds(subcommand, args)
    if not kinds:
        return []
    detail = " ".join(words)
    if subcommand == "push" and any(word in {"--all", "--mirror", "--tags"} for word in args):
        return [("overwrite", "unmeasured", f"{detail}: every branch or tag on the remote can be rewritten")]
    if remote or cwd is None:
        location = "target is remote" if remote else "working directory is unmeasured"
        return [(kind, "unmeasured", f"{detail}: {kind} unmeasured ({location})") for kind in kinds]
    try:
        if subcommand == "clean":
            flags = []
            literal = False
            for word in args:
                if word == "--":
                    literal = True
                if not literal and word == "--force":
                    continue
                if not literal and short_flag(word, "f"):
                    word = "-" + word[1:].replace("f", "")
                    if word == "-":
                        continue
                flags.append(word)
            output = git_output(cwd, ["clean", "-n"] + flags, git_options)
            count = sum(line.startswith("Would remove") for line in output.splitlines())
            return [("delete", count, f"{detail}: {count} paths from git clean")]
        if subcommand == "reset":
            return reset_counts(args, cwd, detail, git_options)
        if subcommand == "branch":
            branches = [branch for branch in dict.fromkeys(operands(args))
                        if ref_exists(cwd, f"refs/heads/{branch}", git_options)]
            count = int(git_output(cwd, ["rev-list", "--count"]
                                   + [f"refs/heads/{branch}" for branch in branches] + ["--not"]
                                   + [f"--exclude={branch}" for branch in branches]
                                   + ["--branches", "--remotes"], git_options)) if branches else 0
            return [("drop", count, f"{detail}: drops {count} unique commits")]
        names = operands(args, {"--repo", "--receive-pack", "--exec", "-o", "--push-option"})
        branch = None
        if not names or len(names) == 1 or any(name.lstrip("+") == "HEAD" for name in names[1:]):
            branch = git_output(cwd, ["rev-parse", "--abbrev-ref", "HEAD"], git_options)
        if names:
            remote, *refs = names
        else:
            upstream = git_run(cwd, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], git_options)
            if upstream.returncode and not any(text in upstream.stderr.lower() for text in (
                "no upstream configured", "no such branch", "head does not point to a branch",
            )):
                raise GitFailure()
            remote = upstream.stdout.strip().split("/", 1)[0] if not upstream.returncode else "origin"
            refs = []
        results = []
        for ref in refs or [branch]:
            src, separator, dst = ref.lstrip("+").partition(":")
            if not separator:
                dst = branch if src == "HEAD" else src
            dst = dst.removeprefix("refs/heads/")
            tracking = f"refs/remotes/{remote}/{dst}"
            if not ref_exists(cwd, tracking, git_options):
                results.append(("overwrite", "new", f"{detail}: unknown commits on {remote}/{dst}; "
                                f"run git fetch {remote} first if the branch might exist on the remote"))
            else:
                count = int(git_output(cwd, ["rev-list", "--count", tracking, "--not", src], git_options))
                results.append(("overwrite", count, f"{detail}: overwrites {count} commits on {remote}/{dst}"))
        return results
    except NotRepository:
        return [(kind, 0, f"{detail}: 0 (not a repository)") for kind in kinds]
    except (GitFailure, ValueError):
        return [(kind, "unmeasured", f"{detail}: {kind} unmeasured") for kind in kinds]


def unquote(word):
    return word[1:-1] if len(word) >= 2 and word[0] in "\"'" and word[-1] == word[0] else word


def split_commas(segment):
    quote = None
    result = []
    escaped = False
    for char in segment:
        if escaped:
            escaped = False
        elif char == "`" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == ",":
            char = " "
        result.append(char)
    return "".join(result)


def command_words(segment, posix=True, split_lists=False):
    import shlex

    if split_lists:
        segment = split_commas(segment)
    try:
        words = shlex.split(segment, posix=posix)
    except ValueError:
        words = segment.split()
    if not posix:
        words = [unquote(word) for word in words]
    return words


def command_name(word):
    return os.path.splitext(word.replace("\\", "/").rsplit("/", 1)[-1])[0].lower()


def leading_words(words, tool_name="Bash"):
    words = list(words)
    while words and words[-1] in {")", "}"}:
        words.pop()
    wrappers = {"sudo", "time", "env", "command", "nohup", "exec"}
    keywords = {"then", "do", "else", "elif", "{", "(", "!"}
    if tool_name != "Bash":
        keywords.add("&")
    while words:
        name = command_name(words[0])
        if words[0] in keywords or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words.pop(0)
        elif name in wrappers:
            words.pop(0)
            values = {"-u", "-g", "-h", "-p", "-C", "--user", "--group"} if name == "sudo" else {
                "-u", "--unset", "-C", "--chdir", "-a", "-f", "-o",
            }
            while words and words[0].startswith("-"):
                option = words.pop(0)
                if option in values and words:
                    words.pop(0)
        else:
            words[0] = name
            break
    return words


def preserve_existing_targets(segment, targets, cwd, tool_name):
    if "\\" not in segment:
        return targets
    words = leading_words(command_words(segment, posix=False, split_lists=tool_name != "Bash"), tool_name)
    preserved = delete_targets(words, tool_name) if words else None
    if preserved is None or len(preserved) != len(targets):
        return targets
    result = []
    for target, alternative in zip(targets, preserved):
        path = resolve_target(target, cwd)
        literal_path = resolve_target(alternative, cwd)
        if (
            "\\" in alternative and path is not None and literal_path is not None
            and not os.path.exists(path) and os.path.exists(literal_path)
        ):
            target = alternative
        result.append(target)
    return result


def joined_command(words):
    import shlex

    return words[0] if len(words) == 1 else shlex.join(words)


def nested_command(words):
    name = words[0]
    if name in {"ssh", "wsl"}:
        args = words[1:]
        if name == "wsl":
            for index, word in enumerate(args):
                if word in {"-e", "--exec", "--"}:
                    return joined_command(args[index + 1:]), "Bash", True
        value_options = {"-p", "-i", "-l", "-o", "-F", "-J", "-S", "-L", "-R"} if name == "ssh" else {
            "-d", "--distribution", "-u", "--user", "--cd",
        }
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if option in value_options and args:
                args.pop(0)
        if name == "ssh" and args:
            args.pop(0)
        return joined_command(args), "Bash", True
    for index, word in enumerate(words[1:], 1):
        if name in {"bash", "sh", "zsh"}:
            matches = short_flag(word, "c")
            inner_tool = "Bash"
        else:
            matches = word.lower() in ({"/c"} if name == "cmd" else {"-command", "-c"})
            inner_tool = "cmd" if name == "cmd" else "PowerShell"
        if matches and index + 1 < len(words):
            inner = words[index + 1] if inner_tool == "Bash" else joined_command(words[index + 1:])
            return inner, inner_tool, False
    return "", "Bash", False


def directory_target(words):
    for word in words[1:]:
        if word.lower().startswith(("-path:", "-literalpath:")):
            return unquote(word.split(":", 1)[1])
        if not word.startswith("-"):
            return word
    return "~" if len(words) == 1 else "$unresolved"


def indirect_delete(words):
    if words[0] == "find":
        if "-delete" in words:
            return "find -delete"
        for index, word in enumerate(words[:-1]):
            if word in {"-exec", "-execdir"} and command_name(words[index + 1]) == "rm":
                return f"find {word} rm"
    if words[0] == "xargs":
        args = words[1:]
        value_options = {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file", "--delimiter",
                         "--eof", "--replace", "--max-lines", "--max-args", "--max-procs", "--max-chars"}
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if option == "--":
                break
            if option in value_options and args:
                args.pop(0)
        args = leading_words(args)
        if args and args[0] == "rm" and delete_targets(args) is not None:
            return "xargs recursive rm"
    return None


def destructive_substitution(command):
    for match in re.finditer(r"(?=\$\(([^)]*)\)|`([^`]*)`)", command):
        inner = match[1] if match[1] is not None else match[2]
        for segment in segments(inner):
            words = leading_words(command_words(segment))
            if words and words[0] == "rm" and delete_targets(words) is not None:
                return "$(...) substitution" if match[1] is not None else "backtick substitution"
    return None


def measure_command(command, cwd, tool_name="Bash", remote=False, depth=0):
    measurements = []
    comments = []
    tokens = []
    parts = list(segments(command, tool_name, comments))
    substitution = destructive_substitution("; ".join(parts))
    if substitution:
        location = " (target is remote)" if remote else ""
        measurements.append(("delete", "unmeasured", f"{substitution}: delete unmeasured{location}"))
    for segment in parts:
        words = leading_words(command_words(segment, posix=tool_name == "Bash"), tool_name)
        if not words:
            continue
        name = words[0]
        indirect = indirect_delete(words)
        if indirect:
            location = " (target is remote)" if remote else ""
            measurements.append(("delete", "unmeasured", f"{segment}: {indirect}, delete unmeasured{location}"))
            continue
        if name in NESTED:
            inner, inner_tool, inner_remote = nested_command(words)
            if inner and CANDIDATE.search(inner) and depth < 20:
                measured, inner_tokens = measure_command(inner, cwd, inner_tool, remote or inner_remote, depth + 1)
                measurements.extend((kind, count, f"{segment}: {detail}") for kind, count, detail in measured)
                tokens.extend(inner_tokens)
            continue
        if name in {"cd", "pushd", "set-location", "sl", "chdir"}:
            target = directory_target(words)
            cwd = resolve_target(target, cwd) if not remote and target != "-" else None
            if cwd is not None and not remote and not os.path.isdir(cwd):
                cwd = None
            continue
        if name == "git":
            measured = measure_git(words, cwd, remote)
            if measured:
                prefix = len(" ".join(words)) + 2
                detail = segment + ": " + "; ".join(detail[prefix:] for _, _, detail in measured)
                measurements.extend((kind, count, detail) for kind, count, _ in measured)
        elif name in {"rm", "ri", "rd", "rmdir", "del", "erase", "remove-item"}:
            if tool_name != "Bash":
                words = leading_words(command_words(segment, posix=False, split_lists=True), tool_name)
            targets = delete_targets(words, tool_name)
            if targets is not None:
                supplied = bool(targets)
                if tool_name == "Bash":
                    targets = brace_targets(targets)
                if targets is not None:
                    targets = [target for target in targets if target]
                    if supplied and not targets:
                        continue
                if remote or cwd is None or not targets:
                    count = "unmeasured"
                    paths = "target is remote" if remote else "unresolved targets or working directory"
                else:
                    targets = preserve_existing_targets(segment, targets, cwd, tool_name)
                    count, paths = measure_delete(targets, cwd, recursive_glob=tool_name == "cmd" and name in {"del", "erase"})
                detail = f"{segment}: {count} files under {paths}"
                measurements.append(("delete", count, detail))
    tokens.extend(TOKENS.findall("\n".join(comments)))
    return measurements, tokens


def inspect_command(command, cwd, tool_name="Bash"):
    measurements, tokens = measure_command(command, cwd, tool_name)
    totals = {}
    for kind, count, _ in measurements:
        totals[kind] = add_counts(totals[kind], count) if kind in totals else count
    required = {kind: str(totals[kind]) for kind in KINDS if totals.get(kind, 0) != 0}
    if all((kind, count) in tokens for kind, count in required.items()):
        return 0
    lines = ["Destructive command: measured before it runs."]
    details = dict.fromkeys(detail for _, count, detail in measurements if count != 0)
    lines.extend("- " + detail for detail in details)
    lines.extend(
        f"confirm-{kind}={value} does not match the measured {required[kind]}"
        for kind, value in tokens if kind in required and value != required[kind]
    )
    lines.extend([
        "Rerun the same command with the counts appended as a comment:",
        "  # " + " ".join(f"confirm-{kind}={count}" for kind, count in required.items()),
        "A count that does not match what is there now is refused. The count proves the command "
        "was checked; the user's approval rule for irreversible actions still applies.",
    ])
    sys.stderr.write("\n".join(lines) + "\n")
    return 2


def main():
    try:
        payload = read_payload()
        if not payload or payload.get("tool_name") not in {"Bash", "PowerShell"}:
            return 0
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return 0
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip() or not CANDIDATE.search(command):
            return 0
        return inspect_command(command, payload.get("cwd") or os.getcwd(), payload["tool_name"])
    except Exception as error:
        sys.stderr.write(f"destructive_guard: internal error {type(error).__name__}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
