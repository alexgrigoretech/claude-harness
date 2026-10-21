import copy
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
STATUSLINE_COMMAND = "bash ~/.claude/statusline-command.sh"
DEFAULT_CODEX_EFFORT = "high"
DEFAULT_CLAUDE_EFFORT = {"claude-fable-5-1": "medium", "claude-opus-5-5": "high", "claude-sonnet-5-5": "medium", "claude-haiku-5-5": "medium"}
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claude.hooks._common import nudge_disabled


USAGE = (
    "usage: python install.py [--machine NAME] [--home PATH] [--repo PATH] "
    "[--dry-run] [--no-tests] [--git-hooks REPO] [--gitleaks PATH] "
    "[--bundle NAME] [--addon NAME] [--out PATH] [--unpack ZIP]"
)


def parse_args(arguments):
    options = {
        "machine": None,
        "home": Path.home(),
        "repo": Path(__file__).resolve().parent,
        "dry_run": False,
        "no_tests": False,
        "git_hooks": None,
        "gitleaks": None,
        "bundle": None,
        "addon": None,
        "unpack": None,
        "out": None,
    }
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--dry-run", "--no-tests"}:
            options[argument[2:].replace("-", "_")] = True
            index += 1
            continue
        if argument in {
            "--machine",
            "--home",
            "--repo",
            "--git-hooks",
            "--gitleaks",
            "--bundle",
            "--addon",
            "--unpack",
            "--out",
        }:
            if index + 1 >= len(arguments):
                raise ValueError(f"missing value for {argument}")
            key = argument[2:].replace("-", "_")
            options[key] = arguments[index + 1]
            index += 2
            continue
        raise ValueError(f"unknown argument: {argument}")
    options["home"] = Path(options["home"]).expanduser().resolve()
    options["repo"] = Path(options["repo"]).expanduser().resolve()
    if options["out"] is not None:
        options["out"] = Path(options["out"]).expanduser().resolve()
    if options["unpack"] is not None:
        options["unpack"] = Path(options["unpack"]).expanduser().resolve()
        for key in ("machine", "bundle", "addon", "out", "git_hooks"):
            if options[key] is not None:
                raise ValueError("--unpack cannot be combined with --" + key.replace("_", "-"))
    if options["bundle"] is not None and options["git_hooks"] is not None:
        raise ValueError("--bundle cannot be combined with --git-hooks")
    if options["addon"] is not None:
        for key in ("bundle", "machine", "git_hooks"):
            if options[key] is not None:
                raise ValueError("--addon cannot be combined with --" + key.replace("_", "-"))
    return options


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def deep_merge(base, override, replace_whole=frozenset()):
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    result = dict(base)
    for key, value in override.items():
        if key not in replace_whole and key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def validate_machine_flags(machine, defaults, source_for_key):
    flags = {}
    for key, default in defaults.items():
        flags[key] = machine.get(key, default)
        if not isinstance(flags[key], bool):
            raise ValueError(f"{source_for_key(key)}: {key} must be true or false")
    return flags


def validate_gh_accounts(machine, source_for_key):
    configured = machine.get("gh_accounts")
    if configured is not None and not isinstance(configured, dict):
        raise ValueError(f"{source_for_key('gh_accounts')}: gh_accounts must be an object")
    accounts = dict(configured or {})
    for prefix, login in accounts.items():
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValueError(f"{source_for_key('gh_accounts')}: gh_accounts key {prefix!r} must be a non empty folder prefix")
        if prefix != "default" and not prefix.startswith(("/", "~/")) and prefix != "~" and not re.match(r"^[A-Za-z]:[/\\]", prefix):
            raise ValueError(f"{source_for_key('gh_accounts')}: gh_accounts key {prefix!r} must be absolute, ~ or start with ~/")
        if not isinstance(login, str) or not login.strip():
            raise ValueError(f"{source_for_key('gh_accounts')}: gh_accounts.{prefix} must be a non empty string")
    legacy = machine.get("gh_account")
    if legacy is not None:
        if not isinstance(legacy, str) or not legacy.strip():
            raise ValueError(f"{source_for_key('gh_account')}: gh_account must be a non empty string")
        if "default" in accounts and accounts["default"] != legacy:
            raise ValueError(f"{source_for_key('gh_accounts')}: gh_accounts.default and gh_account must agree")
        accounts.setdefault("default", legacy)
    return accounts if configured is not None or legacy is not None else None


def validate_machine_efforts(machine, source_for_key):
    muse_effort = machine.get("muse_effort")
    if muse_effort is not None and muse_effort not in ("minimal", "low", "medium", "high", "xhigh", "max"):
        raise ValueError(f"{source_for_key('muse_effort')}: muse_effort must be minimal, low, medium, high, xhigh, max or null")
    muse_model = machine.get("muse_model")
    if muse_model is not None and (not isinstance(muse_model, str) or re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", muse_model) is None):
        raise ValueError(f"{source_for_key('muse_model')}: muse_model must match [A-Za-z0-9][A-Za-z0-9._-]* or be null")
    kimi_model = machine.get("kimi_model")
    if kimi_model is not None and (not isinstance(kimi_model, str) or re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", kimi_model) is None):
        raise ValueError(f"{source_for_key('kimi_model')}: kimi_model must match [A-Za-z0-9][A-Za-z0-9._/-]* or be null")
    kimi_effort = machine.get("kimi_effort")
    if kimi_effort is not None and kimi_effort not in ("low", "high", "max"):
        raise ValueError(f"{source_for_key('kimi_effort')}: kimi_effort must be low, high, max or null")
    allowed = ("low", "medium", "high", "xhigh")
    codex = machine.get("codex_effort")
    if codex is not None and codex not in ("minimal", "low", "medium", "high", "xhigh"):
        raise ValueError(f"{source_for_key('codex_effort')}: codex_effort must be minimal, low, medium, high, xhigh or null")
    claude = machine.get("claude_effort")
    if claude is not None and (
        not isinstance(claude, dict)
        or any(not isinstance(model, str) or not model or effort not in allowed for model, effort in claude.items())
    ):
        raise ValueError(f"{source_for_key('claude_effort')}: claude_effort must be a model map with values low, medium, high or xhigh, or null")


def validate_statusline_weather(machine, source_for_key):
    weather = machine.get("statusline_weather")
    if weather is None:
        return
    if (
        not isinstance(weather, dict)
        or set(weather) != {"city", "lat", "lon"}
        or not isinstance(weather["city"], str)
        or not weather["city"].strip()
        or type(weather["lat"]) not in (int, float)
        or not -90 <= weather["lat"] <= 90
        or type(weather["lon"]) not in (int, float)
        or not -180 <= weather["lon"] <= 180
    ):
        raise ValueError(f"{source_for_key('statusline_weather')}: statusline_weather must be null or an object with city, lat and lon")


def validate_proxy(machine, source_for_key):
    if "proxy" not in machine:
        return
    prefix = f"{source_for_key('proxy')}: proxy"
    proxy = machine["proxy"]
    if not isinstance(proxy, dict):
        raise ValueError(f"{prefix} must be an object")
    unknown = sorted(set(proxy) - {
        "host", "port", "scheme", "key_file", "claude_command", "codex_command", "bin_dir",
    })
    if unknown:
        raise ValueError(f"{prefix} has unknown keys: {', '.join(unknown)}")
    for key in ("host", "key_file", "claude_command", "codex_command", "bin_dir"):
        if key not in proxy and key not in ("host", "key_file"):
            continue
        if not isinstance(proxy.get(key), str) or not proxy[key].strip():
            raise ValueError(f"{prefix}.{key} must be a non empty string")
    if type(proxy.get("port", 8317)) is not int or not 1 <= proxy.get("port", 8317) <= 65535:
        raise ValueError(f"{prefix}.port must be an integer from 1 to 65535")
    if proxy.get("scheme", "http") not in ("http", "https"):
        raise ValueError(f"{prefix}.scheme must be http or https")
    if machine.get("os") not in ("windows", "linux", "darwin"):
        raise ValueError(f"{prefix} requires os windows, linux or darwin; got {machine.get('os')!r}")


def validate_remote(machine, source_for_key):
    if "remote" not in machine:
        return
    from tools.remote_prompt import validate_remote_block

    try:
        validate_remote_block(machine["remote"])
    except ValueError as exc:
        raise ValueError(f"{source_for_key('remote')}: {exc}") from exc


def read_local_machine(path, label=None):
    try:
        machine = read_json(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label or path}: {exc}") from exc
    if not isinstance(machine, dict):
        raise ValueError(f"{label or path}: must be a JSON object")
    return machine


def load_machine(repo, name, home=None, sources=None):
    tracked_path = repo / "machines" / f"{name}.json"
    machine = read_json(tracked_path)
    if sources is not None:
        sources.update({key: tracked_path for key in machine})
    local_path = repo / "machines" / f"{name}.local.json"
    local = read_local_machine(local_path)
    machine = deep_merge(machine, local, replace_whole=frozenset({"statusline_weather"}))
    if sources is not None:
        sources.update({key: local_path for key in local})
    if home is not None:
        home_local_path = home / ".claude" / "local" / "machine.local.json"
        local = read_local_machine(home_local_path)
        machine = deep_merge(machine, local, replace_whole=frozenset({"statusline_weather"}))
        if sources is not None:
            sources.update({key: home_local_path for key in local})
        if local_path.is_file() and home_local_path.is_file():
            print(f"note: {home_local_path} merged over machines/{name}.local.json")
    return machine


def clear_read_only(path):
    path = Path(path)
    if path.is_file() or (os.name == "nt" and path.is_dir()):
        try:
            os.chmod(path, stat.S_IWRITE)
        except OSError:
            pass


def remove_with_retry(path, operation, on_failure):
    clear_read_only(path)
    try:
        operation()
        return True
    except PermissionError:
        clear_read_only(path)
        try:
            operation()
            return True
        except PermissionError as exc:
            if on_failure is not None:
                on_failure(path, exc)
            return False


def remove_path(path, on_failure=None):
    path = Path(path)
    if path.is_symlink() or path.is_file():
        return remove_with_retry(path, path.unlink, on_failure)
    if path.is_dir():
        removed = True
        for child in path.iterdir():
            if not remove_path(child, on_failure):
                removed = False
        if not removed:
            return False
        return remove_with_retry(path, path.rmdir, on_failure)
    return True


def copy_tree(source, destination):
    source = Path(source)
    destination = Path(destination)
    if source.is_dir() and not source.is_symlink():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            copy_tree(child, destination / child.name)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())


def tree_snapshot(path):
    path = Path(path)
    if not path.exists():
        return None
    if path.is_file() or path.is_symlink():
        return {"": path.read_bytes()}
    result = {}
    for child in sorted(path.rglob("*"), key=lambda item: str(item)):
        if child.is_file():
            result[child.relative_to(path).as_posix()] = child.read_bytes()
        elif child.is_dir():
            result.setdefault(child.relative_to(path).as_posix() + "/", None)
    return result


class Installer:
    def __init__(self, home, repo, dry_run):
        self.home = Path(home)
        self.repo = Path(repo)
        self.dry_run = dry_run
        self.backup_root = None
        self.backed_up = set()
        self.failed = False

    def action(self, path, status, detail=None):
        label = str(path)
        if detail:
            label += f"  {detail}"
        print(f"ACTION  {label}  ({status})")

    def backup_relative(self, path):
        path = Path(path).resolve()
        try:
            return path.relative_to(self.home)
        except ValueError:
            drive = path.drive.rstrip(":").replace("\\", "_").replace("/", "_")
            parts = [part for part in path.parts if part not in {path.anchor, "\\", "/"}]
            return Path("external") / (drive or "root") / Path(*parts)

    def backup(self, path):
        path = Path(path)
        key = str(path.resolve()).lower()
        if key in self.backed_up or not path.exists() or self.dry_run:
            return
        if self.backup_root is None:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            candidate = self.home / ".claude" / "local" / f"backup-{stamp}"
            counter = 1
            while candidate.exists():
                candidate = self.home / ".claude" / "local" / f"backup-{stamp}-{counter}"
                counter += 1
            self.backup_root = candidate
        destination = self.backup_root / self.backup_relative(path)
        copy_tree(path, destination)
        self.backed_up.add(key)

    def removal_failed(self, path, reason):
        self.failed = True
        self.action(path, f"failed: {reason}")

    def write_bytes(self, destination, content):
        destination = Path(destination)
        if destination.is_file() and destination.read_bytes() == content:
            self.action(destination, "unchanged")
            return False
        if self.dry_run:
            self.action(destination, "would write")
            return True
        if destination.exists():
            self.backup(destination)
            if destination.is_dir():
                if not remove_path(destination, self.removal_failed):
                    self.action(destination, "failed: destination was not removed")
                    return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        self.action(destination, "written")
        return True

    def copy_file(self, source, destination):
        return self.write_bytes(destination, Path(source).read_bytes())

    def sync_directory(self, source, destination):
        source = Path(source)
        destination = Path(destination)
        if tree_snapshot(source) == tree_snapshot(destination):
            self.action(destination, "unchanged")
            return False
        if self.dry_run:
            self.action(destination, "would write")
            return True
        if destination.exists():
            self.backup(destination)
            if not remove_path(destination, self.removal_failed):
                self.action(destination, "failed: destination was not fully removed")
                return False
        copy_tree(source, destination)
        self.action(destination, "written")
        return True

    def remove(self, path, status="removed"):
        path = Path(path)
        if not path.exists() and not path.is_symlink():
            self.action(path, "skipped")
            return False
        if self.dry_run:
            self.action(path, "would remove")
            return True
        if not remove_path(path, self.removal_failed):
            return False
        self.action(path, status)
        return True


def command_path(path):
    value = str(Path(path).resolve()).replace("\\", "/")
    return f'"{value}"' if " " in value else value


def command_hook(interpreter, hook_path):
    return {
        "type": "command",
        "command": f"{command_path(interpreter)} {command_path(hook_path)}",
        "timeout": 10,
    }


def entry_mentions(entry, terms):
    if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
        return False
    for hook in entry["hooks"]:
        command = hook.get("command", "") if isinstance(hook, dict) else ""
        if any(term in str(command).lower() for term in terms):
            return True
    return False


def ensure_dict(parent, key):
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def union_list(parent, key, additions, defaults=False):
    current = parent.get(key)
    if not isinstance(current, list):
        current = ["$defaults"] if defaults else []
        parent[key] = current
    elif defaults and "$defaults" not in current:
        current.insert(0, "$defaults")
    for item in additions:
        if item not in current:
            current.append(item)


def merge_keybindings(existing, defaults):
    if not isinstance(existing, dict) or not isinstance(existing.get("bindings", []), list):
        return None
    contexts = [block.get("context") for block in defaults.get("bindings", [])]
    seen = []
    for block in existing.get("bindings", []):
        if not isinstance(block, dict):
            return None
        context = block.get("context")
        if context in contexts:
            if context in seen or not isinstance(block.get("bindings", {}), dict):
                return None
            seen.append(context)
    defaults = copy.deepcopy(defaults)
    merged = copy.deepcopy(existing)
    for key, value in defaults.items():
        merged.setdefault(key, value)
    blocks = merged.get("bindings")
    if not isinstance(blocks, list):
        blocks = []
        merged["bindings"] = blocks
    for block in defaults.get("bindings", []):
        target = next(
            (item for item in blocks if isinstance(item, dict)
             and item.get("context") == block.get("context")),
            None,
        )
        if target is None:
            blocks.append(block)
            continue
        bindings = ensure_dict(target, "bindings")
        for keystroke, action in block.get("bindings", {}).items():
            bindings.setdefault(keystroke, action)
    return merged


def git_config_value(value):
    if any(ord(character) < 32 for character in value):
        raise ValueError("git credential configuration must not contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def install_gh_credentials(installer, machine):
    begin = b"# harness gh accounts begin"
    end = b"# harness gh accounts end"
    destination = installer.home / ".gitconfig"
    accounts = validate_gh_accounts(machine, lambda key: destination)
    for login in (accounts or {}).values():
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login) or login.lower().startswith("todo"):
            installer.action(destination, f"skipped (gh account {login} is a placeholder)")
            return
    if accounts == {}:
        installer.action(destination, "skipped (no gh accounts configured)")
        return
    if destination.exists() and not destination.is_file():
        raise ValueError(".gitconfig must be a file")
    original = destination.read_bytes() if destination.is_file() else b""
    pattern = re.compile(rb"^" + begin + rb"\r?\n.*?^" + end + rb"(?:\r?\n|$)", re.MULTILINE | re.DOTALL)
    matches = list(pattern.finditer(original))
    if len(matches) > 1 or original.count(begin) != len(matches) or original.count(end) != len(matches):
        raise ValueError(".gitconfig has invalid harness gh accounts markers")
    previous = matches[0] if matches else None
    outside = original[:previous.start()] + original[previous.end():] if previous else original
    fragments = {}
    block = b""
    if accounts:
        newline_match = re.search(rb"\r?\n", previous.group() if previous else original)
        newline = newline_match.group().decode() if newline_match else "\n"
        interpreter = str(Path(sys.executable).resolve()).replace("\\", "/")
        helper_path = (installer.home / ".claude/hooks/gh_credential_helper.py").resolve().as_posix()
        helper = git_config_value(f'!"{interpreter}" -I "{helper_path}"')
        hosts = ("github.com", "gist.github.com")
        existing = {}
        scope = None
        for line in outside.decode("utf-8", errors="surrogateescape").splitlines():
            if line.lstrip().startswith("["):
                section = re.match(r'\s*\[credential(?:\s+"https://(github\.com|gist\.github\.com)")?\s*\]', line, re.IGNORECASE)
                scope = (section.group(1) or "default").lower() if section else None
            elif scope and re.match(r"\s*helper\s*=", line, re.IGNORECASE) and re.search(r"\bauth\s+git-credential\b", line.partition("=")[2]):
                existing[scope] = line
        lines = [begin.decode(), "# The installer rewrites this block on every run; re-run install.py after a Python upgrade."]
        for host in hosts:
            lines.extend([f'[credential "https://{host}"]', "\thelper =", f"\thelper = {helper}"])
            lines.append(existing.get(host, existing.get("default", "\thelper = !gh auth git-credential")))
            if "default" in accounts:
                lines.append("\tusername = " + git_config_value(accounts["default"]))
        prefixes = []
        for prefix, login in accounts.items():
            if prefix == "default":
                continue
            normalized = prefix.replace("\\", "/")
            if normalized == "~" or normalized.startswith("~/"):
                normalized = installer.home.resolve().as_posix() + normalized[1:]
            normalized = os.path.abspath(os.path.normpath(normalized)).replace("\\", "/").rstrip("/")
            prefixes.append((normalized, prefix, login))
        for normalized, prefix, login in sorted(prefixes, key=lambda entry: (len(entry[0]), entry[0].lower())):
            slug = re.sub(r"[^a-z0-9]+", "-", prefix.lower()).strip("-") or "root"
            fragment = installer.home / ".claude/local" / ("gitconfig-gh-" + slug)
            if fragment.exists() and not fragment.is_file():
                raise ValueError(f"{fragment}: git credential fragment must be a file")
            if fragment in fragments:
                raise ValueError(f"gh_accounts prefixes have the same fragment slug: {slug}")
            content = []
            for host in hosts:
                content.extend([f'[credential "https://{host}"]', "\tusername = " + git_config_value(login)])
            fragments[fragment] = ("\n".join(content) + "\n").encode("utf-8")
            lines.extend([f'[includeIf {git_config_value("gitdir/i:" + normalized + "/")}]', "\tpath = " + git_config_value(fragment.resolve().as_posix())])
        lines.append(end.decode())
        block = (newline.join(lines) + (newline if not previous or previous.group().endswith(b"\n") else "")).encode("utf-8")
    if previous:
        updated = original[:previous.start()] + block + original[previous.end():]
    else:
        separator = b"" if not original or original.endswith(b"\n") or not block else (b"\r\n" if b"\r\n" in original else b"\n")
        updated = original + separator + block
    for fragment, content in fragments.items():
        installer.write_bytes(fragment, content)
    if updated != original:
        installer.write_bytes(destination, updated)
    for fragment in (installer.home / ".claude/local").glob("gitconfig-gh-*"):
        if fragment.is_file() and fragment not in fragments:
            installer.backup(fragment)
            installer.remove(fragment)


def install_keybindings(installer, repo, home):
    keybindings_path = home / ".claude" / "keybindings.json"
    default_keybindings_path = repo / "claude" / "keybindings.json"
    if not default_keybindings_path.is_file():
        installer.action(keybindings_path, "skipped (no claude/keybindings.json in the install folder)")
        return
    try:
        defaults = read_json(default_keybindings_path)
    except (ValueError, UnicodeError, OSError):
        defaults = None
    if not isinstance(defaults, dict):
        installer.action(keybindings_path, "skipped (claude/keybindings.json in the install folder is not a JSON object)")
        return
    invalid_existing = False
    try:
        existing = read_json(keybindings_path) if keybindings_path.is_file() else {}
    except (ValueError, UnicodeError, OSError):
        existing = {}
        invalid_existing = True
    merged = merge_keybindings(existing, defaults)
    if merged is None:
        installer.action(keybindings_path, "skipped (unexpected shape, left as is)")
        return
    if invalid_existing:
        installer.action(keybindings_path, "replaced (existing file was not valid JSON, backup kept)")
    installer.write_bytes(keybindings_path, json_bytes(merged))


def install_statusline(installer, repo, home, machine):
    destination = home / ".claude" / "statusline-command.sh"
    if not machine.get("statusline", True):
        if destination.exists() or destination.is_symlink():
            installer.remove(destination, status="removed (statusline false)")
        return False
    weather = machine.get("statusline_weather")
    if weather is not None:
        config = home / ".claude" / "local" / "statusline.conf"
        if config.exists():
            installer.action(config, "unchanged")
        else:
            content = f"STATUSLINE_CITY={weather['city']}\nSTATUSLINE_LAT={json.dumps(weather['lat'])}\nSTATUSLINE_LON={json.dumps(weather['lon'])}\n"
            installer.write_bytes(config, content.encode("utf-8"))
    source = repo / "claude" / "statusline-command.sh"
    if not source.is_file():
        installer.action(destination, "skipped (no claude/statusline-command.sh in the install folder)")
        return False
    installer.copy_file(source, destination)
    return True


PROXY_SH = r"""#!/bin/sh
# Written by install.py from the machine's proxy block. HARNESS_PROXY=off forces the own login.
{resolve}
if [ "$HARNESS_PROXY" != "force" ]; then
  if [ "$HARNESS_PROXY" = "off" ] || {bypass}; then
    exec "$real" "$@"
  fi
fi
tok=$({token})
if [ -n "$tok" ] && curl -sf -m 2 -o /dev/null -H "Authorization: Bearer $tok" {probe} 2>/dev/null; then
  echo "{name} via the proxy pool" >&2
  {execute}
fi
if [ "$HARNESS_PROXY" = "force" ]; then
  echo "proxy unreachable" >&2
  exit 1
fi
echo "proxy down, {name} on this device's own login" >&2
exec "$real" "$@"
"""

PROXY_SH_RESOLVE = r"""resolve_path() {{
  target=$1
  while [ -L "$target" ]; do
    folder=$(CDPATH= cd -P "$(dirname "$target")" && pwd) || return 1
    target=$(readlink "$target") || return 1
    case "$target" in /*) ;; *) target="$folder/$target" ;; esac
  done
  folder=$(CDPATH= cd -P "$(dirname "$target")" && pwd) || return 1
  printf '%s/%s\n' "$folder" "$(basename "$target")"
}}
self=$(resolve_path "$0")
own_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
real=""
remaining=$PATH
while :; do
  folder=${{remaining%%:*}}
  [ -n "$folder" ] || folder=.
  directory=$(CDPATH= cd -P "$folder" 2>/dev/null && pwd)
  if [ -n "$directory" ] && [ "$directory" != "$own_dir" ]; then
    for candidate in "$directory/{name}"{extensions}; do
      [ -f "$candidate" ] && [ -x "$candidate" ] || continue
      resolved=$(resolve_path "$candidate")
      [ "$resolved" = "$self" ] && continue
      [ "$(dirname "$resolved")" = "$own_dir" ] && continue
      real=$candidate
      break
    done
  fi
  [ -z "$real" ] || break
  case "$remaining" in *:*) remaining=${{remaining#*:}} ;; *) break ;; esac
done
if [ -z "$real" ]; then
  echo "{name}: real binary not found on PATH" >&2
  exit 127
fi"""

PROXY_SH_ALIAS = """#!/bin/sh
# Written by install.py from the machine's proxy block. HARNESS_PROXY=off forces the own login.
HARNESS_PROXY=force exec "$(dirname "$0")/{name}" "$@"
"""

PROXY_CMD = r"""@rem Written by install.py from the machine's proxy block. HARNESS_PROXY=off forces the own login.
@echo off
setlocal DisableDelayedExpansion
{resolve}
if "%HARNESS_PROXY%"=="force" goto probe
if "%HARNESS_PROXY%"=="off" goto own
{bypass}
:probe
set "tok="
{token}
if not defined tok goto down
@rem Call lets cmd probe stubs return. The validated key alphabet and percent-free base URL make its second expansion safe.
call curl -sf -m 2 -o NUL -H "Authorization: Bearer %tok%" "{base}/v1/models" 2>NUL
if errorlevel 1 goto down
echo {name} via the proxy pool >&2
{prepare}@rem The next line ends the local scope on purpose: a batch target such as the npm codex.cmd shim would otherwise start after the implicit endlocal and lose the variables.
{execute}
exit /b %errorlevel%
:down
if "%HARNESS_PROXY%"=="force" (
  echo proxy unreachable >&2
  exit /b 1
)
echo proxy down, {name} on this device's own login >&2
:own
"%real%" %*
exit /b %errorlevel%
"""

PROXY_CMD_RESOLVE = r"""set "proxy_orig_path=%PATH%"
set "proxy_path="
for %%D in ("%PATH:;=" "%") do call :path_entry "%%~D"
set "PATH=%proxy_path%"
set "real="
{lookup}
set "PATH=%proxy_orig_path%"
if defined real goto resolved
echo {name}: real binary not found on PATH >&2
exit /b 127
:path_entry
for %%F in ("%~1\.") do if /i "%%~fF\"=="%~dp0" exit /b
if defined proxy_path (set "proxy_path=%proxy_path%;%~1") else set "proxy_path=%~1"
exit /b
:resolved"""

PROXY_CMD_ALIAS = r"""@rem Written by install.py from the machine's proxy block. HARNESS_PROXY=off forces the own login.
@echo off
setlocal DisableDelayedExpansion
set "HARNESS_PROXY=force"
"%~dp0{name}.cmd" %*
"""

PROXY_CODEX_CONFIG = """model_provider = "cliproxyapi"
model = {model}
{effort_line}
[model_providers.cliproxyapi]
base_url = {base_url}
model_catalog_url = {catalog_url}
experimental_bearer_token = {token}
name = "OpenAI"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = true
"""


def proxy_bin_dir(machine):
    return machine["proxy"].get("bin_dir", ".claude/local/bin")


def proxy_path(raw, home):
    path, _ = machine_file_path(raw, home)
    return path.resolve()


def proxy_key(machine, home, dry_run):
    if "proxy" not in machine:
        return None
    path = proxy_path(machine["proxy"]["key_file"], home)
    try:
        key = path.read_text(encoding="utf-8-sig").rstrip("\r\n")
    except FileNotFoundError:
        if dry_run:
            return None
        raise ValueError(f"proxy key file not found: {path} (copy the device's client key there by hand first)")
    if not key:
        raise ValueError(f"proxy key file must hold a non empty key on one line: {path}")
    if re.fullmatch(r"[A-Za-z0-9_-]+", key) is None:
        raise ValueError(f"proxy key file holds characters outside letters, digits, dash and underscore: {path}")
    return key


def is_proxy_launcher(path):
    try:
        return b"Written by install.py from the machine's proxy block" in path.read_bytes()
    except OSError:
        return False


def proxy_launcher_conflicts(machine, home):
    if "proxy" not in machine:
        return []
    bin_dir = proxy_path(proxy_bin_dir(machine), home)
    conflicts = []
    for name in ("claude", "codex", "claude-proxy", "codex-proxy"):
        for suffix in ("", ".cmd") if machine["os"] == "windows" else ("",):
            path = bin_dir / (name + suffix)
            if not os.path.lexists(path):
                continue
            if not is_proxy_launcher(path):
                conflicts.append(path)
    return conflicts


REMOTE_SKILL_MARKER = "Written by install.py from the machine's remote block"


def remote_skill_conflicts(machine, home):
    conflicts = []
    for name in machine.get("remote", {}):
        path = Path(home) / ".claude" / "skills" / name / "SKILL.md"
        if not os.path.lexists(path):
            continue
        try:
            managed = REMOTE_SKILL_MARKER.encode("utf-8") in path.read_bytes()
        except OSError:
            managed = False
        if not managed:
            conflicts.append(path)
    return conflicts


def check_remote_skills(machine, home, repo):
    for name in machine.get("remote", {}):
        if (Path(repo) / "claude" / "skills" / name).is_dir():
            raise ValueError(f"remote.{name} conflicts with a repository skill")
    conflicts = remote_skill_conflicts(machine, home)
    if conflicts:
        raise ValueError(f"{conflicts[0]} exists and was not written by install.py (choose another remote name or move that file)")


def install_remote_skills(installer, machine, repo):
    tool = shlex.quote((installer.repo / "tools" / "remote_prompt.py").resolve().as_posix())
    for name in machine.get("remote", {}):
        description = f'Run a prompt on the {name} machine through claude -p over ssh (or locally when this is that machine). Use when the user types /{name} followed by a prompt, "ask {name} to", "run on {name}", "on {name}:", or wants a goal executed on {name}.'
        content = f'''---
name: {name}
description: {json.dumps(description)}
---

# /{name}

{REMOTE_SKILL_MARKER}. Edit `remote.{name}` in the machine's local json and run install.py again; never edit this file.

The argument is the prompt to run on `{name}`; it may start with a slash command such as `/auto <goal>`, which the remote session expands itself. Steps:

1. Write the argument verbatim to `<scratchpad>/remote-{name}-<timestamp>.md` (the session scratchpad, never the project).
2. Short prompts (a question, a status, a one step task): run `python {tool} {name} --prompt-file <that file>` from the Bash tool and relay the stdout answer; the stderr footer carries the session id, turns, duration and cost; a `permission denied` block in stderr means the remote session was blocked and the user must decide.
3. Long prompts (anything starting with `/auto`, `/ship`, or a task that would run Codex): add `--detach`, print the run id, then poll `python {tool} {name} --collect` no more than once every five minutes (Monitor or a background Bash loop), and relay the result when it arrives. Exit 4 means still running. Exit 3 means finished without a result. The tool passes a headless system prompt telling the remote session to keep long jobs in the foreground or under setsid nohup; `--no-preamble` skips it.
4. Follow-ups in the same remote conversation: add `--continue`. Collect a detached run first; `--continue` refuses an uncollected run. Use `--resume <session id>` to choose a session explicitly.
5. `--cwd <folder>` runs in another folder on that machine; `--dry-run` shows the exact command without running it. For an ssh entry, from Git Bash prefix the command with `MSYS_NO_PATHCONV=1` and quote the value, such as `--cwd '/srv/app'` or `--cwd '~/x'`. This disables path conversion for every argument, so give `--prompt-file` and `--machine-json` values as Windows paths such as `C:/Users/...` or `F:/...`, not `/c/Users/...` forms, to avoid a `No such file or directory` error.

An ssh entry must point at a machine whose login shell is POSIX; a Windows host over ssh is not supported.

Never put the prompt on the command line; always use the prompt file. Never run two detached runs on the same machine at once without the user asking.
'''
        installer.write_bytes(installer.home / ".claude" / "skills" / name / "SKILL.md", content.encode("utf-8"))


CODEX_PROXY_OVERRIDES = (
    "model_provider=cliproxyapi",
    'model_providers.cliproxyapi.name="OpenAI"',
    "model_providers.cliproxyapi.base_url={base_url}",
    "model_providers.cliproxyapi.model_catalog_url={catalog_url}",
    'model_providers.cliproxyapi.wire_api="responses"',
    "model_providers.cliproxyapi.requires_openai_auth=false",
    "model_providers.cliproxyapi.supports_websockets=true",
    'model_providers.cliproxyapi.env_key="CLIPROXY_API_KEY"',
)

# Hide stale ChatGPT login refresh errors on proxied runs, measured 26 to 0 on codex 0.160.0; keep error so other errors remain visible.
PROXY_CODEX_RUST_LOG = "error,codex_login=off"


def codex_proxy_overrides(base):
    return [value.format(base_url=json.dumps(base + "/v1"), catalog_url=json.dumps(base + "/v1/models")) for value in CODEX_PROXY_OVERRIDES]


def proxy_launchers(machine, home, base):
    proxy = machine["proxy"]
    bin_dir = proxy_path(proxy_bin_dir(machine), home)
    files = {}
    overrides = codex_proxy_overrides(base)
    for name in ("claude", "codex"):
        command = proxy.get(name + "_command")
        command_path = proxy_path(os.path.expandvars(os.path.expanduser(command)), home) if command else None
        profile = home / (".claude-proxy/settings.json" if name == "claude" else ".codex-proxy/config.toml")
        profile_sh = shlex.quote(profile.as_posix())
        resolve = "real=" + shlex.quote(command_path.as_posix()) if command_path else PROXY_SH_RESOLVE.format(
            name=name, extensions=f' "$directory/{name}.exe" "$directory/{name}.cmd"' if machine["os"] == "windows" else "",
        )
        if name == "claude":
            bypass = '[ "${ANTHROPIC_BASE_URL+x}" = x ]'
            token = r"""sed -n 's/.*"ANTHROPIC_AUTH_TOKEN": *"\([^"]*\)".*/\1/p' """ + profile_sh + " 2>/dev/null"
            execute = f'ANTHROPIC_BASE_URL={shlex.quote(base)} ANTHROPIC_AUTH_TOKEN="$tok" CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1 exec "$real" "$@"'
        else:
            bypass = '[ "${CODEX_HOME+x}" = x ] || [ "${OPENAI_BASE_URL+x}" = x ]'
            token = r"""sed -n 's/^experimental_bearer_token *= *"\([^"]*\)".*/\1/p' """ + profile_sh + " 2>/dev/null"
            config_args = " ".join("-c " + shlex.quote(value) for value in overrides)
            execute = f'''# The launcher recognises a subcommand only as the first word, so put options after it. The same rule applies to nested subcommands.
  sub=
  nested=
  case "${{1-}}" in
    ""|-*|help|*[[:space:]]*|*'"'*) ;;
    *)
      sub=$1
      shift
      if [ "$sub" = "exec" ]; then
        case "${{1-}}" in
          resume|fork|review) nested=$1; shift ;;
        esac
      fi
      ;;
  esac
  RUST_LOG="${{RUST_LOG:-{PROXY_CODEX_RUST_LOG}}}" CLIPROXY_API_KEY="$tok" exec "$real" ${{sub:+"$sub"}} ${{nested:+"$nested"}} {config_args} "$@"'''
        files[bin_dir / name] = PROXY_SH.format(
            resolve=resolve, bypass=bypass, token=token, probe=shlex.quote(base + "/v1/models"),
            name=name, execute=execute.rstrip(),
        ).encode("utf-8")
        files[bin_dir / (name + "-proxy")] = PROXY_SH_ALIAS.format(name=name).encode("utf-8")
        if machine["os"] != "windows":
            continue
        lookup = "\n".join(
            f'for /f "delims=" %%R in (\'where $PATH:{name}{suffix} 2^>NUL\') do if not defined real set "real=%%R"'
            for suffix in (".exe", ".cmd", "")
        )
        resolve = f'set "real={command_path}"' if command_path else PROXY_CMD_RESOLVE.format(name=name, lookup=lookup)
        prepare = ""
        if name == "claude":
            bypass = "if defined ANTHROPIC_BASE_URL goto own"
            token = f'''for /f "tokens=2 delims=:" %%T in ('findstr /c:"\\"ANTHROPIC_AUTH_TOKEN\\"" "{profile}" 2^>NUL') do set "tok=%%T"
if not defined tok goto down
set "tok=%tok: =%"
if not defined tok goto down
set "tok=%tok:,=%"
if not defined tok goto down
set "tok=%tok:"=%"'''
            execute = f'endlocal & set "ANTHROPIC_BASE_URL={base}" & set "ANTHROPIC_AUTH_TOKEN=%tok%" & set "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1" & "%real%" %*'
        else:
            bypass = "if defined CODEX_HOME goto own\nif defined OPENAI_BASE_URL goto own"
            token = f'''for /f "tokens=2 delims==" %%T in ('findstr /b /c:"experimental_bearer_token" "{profile}" 2^>NUL') do set "tok=%%T"
if not defined tok goto down
set "tok=%tok: =%"
if not defined tok goto down
set "tok=%tok:"=%"'''
            config_args = " ".join('-c "' + value.replace('"', r'\"') + '"' for value in overrides)
            prepare = f'''@rem The launcher recognises a subcommand only as the first word, so put options after it. The same rule applies to nested subcommands.
@rem Cmd does not treat backslash as an escape. Callers pass prompts through stdin or a file, never on the command line.
set "rest="
set "rest1="
set "rest2="
set "second="
set args=%*
set "captured="
setlocal EnableDelayedExpansion
for /f "eol= tokens=1* delims=\t " %%A in ("!args!") do endlocal & set "captured=1" & set "sub=%%A" & set rest1=%%B
if not defined captured goto proxy_root_local
setlocal EnableDelayedExpansion
set "bare=!sub:"=!"
if not "!bare!"=="!sub!" goto proxy_root_local
if "!sub:~0,1!"=="-" goto proxy_root_local
if /i "!sub!"=="help" goto proxy_root_local
endlocal
@rem Delayed expansion reads the saved arguments without reparsing metacharacters.
@rem End the nested scope before assigning the remainder to preserve literal exclamation marks.
if not defined rest1 goto proxy_select
set "captured="
setlocal EnableDelayedExpansion
for /f "eol= tokens=2* delims=\t " %%A in ("!args!") do endlocal & set "captured=1" & set "second=%%A" & set rest2=%%B
if not defined captured goto proxy_root_local
:proxy_select
setlocal EnableDelayedExpansion
@rem Nested command grammar matches Codex 0.160.0.
if /i "!sub!"=="exec" for %%N in (resume fork review) do if /i "!second!"=="%%N" goto proxy_nested
set "rest=!rest1!"
goto proxy_selected
:proxy_nested
set "sub=exec !second!"
set "rest=!rest2!"
:proxy_selected
endlocal & set "sub=%sub%" & set rest=%rest%
goto proxy_execute
:proxy_root_local
endlocal
:proxy_root
set "sub="
set rest=%*
:proxy_execute
'''
            execute = f'endlocal & set "CLIPROXY_API_KEY=%tok%" & (if not defined RUST_LOG set "RUST_LOG={PROXY_CODEX_RUST_LOG}") & "%real%" %sub% {config_args} %rest%'
        files[bin_dir / (name + ".cmd")] = PROXY_CMD.format(
            resolve=resolve, bypass=bypass, token=token, base=base, name=name, prepare=prepare, execute=execute,
        ).replace("\n", "\r\n").encode("utf-8")
        files[bin_dir / (name + "-proxy.cmd")] = PROXY_CMD_ALIAS.format(name=name).replace("\n", "\r\n").encode("utf-8")
    return files


def proxy_path_note(machine, home):
    bin_dir = proxy_path(proxy_bin_dir(machine), home)
    windows = machine["os"] == "windows"
    path_dirs = [
        Path(os.path.expandvars(os.path.expanduser(part))).resolve()
        for part in os.environ.get("PATH", "").split(os.pathsep)
    ]
    labels = [str(path).casefold() if windows else str(path) for path in path_dirs]
    bin_label = str(bin_dir).casefold() if windows else str(bin_dir)
    bin_index = labels.index(bin_label) if bin_label in labels else len(path_dirs)
    for index, directory in enumerate(path_dirs):
        if labels[index] == bin_label:
            continue
        for name in ("claude", "codex"):
            for suffix in ("", ".exe", ".cmd") if windows else ("",):
                candidate = directory / (name + suffix)
                if candidate.is_file() and (windows or os.access(candidate, os.X_OK)) and not is_proxy_launcher(candidate):
                    if bin_index > index:
                        print(f"proxy launchers in {bin_dir}: this folder must come before {directory} on PATH, or the bare claude and codex skip the pool")
                    return
    if bin_index == len(path_dirs):
        print(f"proxy launchers in {bin_dir}: make sure this folder precedes the real claude and codex on PATH")


def install_proxy(installer, machine, statusline_installed, key, conflicts=()):
    if "proxy" not in machine:
        return
    proxy = machine["proxy"]
    home = installer.home
    host = proxy["host"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    base = f"{proxy.get('scheme', 'http')}://{host}:{proxy.get('port', 8317)}"
    settings_path = home / ".claude-proxy" / "settings.json"
    config_path = home / ".codex-proxy" / "config.toml"
    auth_path = home / ".codex-proxy" / "auth.json"
    launchers = proxy_launchers(machine, home, base)
    if key is None:
        path = proxy_path(proxy["key_file"], home)
        for destination in (settings_path, config_path, auth_path, *launchers):
            status = "would refuse (existing file not written by install.py)" if destination in conflicts else f"would write (key file missing: {path})"
            installer.action(destination, status)
    else:
        try:
            settings = read_json(settings_path)
        except (OSError, ValueError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        ensure_dict(settings, "env").update({
            "ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": key,
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        })
        if statusline_installed:
            settings.setdefault("statusLine", {"type": "command", "command": STATUSLINE_COMMAND, "padding": 0})
        existing = config_path.read_bytes().decode("utf-8-sig") if config_path.is_file() else ""
        effort = machine.get("codex_effort", DEFAULT_CODEX_EFFORT)
        effort_line = f'model_reasoning_effort = {json.dumps(effort)}\n' if effort is not None else ""
        if effort is None:
            previous_effort = top_level_toml_line(existing.encode("utf-8"), "model_reasoning_effort")
            if previous_effort:
                effort_line = (previous_effort[1] + previous_effort[2]).decode("utf-8")
        config = PROXY_CODEX_CONFIG.format(
            model=json.dumps(machine.get("codex_model", "gpt-6.1-sol")), base_url=json.dumps(base + "/v1"),
            catalog_url=json.dumps(base + "/v1/models"), token=json.dumps(key), effort_line=effort_line,
        )
        match = re.search(r"^\[projects\.", existing, re.MULTILINE)
        tail = existing[match.start():] if match else ""
        if tail:
            config += "\n" + tail
        profiles = {settings_path: json_bytes(settings), config_path: config.encode("utf-8"), auth_path: json_bytes({"OPENAI_API_KEY": key})}
        for destination, content in {**profiles, **launchers}.items():
            if destination in conflicts:
                installer.action(destination, "would refuse (existing file not written by install.py)")
                continue
            installer.write_bytes(destination, content)
            if not installer.dry_run and machine["os"] != "windows" and os.name != "nt":
                os.chmod(destination, 0o600 if destination in profiles else 0o755)
    proxy_path_note(machine, home)


def install_local_markdown(installer, source, destination):
    """Keep a filled personal section when the supplied section is missing or a placeholder."""
    content = source.read_bytes().decode("utf-8-sig")
    section = re.compile(r"^## Who I am, for calibration(?:\r?\n|\Z).*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    supplied = section.search(content)
    placeholders = ("Not written yet", "Replace this paragraph")
    if (supplied is None or any(text in supplied.group() for text in placeholders)) and destination.is_file():
        existing = destination.read_bytes().decode("utf-8-sig")
        personal = section.search(existing)
        if (
            not existing.startswith("# No local machine facts on this machine yet")
            and personal
            and not any(text in personal.group() for text in placeholders)
        ):
            replacement = personal.group()
            if supplied is None:
                content = content.rstrip("\r\n") + "\n\n" + replacement
            else:
                if supplied.end() < len(content) and not replacement.endswith("\n"):
                    replacement += "\n"
                content = content[:supplied.start()] + replacement + content[supplied.end():]
    installer.write_bytes(destination, content.encode("utf-8"))


def top_level_toml_line(content, key):
    for token in re.finditer(rb"""#[^\r\n]*|\"\"\"|'''|"(?:\\[^\r\n]|[^"\\\r\n])*"|'[^'\r\n]*'""", content):
        if token[0] in (b'"""', b"'''"):
            line_start = max(content.rfind(b"\n", 0, token.start()), content.rfind(b"\r", 0, token.start())) + 1
            content = content[:line_start]
            break
    start = rb"(?:\A|(?<=\n)|(?<=\r))(?:\xef\xbb\xbf)?"
    header = re.search(start + rb"[ \t]*\[", content)
    top_level = content[:header.start()] if header else content
    return re.search(start + rb"(" + re.escape(key.encode("ascii")) + rb"[ \t]*=[^\r\n]*)(\r\n|\r|\n|$)", top_level)


def rewrite_codex_toml_key(content, key, desired, parsed):
    assignment = top_level_toml_line(content, key)
    unrecognised = f"skipped ({key} present in a form the installer does not rewrite)"
    encoded = json.dumps(desired, ensure_ascii=False)[1:-1].replace("\x7f", "\\u007f").encode("utf-8")
    line = key.encode("ascii") + b' = "' + encoded + b'"'
    if assignment:
        value = re.fullmatch(re.escape(key.encode("ascii")) + rb'''[ \t]*=[ \t]*(["'])([^"'\r\n]*)\1[ \t]*(?:#.*)?''', assignment[1])
        if value is None:
            return content, unrecognised
        if value[1] == b"'":
            if "'" in desired or any(ord(character) < 32 or ord(character) == 127 for character in desired):
                return content, unrecognised
            encoded = desired.encode("utf-8")
        if value[2] == encoded:
            return content, "unchanged"
        start = assignment.start(1) + value.start(2)
        end = assignment.start(1) + value.end(2)
        updated = content[:start] + encoded + content[end:]
    else:
        if parsed is None:
            return content, "skipped (no tomllib to confirm the key is absent)"
        if key in parsed:
            return content, unrecognised
        model = top_level_toml_line(content, "model") if key == "model_reasoning_effort" else None
        ending = re.search(rb"\r\n|\n|\r", content)
        newline = model[2] if model and model[2] else ending[0] if ending else b"\n"
        offset = model.end() if model else 3 if content.startswith(b"\xef\xbb\xbf") else 0
        separator = newline if model and not model[2] else b""
        updated = content[:offset] + separator + line + newline + content[offset:]
    return updated, None


def install_codex_effort(installer, machine):
    values = [("model", machine.get("codex_model")),
              ("model_reasoning_effort", machine.get("codex_effort", DEFAULT_CODEX_EFFORT))]
    values = [(key, value) for key, value in values if value is not None]
    if not values:
        return
    path = installer.home / ".codex" / "config.toml"
    if not path.is_file():
        installer.action(path, "skipped (no ~/.codex/config.toml)")
        return
    content = path.read_bytes()
    parsed = None
    try:
        import tomllib
    except ImportError:
        pass
    else:
        try:
            parsed = tomllib.loads(content.decode("utf-8-sig"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            installer.action(path, "skipped (config.toml did not parse)")
            return
    statuses = []
    for key, value in values:
        content, status = rewrite_codex_toml_key(content, key, value, parsed)
        statuses.append(status)
    if all(status == "unchanged" for status in statuses):
        installer.action(path, "unchanged")
        return
    for status in statuses:
        if status is not None and status != "unchanged":
            installer.action(path, status)
    if None in statuses:
        installer.write_bytes(path, content)


def merge_settings(existing, machine, home, *, statusline_available=True, installer=None):
    settings = existing if isinstance(existing, dict) else {}
    statusline = settings.get("statusLine")
    harness_statusline = isinstance(statusline, dict) and statusline.get("command") == STATUSLINE_COMMAND
    if machine.get("statusline", True):
        if "statusLine" in settings and not harness_statusline:
            if installer is not None:
                installer.action(home / ".claude" / "settings.json", "statusLine kept (existing custom status line)")
        elif statusline_available:
            settings["statusLine"] = {"type": "command", "command": STATUSLINE_COMMAND, "padding": 0}
    elif harness_statusline:
        settings.pop("statusLine")
    hooks = ensure_dict(settings, "hooks")
    hook_dir = home / ".claude" / "hooks"
    interpreter = Path(sys.executable)

    pre = hooks.get("PreToolUse")
    if not isinstance(pre, list):
        pre = []
    pre = [
        entry for entry in pre
        if not entry_mentions(
            entry,
            ("codex-first-guard", "secret-guard", "codex_first_guard", "secret_guard", "draft_wrap_guard"),
        )
    ]
    edit_hooks = []
    if machine.get("codex_first", True):
        edit_hooks.append(command_hook(interpreter, hook_dir / "codex_first_guard.py"))
    edit_hooks.extend([
        command_hook(interpreter, hook_dir / "secret_guard.py"),
        command_hook(interpreter, hook_dir / "draft_wrap_guard.py"),
    ])
    pre.extend(
        [
            {
                "matcher": "Edit|MultiEdit|Write|NotebookEdit",
                "hooks": edit_hooks,
            },
            {
                "matcher": "Bash|PowerShell",
                "hooks": [command_hook(interpreter, hook_dir / "secret_guard.py")],
            },
        ]
    )
    hooks["PreToolUse"] = pre

    session = hooks.get("SessionStart")
    if not isinstance(session, list):
        session = []
    session = [
        entry for entry in session
        if not entry_mentions(entry, ("load-context", "session_context"))
    ]
    session.append(
        {
            "matcher": "startup|resume|clear|compact",
            "hooks": [command_hook(interpreter, hook_dir / "session_context.py")],
        }
    )
    hooks["SessionStart"] = session

    denied = hooks.get("PermissionDenied")
    if not isinstance(denied, list):
        denied = []
    denied = [
        entry for entry in denied
        if not entry_mentions(entry, ("permission_denied_log",))
    ]
    denied.append(
        {"hooks": [command_hook(interpreter, hook_dir / "permission_denied_log.py")]}
    )
    hooks["PermissionDenied"] = denied

    post = hooks.get("PostToolUse", [])
    if not isinstance(post, list):
        post = []
    post = [
        entry for entry in post
        if not entry_mentions(entry, ("context_save_nudge",))
    ]
    if not nudge_disabled(machine.get("context_save_at")):
        post.append(
            {
                "matcher": "Bash|PowerShell|Edit|MultiEdit|Write|Agent",
                "hooks": [command_hook(interpreter, hook_dir / "context_save_nudge.py")],
            }
        )
    hooks["PostToolUse"] = post

    desired = machine.get("settings")
    if not isinstance(desired, dict):
        desired = {}
    efforts = machine.get("claude_effort", DEFAULT_CLAUDE_EFFORT)
    if efforts:
        models = ensure_dict(settings, "modelSettings")
        for model, effort in efforts.items():
            ensure_dict(models, model)["effortLevel"] = effort
    permissions = ensure_dict(settings, "permissions")
    union_list(permissions, "allow", desired.get("permissions_allow") or [])
    union_list(permissions, "deny", desired.get("permissions_deny") or [])
    asks = desired.get("permissions_ask") or []
    if asks:
        union_list(permissions, "ask", asks)

    environment = ensure_dict(settings, "env")
    machine_environment = desired.get("env")
    if isinstance(machine_environment, dict):
        for key, value in machine_environment.items():
            environment[key] = value
    if "cleanupPeriodDays" in desired:
        settings["cleanupPeriodDays"] = desired["cleanupPeriodDays"]

    automatic_environment = desired.get("autoMode_environment")
    automatic_allow = desired.get("autoMode_allow")
    if isinstance(automatic_environment, list) and automatic_environment:
        ensure_dict(settings, "autoMode")["environment"] = list(automatic_environment)
    if isinstance(automatic_allow, list) and automatic_allow:
        union_list(ensure_dict(settings, "autoMode"), "allow", automatic_allow, True)
    return settings


def dotted_get(value, dotted):
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None, False
        current = current[part]
    return current, True


def dotted_remove(value, dotted):
    parts = dotted.split(".")
    parents = []
    current = value
    for part in parts[:-1]:
        parents.append((current, part))
        current = current[part]
    del current[parts[-1]]
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break


def dotted_append_defaults(value, dotted, additions):
    parts = dotted.split(".")
    current = value
    for part in parts[:-1]:
        current = ensure_dict(current, part)
    union_list(current, parts[-1], additions, True)


def machine_names(repo):
    return sorted(
        path.stem for path in (repo / "machines").glob("*.json")
        if not path.name.endswith(".local.json")
    )


def bundle_commit(repo):
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return "an uncommitted tree"
    if commit.returncode == 0 and status.returncode == 0 and not status.stdout.strip():
        return commit.stdout.strip()
    return "an uncommitted tree"


PLAN_TOOLS_EXCLUDED = (
    "tools/masterplan.py", "tools/masterplan_registry.py", "tools/masterplan_test.py",
    "tools/daily_collect.py", "tools/daily_collect_test.py", "tools/project_init.py",
    "tools/project_init_test.py", "tools/transcripts.py", "tools/transcripts_test.py",
    "templates/project-masterplan.json", "templates/project-plan.md", "templates/project-structure.md",
    "audit/", "claude/skills/masterplan/", "claude/skills/progress/", "claude/skills/daily/",
    "claude/skills/project-init/", "claude/skills/harness-audit/", "claude/skills/relay-prompt/",
    "claude/skills/t3-insights/",
)


ADDONS = {
    "daily": {
        "description": "The daily skill and its collector: one day of work in a repository summarized for a person who was not at the keyboard.",
        "paths": ("claude/skills/daily/", "tools/daily_collect.py", "tools/daily_collect_test.py"),
        "steps": (
            "1. Unzip this file into the harness install folder, the folder that holds install.py. Only the files listed above and this ADDON.md are written.",
            '2. Add a `daily` object to `~/.claude/local/machine.local.json` (create the file as `{}` first if it does not exist): `"daily": {"output": "<folder for the summaries>"}`. Optional keys: `"roots"`, a list of folders scanned when the person asks for the machine-wide daily. The collector reads the merged `~/.claude/local/machine.json`, which the next step writes.',
            "3. Run `python install.py` from the install folder with the interpreter the machine file names (the machine name is remembered from the first install). It copies the skill into `~/.claude/skills/` and reruns the hook tests.",
            "4. Restart Claude Code and type `daily`.",
        ),
    },
    "usage": {
        "description": "The usage skill and the local usage page: Claude and Codex five hour and seven day meters with reset countdowns, served on loopback from the status line cache and the local Codex session files.",
        "paths": (
            "monitor/local.py",
            "monitor/local.html",
            "monitor/heartbeat.py",
            "monitor/local_test.py",
            "monitor/heartbeat_test.py",
            "monitor/fixtures/proxy-credentials.json",
            "monitor/USAGE.md",
            "claude/skills/usage/SKILL.md",
        ),
        "steps": (
            "1. Unzip this file into the harness install folder, the folder that holds install.py. Only the files listed above and this ADDON.md are written.",
            "2. Run `python install.py` from the install folder with the interpreter the machine file names (the machine name is remembered from the first install). It copies the skill into `~/.claude/skills/` and reruns the hook tests. No key in `machine.local.json` is needed.",
            "3. Restart Claude Code and type `usage`. The page opens on http://127.0.0.1:9000; `monitor/USAGE.md` says what it reads, what it writes and what stays off.",
        ),
    },
}


class BundleRefusal(ValueError):
    pass


class NoDomainLists(BundleRefusal):
    pass


class BundleTermList(list):
    def __init__(self, terms, content, path):
        super().__init__(terms)
        self.fingerprint = hashlib.sha256(content).hexdigest()[:8]
        self.path = path


def parse_bundle_terms(text, filename):
    terms = []
    for line_number, line in enumerate(text.splitlines(), 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        substring = value.lower().startswith("substr:")
        if substring or value.lower().startswith("term:"):
            value = value.split(":", 1)[1].strip()
        if not value:
            raise BundleRefusal(f"{filename}:{line_number}: empty term")
        if any(character != " " and unicodedata.category(character)[0] in "CZ" for character in value):
            raise BundleRefusal(f"{filename}:{line_number}: term contains a control or separator character")
        value = re.sub(" +", " ", value)
        expression = re.escape(value).replace(r"\ ", r"\s+")
        prefix = r"(?<!\w)" if not substring and re.match(r"\w", value[0]) else ""
        suffix = r"(?!\w)" if not substring and re.match(r"\w", value[-1]) else ""
        pattern = re.compile(prefix + expression + suffix, re.IGNORECASE)
        terms.append((value, pattern))
    return terms


def read_bundle_terms(path, filename):
    try:
        with path.open("rb") as handle:
            content = handle.read()
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BundleRefusal(f"{filename}: is not UTF-8") from exc
    except OSError as exc:
        raise BundleRefusal(f"{filename}: {exc}") from exc
    return BundleTermList(parse_bundle_terms(text, filename), content, path)


def bundle_forbidden_terms():
    path = Path(__file__).resolve().with_name("bundle-terms.txt")
    if not path.exists():
        raise BundleRefusal("bundle-terms.txt is missing next to install.py")
    return read_bundle_terms(path, str(path))


def dedupe_terms(terms, excluded=()):
    seen = {term[1].pattern for term in excluded}
    unique = {}
    for term in terms:
        key = term[1].pattern
        if key not in seen:
            unique.setdefault(key, term)
    return list(unique.values())


def bundle_terms_directory(home):
    return home / ".claude" / "local" / "bundle-terms"


def load_bundle_terms(home):
    global_terms = bundle_forbidden_terms()
    folder = bundle_terms_directory(home)
    try:
        paths = []
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() == ".txt":
                paths.append(path)
            else:
                print(f"bundle gate: ignoring {path.name} (not a .txt list)")
    except FileNotFoundError:
        paths = []
    except OSError as exc:
        raise BundleRefusal(f"{folder}: {exc}") from exc
    if not paths:
        raise NoDomainLists(f"no domain lists under {folder} (one file per engagement or product family, see README)")
    domain_paths = {}
    for path in paths:
        key = path.stem.casefold()
        if key in domain_paths:
            raise BundleRefusal(f"domain lists {domain_paths[key]} and {path} have the same case-insensitive name")
        domain_paths[key] = path
    domains = {key: read_bundle_terms(path, str(path)) for key, path in domain_paths.items()}
    for terms in domains.values():
        if not terms:
            raise BundleRefusal(f"{terms.path} holds no terms")
    return global_terms, domains


def require_bundle_domains(home, domains, required):
    for key, (domain, owners) in required.items():
        if key not in domains:
            path = bundle_terms_directory(home) / f"{domain}.txt"
            raise BundleRefusal(f"domain {domain} is owned by {', '.join(sorted(owners))} but {path} does not exist")


def all_bundle_terms(home, repo=None, report=None, *, details=None):
    global_terms, domains = load_bundle_terms(home)
    if repo is not None:
        required = {}
        names = tracked_machine_names(repo)
        texts = committed_machine_texts(repo, names) if (repo / ".git").exists() else None
        for name in names:
            for domain in machine_owns(repo, name, texts=texts):
                required.setdefault(domain.casefold(), (domain, set()))[1].add(name)
        require_bundle_domains(home, domains, required)
    global_terms = dedupe_terms(global_terms)
    domain_terms = dedupe_terms([term for terms in domains.values() for term in terms], excluded=global_terms)
    if report is not None:
        report(len(global_terms), len(domain_terms), len(domains))
    if details is not None:
        details.update(global_count=len(global_terms), domain_count=len(domain_terms), domains=domains)
    return global_terms + domain_terms


def bundle_install_text(name, date, commit, gate_record, *, harvest=True, publish=False, machine=None):
    heading = f"# Harness bundle for {name}, built {date} from {commit}\n\n{gate_record}\n\n"
    first = f"1. On a first install, unpack the zip as the harness install folder named in machines/{name}.md. On every later update, run `python install.py --unpack <zip>` inside the existing install folder (use the interpreter the machine file names), which writes the shipped files, removes files the previous release shipped and this one does not, and keeps everything else (handoffs, `.scratch/`, `bundles/`, `inbox/`, your local machine files, which it backs up under `.scratch/` before replacing)."
    first += " If the install folder's install.py does not know `--unpack` yet, extract install.py from the zip first (`unzip -o <zip> install.py` or Expand-Archive for that one file) and run that one."
    restart = "4. Restart Claude Code and run /setup; it asks for what this machine still needs (who you are, git identities, gh account, Codex model, engagement rules) and writes the local files."
    if publish:
        heading = f"# Harness bundle for {name}, built {date}\n\n"
        first = f"1. Clone the repository as the harness install folder named in machines/{name}.md, or unpack this folder there. Update it later with git pull; a release replaces the shared files and never touches your local files."
        restart = "4. Restart Claude Code and run /setup; it asks who you are, your git identities and gh account, and whether you use Codex CLI, and writes the local files."
    elif harvest:
        restart += " Memories travel back with `python harvest.py --export <folder>`."
    proxy_step = ""
    dry_run_step = 2
    real_run_step = 3
    if machine is not None and "proxy" in machine:
        proxy_step = f"2. Before running install.py, copy the device's client key to `{machine['proxy']['key_file']}` by hand. The proxy launchers land in `{proxy_bin_dir(machine)}` (relative to the home unless absolute) and must precede the real binaries on PATH.\n"
        dry_run_step = 3
        real_run_step = 4
        restart = "5." + restart[2:]
    return (
        f"{heading}"
        f"{first}\n"
        f"{proxy_step}"
        f"{dry_run_step}. Dry run and read every line: `python install.py --machine {name} --dry-run` (use the interpreter the machine file names; where the permission classifier blocks config edits, run the real install with the ! prefix).\n"
        f"{real_run_step}. Real run: `python install.py --machine {name}`. The hook tests run last and all must pass.\n"
        f"{restart}\n"
    )


def bundle_entry_adders(entries, *, plan_tools=True):
    def add(source, relative):
        source = Path(source)
        relative = Path(relative)
        normalized = relative.as_posix().casefold()
        if not plan_tools and any(
            normalized.startswith(item.casefold()) if item.endswith("/") else normalized == item.casefold()
            for item in PLAN_TOOLS_EXCLUDED
        ):
            return
        if "__pycache__" in source.parts or source.suffix.lower() == ".pyc":
            return
        filename = relative.name.casefold()
        if any(part.casefold() in {"bundle-terms", "bundle-terms.txt"} for part in relative.parts) or filename.startswith("bundle-terms") or filename.endswith("bundle-terms.txt"):
            raise BundleRefusal(f"terms file inside the bundle tree: {relative.as_posix()}")
        entries[relative.as_posix()] = source.read_bytes()

    def add_tree(source, relative, excluded=None):
        source = Path(source)
        if not source.is_dir():
            return
        excluded = {item.rstrip("/").casefold() for item in excluded or []}
        prefixes = tuple(item + "/" for item in excluded)
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            child = path.relative_to(source)
            normalized = child.as_posix().casefold()
            if normalized in excluded or normalized.startswith(prefixes):
                continue
            add(path, Path(relative) / child)

    return add, add_tree


def bundle_file_entries(repo, name, date, gate_record, *, harvest=True, plan_tools=True, publish=False, machine=None):
    entries = {}
    add, add_tree = bundle_entry_adders(entries, plan_tools=plan_tools)
    add(repo / "install.py", "install.py")
    add(repo / "install-manifest.json", "install-manifest.json")
    add(repo / "claude" / "CLAUDE.md", "claude/CLAUDE.md")
    add(repo / "claude" / "keybindings.json", "claude/keybindings.json")
    add(repo / "claude" / "statusline-command.sh", "claude/statusline-command.sh")
    for source in sorted((repo / "claude" / "hooks").glob("*.py")):
        add(source, Path("claude/hooks") / source.name)
    add_tree(repo / "claude" / "skills", "claude/skills", excluded={"usage"} if harvest else {"harvest", "usage"})
    agents = repo / "claude" / "agents"
    if agents.is_dir():
        for source in sorted(agents.glob("*.md")):
            add(source, Path("claude/agents") / source.name)
    add_tree(
        repo / "templates",
        "templates",
        excluded={"humanizer-install.md"},
    )
    add(repo / "codex" / "AGENTS.md", "codex/AGENTS.md")
    add(repo / "codex" / "config.template.toml", "codex/config.template.toml")
    for source in sorted((repo / "audit").glob("*.py")):
        add(source, Path("audit") / source.name)
    for source in sorted((repo / "checkers").glob("*.py")):
        add(source, Path("checkers") / source.name)
    for source in sorted((repo / "tools").glob("*.py")):
        add(source, Path("tools") / source.name)
    for source in sorted((repo / "tools").glob("*.ps1")):
        add(source, Path("tools") / source.name)
    add_tree(repo / "tools" / "pr_review_prompts", "tools/pr_review_prompts")
    if harvest:
        add(repo / "harvest.py", "harvest.py")
    if publish:
        for filename in ("README.md", "LICENSE", "THIRD-PARTY-NOTICES.md", ".gitignore", ".gitattributes"):
            source = repo / "publish" / filename
            if not source.is_file():
                raise BundleRefusal(f"publish/{filename} is missing")
            add(source, filename)
    for suffix in (".md", ".json", ".local.md", ".local.json"):
        source = repo / "machines" / f"{name}{suffix}"
        if source.is_file():
            add(source, Path("machines") / source.name)
    commit = bundle_commit(repo)
    entries["INSTALL.md"] = bundle_install_text(name, date, commit, gate_record, harvest=harvest, publish=publish, machine=machine).encode("utf-8")
    if not publish:
        entries["MANIFEST.txt"] = ("\n".join(sorted([*entries, "MANIFEST.txt"])) + "\n").encode("utf-8")
    return unpack_build_entries(entries, repo)


def gate_fragment(value):
    fragments = [run for piece in value.split(" ") for run in re.findall(r"[\x00-\x7f]+", piece)]
    return max(fragments, key=len).casefold() if fragments else None


def bundle_gate(entries, terms):
    prefiltered = [(value, pattern, gate_fragment(value)) for value, pattern in terms]
    hits = 0
    for relative, content in entries:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            print(f"GATE {relative}: binary or non-UTF-8 content")
            hits += 1
            continue
        folded = text.casefold().replace("i\u0307", "i").replace("\u0131", "i")
        live = [(value, pattern) for value, pattern, fragment in prefiltered if fragment is None or fragment in folded]
        if not live:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for term, pattern in live:
                if pattern.search(line):
                    print(f"GATE {relative}:{line_number}: {term}")
                    hits += 1
    return hits


def write_bundle_tree(root, entries):
    for relative, content in entries:
        destination = root / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def write_bundle_output(output, entries):
    if output.suffix.lower() == ".zip":
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
            root = Path(temporary)
            write_bundle_tree(root, entries)
            archive = Path(shutil.make_archive(str(output.with_suffix("")), "zip", root_dir=root))
            if str(archive) != str(output):
                archive.replace(output)
    else:
        output.mkdir(parents=True, exist_ok=True)
        write_bundle_tree(output, entries)


def tracked_machine_names(repo):
    if not (repo / ".git").exists():
        return machine_names(repo)
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "--name-only", "HEAD", "machines/"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except OSError as exc:
        raise BundleRefusal(f"{repo}: cannot list tracked machine files: {exc}") from exc
    if result.returncode:
        try:
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
        except OSError as exc:
            raise BundleRefusal(f"{repo}: cannot list tracked machine files: {exc}") from exc
        if head.returncode:
            raise BundleRefusal(f"{repo} has no commit to read machine ownership from")
        raise BundleRefusal(f"{repo}: cannot list tracked machine files: {result.stderr.strip()}")
    return sorted(Path(relative).stem for relative in result.stdout.splitlines() if relative.endswith(".json") and not relative.endswith(".local.json"))


def validate_machine_owns(machine, relative):
    owns = machine.get("owns", [])
    if not isinstance(owns, list) or any(not isinstance(domain, str) or not domain.strip() for domain in owns):
        raise ValueError(f"{relative}: owns must be a list of non-empty strings")
    return owns


def committed_machine_texts(repo, names):
    names = list(names)
    if not names:
        return {}
    requested = ", ".join(f"machines/{name}.json" for name in names)
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            input=("\n".join(f"HEAD:machines/{name}.json" for name in names) + "\n").encode("utf-8"),
            capture_output=True, check=False,
        )
    except OSError as exc:
        raise BundleRefusal(f"{repo}: cannot read committed {requested}: {exc}") from exc
    if result.returncode:
        raise BundleRefusal(f"{repo}: cannot read committed {requested}: {result.stderr.decode('utf-8', errors='replace').strip()}")
    texts = {}
    offset = 0
    for name in names:
        try:
            end = result.stdout.index(b"\n", offset)
            header = result.stdout[offset:end]
            offset = end + 1
            if header.endswith(b" missing"):
                texts[name] = None
                continue
            sha, kind, size = header.split()
            size = int(size)
            end = offset + size
            if result.stdout[end:end + 1] != b"\n":
                raise ValueError("invalid batch content size")
            texts[name] = result.stdout[offset:end].decode("utf-8", errors="replace").removeprefix("\ufeff")
            offset = end + 1
        except ValueError as exc:
            raise BundleRefusal(f"{repo}: cannot read committed machines/{name}.json: {exc}") from exc
    return texts


def machine_owns(repo, name, texts=None, machine=None):
    relative = f"machines/{name}.json"
    committed = (repo / ".git").exists()
    if committed and texts is None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "show", f"HEAD:{relative}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
        except OSError as exc:
            raise BundleRefusal(f"{repo}: cannot read committed {relative}: {exc}") from exc
        if result.returncode:
            raise BundleRefusal(f"{repo}: cannot read committed {relative}: {result.stderr.strip()}")
        text = result.stdout
    elif committed:
        text = texts.get(name)
        if text is None:
            raise BundleRefusal(f"{repo}: cannot read committed {relative}: HEAD:{relative} missing")
    try:
        if committed:
            machine = json.loads(text.removeprefix("\ufeff"))
        elif machine is None:
            machine = read_json(repo / relative)
        if not isinstance(machine, dict):
            raise TypeError(f"got {type(machine).__name__}")
    except (OSError, ValueError, TypeError) as exc:
        raise BundleRefusal(f"{relative}: is not a JSON object ({exc})") from exc
    try:
        return validate_machine_owns(machine, relative)
    except ValueError as exc:
        raise BundleRefusal(str(exc)) from exc


def build_bundle(options):
    name = options["bundle"]
    try:
        available = tracked_machine_names(options["repo"])
        if name not in available:
            raise BundleRefusal(f"machines/{name}.json is not a tracked machine; available machines: {', '.join(available)}")
        try:
            machine = read_json(options["repo"] / "machines" / f"{name}.json")
            if not isinstance(machine, dict):
                raise TypeError(f"got {type(machine).__name__}")
        except (OSError, ValueError, TypeError) as exc:
            raise BundleRefusal(f"machines/{name}.json: is not a JSON object ({exc})") from exc
        texts = committed_machine_texts(options["repo"], available) if (options["repo"] / ".git").exists() else None
        owns = machine_owns(options["repo"], name, texts=texts, machine=machine)
        owned = {domain.casefold() for domain in owns}
        required = {}
        owners = {}
        for other in available:
            declarations = owns if other == name else machine_owns(options["repo"], other, texts=texts)
            for domain in declarations:
                owners.setdefault(domain.casefold(), set()).add(other)
                if domain.casefold() not in owned:
                    required.setdefault(domain.casefold(), (domain, set()))[1].add(other)
        global_terms, domains = load_bundle_terms(options["home"])
        for domain, terms in domains.items():
            declared = owners.get(domain, set())
            if not declared:
                raise BundleRefusal(f"domain list {terms.path} is owned by no machine; declare it in one machines/<name>.json owns list")
        if (options["repo"] / ".git").exists():
            relative = f"machines/{name}.json"
            try:
                status = subprocess.run(
                    ["git", "-C", str(options["repo"]), "status", "--porcelain", "--", relative],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                )
            except OSError as exc:
                raise BundleRefusal(f"{options['repo']}: cannot check {relative}: {exc}") from exc
            if status.returncode:
                raise BundleRefusal(f"{options['repo']}: cannot check {relative}: {status.stderr.strip()}")
            if status.stdout.strip():
                raise BundleRefusal(f"{relative} has uncommitted changes; commit them so the shipped file matches the ownership the gate applied")
        local_relative = f"machines/{name}.local.json"
        try:
            local_machine = read_local_machine(options["repo"] / local_relative, label=local_relative)
        except ValueError as exc:
            raise BundleRefusal(str(exc)) from exc
    except BundleRefusal as exc:
        print(f"bundle refused: {exc}")
        return 2
    if "owns" in local_machine:
        print(f"bundle gate: ignoring owns in machines/{name}.local.json (ownership is read from the committed machines/{name}.json)")
    for domain in owns:
        if domain.casefold() not in domains:
            path = bundle_terms_directory(options["home"]) / f"{domain}.txt"
            print(f"bundle refused: machines/{name}.json owns {domain} but {path} does not exist")
            return 2
    try:
        require_bundle_domains(options["home"], domains, required)
    except BundleRefusal as exc:
        print(f"bundle refused: {exc}")
        return 2
    excluded = {domain: terms for domain, terms in domains.items() if domain not in owned}
    if not excluded:
        print(f"bundle refused: no domain list applies to {name} (every list on the builder is owned by it); a client bundle must exclude at least one other domain")
        return 2
    global_terms = dedupe_terms(global_terms)
    domain_terms = dedupe_terms([term for terms in excluded.values() for term in terms], excluded=global_terms)
    gate_terms = global_terms + domain_terms
    domain_count = len(domain_terms)
    print(
        f"bundle gate: {len(global_terms)} global terms, {domain_count} terms from {len(excluded)} domain lists, "
        f"owned: {', '.join(owns) or 'none'}"
    )
    date = datetime.date.today().isoformat()
    output = options["out"] or options["repo"] / "bundles" / f"{name}-{date}"
    is_zip = output.suffix.lower() == ".zip"
    if output.exists():
        if is_zip or not output.is_dir() or any(output.iterdir()):
            print(f"bundle refused: bundle output must not exist or must be empty: {output}")
            return 2
    fingerprints = ", ".join(sorted(terms.fingerprint for terms in excluded.values()))
    gate_record = (
        f"Bundle gate applied: {len(global_terms)} global terms, {len(excluded)} domain lists with {domain_count} terms, "
        f"fingerprints {fingerprints} (owned: {', '.join(owns) or 'none'})"
    )
    try:
        machine = deep_merge(machine, local_machine, replace_whole=frozenset({"statusline_weather"}))
        flags = validate_machine_flags(
            machine, {"harvest": True, "plan_tools": True, "publish": False, "codex_first": True, "statusline": True},
            lambda key: local_relative if key in local_machine else f"machines/{name}.json",
        )
        validate_statusline_weather(machine, lambda key: local_relative if key in local_machine else f"machines/{name}.json")
        validate_proxy(machine, lambda key: local_relative if key in local_machine else f"machines/{name}.json")
        validate_remote(machine, lambda key: local_relative if key in local_machine else f"machines/{name}.json")
        validate_gh_accounts(machine, lambda key: local_relative if key in local_machine else f"machines/{name}.json")
        flags.pop("codex_first")
        flags.pop("statusline")
        entries = bundle_file_entries(options["repo"], name, date, gate_record, machine=machine, **flags)
    except ValueError as exc:
        print(f"bundle refused: {exc}")
        return 2
    hits = bundle_gate(entries, gate_terms)
    if hits:
        print(f"bundle refused: {hits} hits")
        return 2
    print(gate_record)
    if options["dry_run"]:
        print(f"bundle would write {output} ({len(entries)} files)")
        return 0

    write_bundle_output(output, entries)
    print(f"bundle written {output} ({len(entries)} files)")
    return 0


def addon_install_text(name, date, commit, gate_record):
    addon = ADDONS[name]
    steps = list(addon["steps"])
    steps[0] += " On every later update, run `python install.py --unpack <zip>` from the install folder, which also works on a clone of the public repository, writes the shipped files, removes files the previous addon shipped and this one does not, and keeps everything else (local machine files are backed up under `.scratch/` before replacing)."
    steps[0] += " If the install folder's install.py does not know `--unpack` yet, unzip this addon by hand this once; a harness bundle brings the newer install.py."
    return (
        "# {} addon\n\n".format(name.title())
        + "Built {} from {}. {}\n\n".format(date, commit, addon["description"])
        + "Files:\n{}\n\n".format("\n".join([*addon["paths"], "MANIFEST-addon-{}.txt".format(name)]))
        + gate_record + "\n\n"
        + "Install, on a machine that already runs the harness (a bundle install or a clone of the public repository):\n\n"
        + "\n".join(steps) + "\n\n"
        + "Update: run `python install.py --unpack <zip>` from the install folder and run install.py again. Remove: delete the listed files from the install folder and `~/.claude/skills/{}/`.\n".format(name)
    )


def build_addon(options):
    name = options["addon"]
    if name not in ADDONS:
        print("addon refused: unknown addon {}; available addons: {}".format(name, ", ".join(sorted(ADDONS))))
        return 2

    def report(global_count, domain_count, list_count):
        print("addon gate: {} global terms, {} terms from {} domain lists".format(global_count, domain_count, list_count))

    try:
        details = {}
        gate_terms = all_bundle_terms(options["home"], options["repo"], report, details=details)
        domains = details["domains"]
        fingerprints = ", ".join(sorted(terms.fingerprint for terms in domains.values()))
        gate_record = "Addon gate applied: {} global terms, {} domain lists with {} terms, fingerprints {}".format(
            details["global_count"], len(domains), details["domain_count"], fingerprints,
        )
        date = datetime.date.today().isoformat()
        entries = {}
        add, add_tree = bundle_entry_adders(entries)
        for relative in ADDONS[name]["paths"]:
            source = options["repo"] / relative
            if not source.exists():
                raise BundleRefusal("{} is missing".format(relative))
            if relative.endswith("/"):
                before = len(entries)
                add_tree(source, relative)
                if len(entries) == before:
                    raise BundleRefusal("{} holds no files".format(relative))
            else:
                add(source, relative)
        entries["ADDON.md"] = addon_install_text(name, date, bundle_commit(options["repo"]), gate_record).encode("utf-8")
        manifest = "MANIFEST-addon-{}.txt".format(name)
        entries[manifest] = ("\n".join(sorted([*entries, manifest])) + "\n").encode("utf-8")
        entries = unpack_build_entries(entries, options["repo"])
    except (OSError, ValueError) as exc:
        print("addon refused: {}".format(exc))
        return 2
    hits = bundle_gate(entries, gate_terms)
    if hits:
        print("addon refused: {} hits".format(hits))
        return 2
    output = options["out"] or options["repo"] / "bundles" / "harness-addon-{}-{}.zip".format(name, date)
    if output.exists():
        print("addon refused: addon output must not exist: {}".format(output))
        return 2
    if options["dry_run"]:
        print("addon would write {} ({} files)".format(output, len(entries)))
        return 0
    write_bundle_output(output, entries)
    print("addon written {} ({} files)".format(output, len(entries)))
    return 0


def resolve_machine(options):
    name_file = options["home"] / ".claude" / "local" / "machine-name"
    name = options["machine"]
    if not name and name_file.is_file():
        name = name_file.read_text(encoding="utf-8").strip()
    if not name:
        raise ValueError("--machine is required the first time")
    available = machine_names(options["repo"])
    if name not in available:
        raise LookupError(
            f"unknown machine {name!r}; available machines: {', '.join(available)}"
        )
    return name, name_file


def machine_file_path(raw, home):
    expanded = os.path.expanduser(str(raw))
    path = Path(expanded)
    custom_home = Path(home).resolve() != Path.home().resolve()
    return (path if path.is_absolute() else home / path), not (
        path.is_absolute() and custom_home
    )


def git_hook_action(path, status):
    print(f"ACTION {path} ({status})")


def write_git_hook(path, content, dry_run):
    path = Path(path)
    if path.is_file() and path.read_bytes() == content:
        git_hook_action(path, "unchanged")
        return
    if dry_run:
        git_hook_action(path, "would write")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = path.with_name(f"{path.name}.backup-{stamp}")
        shutil.copy2(path, backup)
    path.write_bytes(content)
    if os.name != "nt":
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
    git_hook_action(path, "written")


def install_git_hooks(options):
    repository = Path(options["git_hooks"]).expanduser().resolve()
    git_directory = repository / ".git"
    if not git_directory.is_dir():
        raise ValueError(f"git hooks repo does not contain .git: {repository}")

    interpreter = str(Path(sys.executable).resolve()).replace("\\", "/")
    guard = str(
        options["home"] / ".claude" / "hooks" / "commit_msg_guard.py"
    ).replace("\\", "/")
    commit_message = (
        f'#!/bin/sh\nexec "{interpreter}" "{guard}" "$1"\n'.encode("utf-8")
    )
    hooks = git_directory / "hooks"
    write_git_hook(hooks / "commit-msg", commit_message, options["dry_run"])

    configured_gitleaks = options["gitleaks"]
    gitleaks = (
        str(Path(configured_gitleaks).expanduser().resolve())
        if configured_gitleaks is not None
        else shutil.which("gitleaks")
    )
    pre_commit = hooks / "pre-commit"
    if gitleaks is None:
        git_hook_action(
            pre_commit,
            "skipped: gitleaks not on PATH; install it and re-run --git-hooks",
        )
        return 0
    completed = subprocess.run(
        [gitleaks, "version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=2,
        check=False,
    )
    version = re.search(r"(\d+)\.(\d+)", completed.stdout + completed.stderr)
    if completed.returncode != 0 or version is None:
        raise ValueError("could not determine gitleaks major.minor version")
    current = (int(version.group(1)), int(version.group(2)))
    arguments = (
        "git --pre-commit --staged --redact"
        if current >= (8, 19)
        else "protect --staged --redact"
    )
    executable = str(Path(gitleaks).resolve()).replace("\\", "/")
    content = f'#!/bin/sh\nexec "{executable}" {arguments}\n'.encode("utf-8")
    write_git_hook(pre_commit, content, options["dry_run"])
    return 0


def install(options):
    if options["addon"] is not None:
        return build_addon(options)
    if options["bundle"] is not None:
        return build_bundle(options)
    if options["git_hooks"] is not None:
        return install_git_hooks(options)
    name, name_file = resolve_machine(options)
    installer = Installer(options["home"], options["repo"], options["dry_run"])
    install_marker = options["repo"] / "INSTALL.md"
    git_marker = options["repo"] / ".git"
    local_dir = options["home"] / ".claude" / "local"
    local_markdown_destination = local_dir / "machine.local.md"
    try:
        tracked_path = options["repo"] / "machines" / f"{name}.json"
        tracked_flags = validate_machine_flags(read_json(tracked_path), {"publish": False}, lambda key: tracked_path)
        sources = {}
        machine = load_machine(options["repo"], name, home=options["home"], sources=sources)
        validate_machine_flags(machine, {"codex_first": True, "publish": False, "statusline": True}, sources.__getitem__)
        validate_machine_efforts(machine, sources.__getitem__)
        validate_gh_accounts(machine, sources.__getitem__)
        machine.setdefault("codex_effort", DEFAULT_CODEX_EFFORT)
        machine.setdefault("claude_effort", DEFAULT_CLAUDE_EFFORT.copy())
        validate_statusline_weather(machine, sources.__getitem__)
        validate_proxy(machine, sources.__getitem__)
        validate_remote(machine, sources.__getitem__)
        key = proxy_key(machine, options["home"], options["dry_run"])
        proxy_conflicts = proxy_launcher_conflicts(machine, options["home"])
        if proxy_conflicts and not options["dry_run"]:
            raise ValueError(f"{proxy_conflicts[0]} exists and was not written by install.py (set proxy.bin_dir to another folder or move that file)")
        check_remote_skills(machine, options["home"], options["repo"])
        if local_markdown_destination.exists() and not local_markdown_destination.is_file():
            raise ValueError(f"{local_markdown_destination}: must be a file")
    except (OSError, ValueError) as exc:
        print(f"install refused: {exc}")
        return 2
    if install_marker.exists() and git_marker.exists() and not tracked_flags["publish"]:
        raise ValueError(
            "this install folder holds both INSTALL.md and .git: a bundle was "
            "unpacked over a clone. Delete the folder and unpack the bundle again."
        )
    installer.write_bytes(name_file, (name + "\n").encode("utf-8"))
    mode = "repository" if git_marker.exists() and not install_marker.exists() else "bundle"
    installer.write_bytes(
        name_file.parent / "harness-mode", (mode + "\n").encode("utf-8")
    )

    installer.write_bytes(local_dir / "machine.json", json_bytes(machine))
    installer.copy_file(
        options["repo"] / "machines" / f"{name}.md",
        local_dir / "machine.md",
    )
    local_markdown_path = options["repo"] / "machines" / f"{name}.local.md"
    if local_markdown_path.is_file():
        install_local_markdown(installer, local_markdown_path, local_markdown_destination)
    elif local_markdown_destination.is_file() and not local_markdown_destination.read_text(encoding="utf-8-sig").startswith("# No local machine facts on this machine yet"):
        installer.action(local_markdown_destination, "unchanged")
    else:
        stub = (
            "# No local machine facts on this machine yet "
            f"(machines/{name}.local.md was not in the install folder; this file is yours, no install overwrites it).\n"
            "\n"
            "## Who I am, for calibration\n"
            "Not written yet. In Claude Code run /setup: it asks who you are, your git identities and gh account, your Codex model and the rules of your engagement, then writes this file and ~/.claude/local/machine.local.json.\n"
        )
        installer.write_bytes(local_dir / "machine.local.md", stub.encode("utf-8"))

    installer.copy_file(
        options["repo"] / "claude" / "CLAUDE.md",
        options["home"] / ".claude" / "CLAUDE.md",
    )
    install_keybindings(installer, options["repo"], options["home"])
    statusline_installed = install_statusline(installer, options["repo"], options["home"], machine)
    install_proxy(installer, machine, statusline_installed, key, proxy_conflicts)
    for source in sorted((options["repo"] / "claude" / "hooks").glob("*.py")):
        installer.copy_file(source, options["home"] / ".claude" / "hooks" / source.name)
    skills = options["repo"] / "claude" / "skills"
    if skills.is_dir():
        for source in sorted(path for path in skills.iterdir() if path.is_dir()):
            installer.sync_directory(
                source, options["home"] / ".claude" / "skills" / source.name
            )
    install_gh_credentials(installer, machine)
    install_remote_skills(installer, machine, options["repo"])
    agents = options["repo"] / "claude" / "agents"
    if agents.is_dir():
        for source in sorted(agents.glob("*.md")):
            installer.copy_file(
                source, options["home"] / ".claude" / "agents" / source.name
            )
    templates = options["repo"] / "templates"
    if templates.is_dir():
        for source in sorted(path for path in templates.rglob("*") if path.is_file()):
            installer.copy_file(
                source,
                options["home"] / ".claude" / "templates" / source.relative_to(templates),
            )
    installer.copy_file(
        options["repo"] / "codex" / "AGENTS.md",
        options["home"] / ".codex" / "AGENTS.md",
    )

    settings_path = options["home"] / ".claude" / "settings.json"
    existing_settings = read_json(settings_path) if settings_path.is_file() else {}
    merged_settings = merge_settings(existing_settings, machine, options["home"], statusline_available=statusline_installed, installer=installer)
    installer.write_bytes(settings_path, json_bytes(merged_settings))
    install_codex_effort(installer, machine)

    for rule in machine.get("remove_allow_rules") or []:
        path, allowed = machine_file_path(rule.get("file", ""), options["home"])
        if not allowed:
            installer.action(path, "skipped (absolute path under a custom home)")
            continue
        if not path.is_file():
            installer.action(path, "skipped")
            continue
        value = read_json(path)
        permissions = value.get("permissions") if isinstance(value, dict) else None
        allowed = permissions.get("allow") if isinstance(permissions, dict) else None
        if not isinstance(allowed, list):
            installer.action(path, "unchanged")
            continue
        pattern = re.compile(str(rule.get("match", "")))
        removed = [item for item in allowed if isinstance(item, str) and pattern.search(item)]
        if not removed:
            installer.action(path, "unchanged")
            continue
        for item in removed:
            preview = item if len(item) <= 60 else item[:60] + "..."
            installer.action(path, "would remove" if options["dry_run"] else "removed", preview)
        permissions["allow"] = [item for item in allowed if item not in removed]
        installer.write_bytes(path, json_bytes(value))
        if path.resolve() == settings_path.resolve():
            merged_settings = value

    for move in machine.get("project_settings_moves") or []:
        source_path, allowed = machine_file_path(move.get("from", ""), options["home"])
        if not allowed:
            installer.action(
                source_path, "skipped (absolute path under a custom home)"
            )
            continue
        if not source_path.is_file():
            installer.action(source_path, "skipped")
            continue
        source_value = read_json(source_path)
        additions, found = dotted_get(source_value, str(move.get("key", "")))
        if not found:
            installer.action(source_path, "skipped")
            continue
        if not isinstance(additions, list):
            raise ValueError(f"project settings move is not a list: {source_path}")
        user_value = merged_settings
        dotted_append_defaults(user_value, str(move.get("to", "")), additions)
        dotted_remove(source_value, str(move.get("key", "")))
        installer.write_bytes(settings_path, json_bytes(user_value))
        installer.write_bytes(source_path, json_bytes(source_value))
        merged_settings = user_value

    manifest = read_json(options["repo"] / "install-manifest.json")
    for relative in manifest.get("delete") or []:
        installer.remove(options["home"] / Path(relative))
    for relative in machine.get("delete") or []:
        installer.remove(options["home"] / Path(relative))

    if options["dry_run"]:
        print("install complete")
        return 0
    tests_failed = False
    if not options["no_tests"]:
        environment = os.environ.copy()
        environment.pop("CLAUDE_HOOKS_HOME", None)
        environment["HARNESS_REPO"] = str(options["repo"])
        completed = subprocess.run(
            [sys.executable, str(options["home"] / ".claude" / "hooks" / "hooks_test.py")],
            cwd=options["repo"],
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            tests_failed = True
    if installer.failed:
        print("install finished with removal failures")
        return 3
    if tests_failed:
        print("install finished with hook test failures")
        return 2
    print("install complete")
    return 0


def unpack_build_entries(entries, repo):
    result = sorted(entries.items())
    for relative, content in result:
        try:
            unpack_relative(relative, repo.resolve())
        except BundleRefusal as exc:
            raise BundleRefusal(f"{relative}: name would be refused by --unpack") from exc
    return result


def unpack_relative(name, target):
    from tools.project_bundle import check_ancestors, safe_relative

    try:
        relative = safe_relative(name.replace("\\", "/"))
        destination = target / relative
        check_ancestors(destination, root=target)
    except ValueError as exc:
        raise BundleRefusal(f"{name}: path outside the install folder") from exc
    if not destination.resolve().is_relative_to(target):
        raise BundleRefusal(f"{name}: path outside the install folder")
    return relative.as_posix()


def unpack_manifest_name(name):
    return re.fullmatch(r"MANIFEST(?:-addon-[^/]+)?[.]txt", name.replace("\\", "/"), re.IGNORECASE)


def unpack_manifest(content, target, *, source=None, dry_run=False):
    paths = {}
    for line_number, line in enumerate(content.splitlines(), 1):
        try:
            relative = unpack_relative(line, target)
        except BundleRefusal:
            if source is None:
                raise
            print(("would " if dry_run else "") + f"note: {source}: ignoring line {line_number}")
            continue
        key = relative.casefold()
        if key in paths and source is None:
            raise BundleRefusal(f"{relative}: duplicate path in the manifest")
        paths[key] = relative
    return paths


def unpack_user_file(relative):
    normalized = relative.casefold()
    parts = normalized.split("/")
    filename = parts[-1]
    return (
        parts[0] in {".scratch", "bundles", "inbox", "transcripts", "data", "daily", ".claude"}
        or (parts[0] == "evals" and normalized != "evals/readme.md")
        or "__pycache__" in parts[:-1]
        or (len(parts) == 2 and parts[0] == "machines" and filename.endswith(".local.md"))
        or filename.endswith((".local.json", ".pyc"))
        or (filename.startswith("handoff-") and filename.endswith(".md"))
        or filename in {"pr-review-findings.json", "auto-trail.tsv"}
    )


def unpack_write_file(destination, content, mode):
    existing_mode = stat.S_IMODE(destination.stat().st_mode) if os.name != "nt" and destination.exists() else None
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + str(os.getpid()) + ".tmp")
    try:
        temporary.write_bytes(content)
        if os.name != "nt":
            if existing_mode is not None:
                temporary.chmod(existing_mode)
            elif mode & 0o111:
                temporary.chmod(stat.S_IMODE(temporary.stat().st_mode) | 0o111)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def unpack_release_files(installer, entries, previous, manifest, archive_path, next_command, action):
    target = installer.repo
    dry_run = installer.dry_run
    owners = {}
    for path in sorted(target.iterdir()):
        filename = path.name.casefold()
        if unpack_manifest_name(path.name) and filename != manifest.casefold() and path.is_file():
            owned = unpack_manifest(path.read_text(encoding="utf-8"), target, source=path, dry_run=dry_run)
            for key in owned:
                owners.setdefault(key, path.name)
            action(path, "read other manifest")
    known = set(entries) | set(previous) | set(owners)
    others = 0
    excluded = {".git", ".scratch", "bundles", "inbox", "transcripts", "data", "daily", "__pycache__"}
    for directory, dirnames, filenames in os.walk(target):
        dirnames[:] = [
            name for name in dirnames if name.casefold() not in {".git", "__pycache__"}
            and (Path(directory) / name).relative_to(target).parts[0].casefold() not in excluded
        ]
        for filename in filenames:
            path = Path(directory) / filename
            if path.is_file() and path.relative_to(target).as_posix().casefold() not in known:
                others += 1
    removals = []
    kept = []
    for key in sorted(set(previous) - set(entries)):
        relative = previous[key]
        destination = target / relative
        if key in owners:
            kept.append((destination, f"kept (owned by {owners[key]})"))
        elif unpack_user_file(relative):
            kept.append((destination, "kept (user file)"))
        elif destination.exists():
            if not destination.is_file():
                raise BundleRefusal(f"{relative}: previous manifest path is not a file")
            removals.append(destination)
    backups = []
    unchanged = set()
    for key, (relative, content, mode) in entries.items():
        destination = target / relative
        if destination.exists() and not destination.is_file():
            raise BundleRefusal(f"{relative}: destination is not a file")
        for parent in destination.parents:
            if parent == target:
                break
            if parent.is_file() or parent.relative_to(target).as_posix().casefold() in entries:
                raise BundleRefusal(f"{relative}: parent is not a directory")
        if destination.is_file():
            if destination.read_bytes() == content:
                unchanged.add(key)
            elif unpack_user_file(relative):
                backups.append(relative)
    stamp = datetime.datetime.now()
    backup_root = target / ".scratch" / ("unpack-backup-" + stamp.strftime("%Y%m%d-%H%M%S"))
    while backup_root.exists():
        stamp += datetime.timedelta(seconds=1)
        backup_root = target / ".scratch" / ("unpack-backup-" + stamp.strftime("%Y%m%d-%H%M%S"))
    for relative in backups:
        unpack_relative((backup_root.relative_to(target) / relative).as_posix(), target)
        if (target / ".scratch").is_file():
            raise BundleRefusal(".scratch: backup parent is not a directory")
    def write_entry(key):
        relative, content, mode = entries[key]
        destination = target / relative
        if key in unchanged:
            action(destination, "unchanged")
            return
        if relative in backups:
            backup = backup_root / relative
            action(destination, "backed up", str(backup))
            if not dry_run:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)
        if not dry_run:
            unpack_write_file(destination, content, mode)
        action(destination, "written")

    for key in sorted(set(entries) - {manifest.casefold()}):
        write_entry(key)
    for destination, status in kept:
        action(destination, status)
    for destination in removals:
        action(destination, "removed")
        if not dry_run:
            destination.unlink()
    parents = {parent for path in removals for parent in path.parents if parent != target and parent.is_relative_to(target)}
    removed_paths = set(removals)
    for parent in sorted(parents, key=lambda path: (-len(path.parts), str(path))):
        if any((target / entry[0]).is_relative_to(parent) for entry in entries.values()):
            continue
        if parent.is_dir() and all(child in removed_paths for child in parent.iterdir()):
            action(parent, "removed empty directory")
            if not dry_run:
                parent.rmdir()
            removed_paths.add(parent)
    print(("would keep" if dry_run else "kept") + f" {others} other files outside the user folders")
    write_entry(manifest.casefold())
    print(("would unpack" if dry_run else "unpacked") + f" {archive_path} into {target}: {len(entries) - len(unchanged)} written, {len(unchanged)} unchanged, {len(removals)} removed, {len(backups)} backed up; next: {next_command}")
    return 0


def unpack_bundle(options):
    import zipfile

    archive_path = options["unpack"]
    target = options["repo"]
    dry_run = options["dry_run"]
    installer = Installer(target, target, dry_run)

    def action(path, status, detail=None):
        if dry_run:
            if status.startswith("kept (owned by "):
                status = "keep" + status[4:]
            status = "would " + {
                "validated": "validate", "checked": "check", "written": "write",
                "removed": "remove", "removed empty directory": "remove empty directory",
                "backed up": "back up", "kept (user file)": "keep (user file)",
                "unchanged": "leave unchanged", "read other manifest": "read other manifest",
            }.get(status, status)
        installer.action(path, status, detail)

    try:
        with zipfile.ZipFile(archive_path) as archive:
            manifests = [
                info for info in archive.infolist()
                if not info.is_dir() and unpack_manifest_name(info.filename)
            ]
            if len(manifests) != 1:
                raise BundleRefusal(f"{archive_path} carries no manifest; it predates the unpack step, unzip it by hand once and later updates can use --unpack")
            manifest = unpack_relative(manifests[0].filename, target)
            current = unpack_manifest(archive.read(manifests[0]).decode("utf-8"), target)
            entries = {}
            for info in archive.infolist():
                directory = info.is_dir() or info.filename.endswith("\\")
                relative = unpack_relative(info.filename[:-1] if directory else info.filename, target)
                key = relative.casefold()
                if directory:
                    if not any(path.startswith(key + "/") for path in current):
                        raise BundleRefusal(f"{info.filename}: not in the manifest")
                    continue
                if key not in current:
                    raise BundleRefusal(f"{info.filename}: not in the manifest")
                if key in entries:
                    raise BundleRefusal(f"{info.filename}: duplicate path in the zip")
                entries[key] = (relative, archive.read(info), info.external_attr >> 16)
            if set(entries) != set(current):
                missing = sorted(set(current) - set(entries))[0]
                raise BundleRefusal(f"{current[missing]}: not in the zip")
        action(archive_path, "validated")
        bundle = manifest.casefold() == "manifest.txt"
        if bundle and (target / ".git").exists():
            raise BundleRefusal(f"{target} is a git clone; update it with git pull")
        if not (target / "install.py").is_file():
            raise BundleRefusal(f"{target} holds no install.py; a first install unzips the bundle by hand")
        if bundle and not (target / "INSTALL.md").is_file():
            raise BundleRefusal(f"{target} holds no INSTALL.md; a first install unzips the bundle by hand")
        next_command = "python install.py"
        if bundle:
            instructions = entries.get("install.md")
            heading = re.match(r"# Harness bundle for ([^,\r\n]+), built ", instructions[1].decode("utf-8")) if instructions else None
            if heading is None:
                raise BundleRefusal("INSTALL.md: missing bundle heading")
            next_command += " --machine " + heading.group(1)
        action(target, "checked")
        previous_path = target / manifest
        if previous_path.is_file():
            previous = unpack_manifest(previous_path.read_text(encoding="utf-8"), target, source=previous_path, dry_run=dry_run)
            action(previous_path, "read previous manifest")
        else:
            previous = {}
            print(("would " if dry_run else "") + f"note: no previous manifest in {target}; files the previous release shipped and this one does not cannot be detected and stay in place")
        return unpack_release_files(installer, entries, previous, manifest, archive_path, next_command, action)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, BundleRefusal):
            raise
        raise BundleRefusal(f"{archive_path}: {exc}") from exc


def main(arguments=None):
    try:
        options = parse_args(sys.argv[1:] if arguments is None else arguments)
        if options["unpack"] is not None:
            return unpack_bundle(options)
        return install(options)
    except BundleRefusal as exc:
        print(f"unpack refused: {exc}")
        return 2
    except (ValueError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
