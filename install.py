import copy
import datetime
import hashlib
import json
import os
import re
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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claude.hooks._common import nudge_disabled


USAGE = (
    "usage: python install.py [--machine NAME] [--home PATH] [--repo PATH] "
    "[--dry-run] [--no-tests] [--git-hooks REPO] [--gitleaks PATH] "
    "[--bundle NAME] [--out PATH]"
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
    if options["bundle"] is not None and options["git_hooks"] is not None:
        raise ValueError("--bundle cannot be combined with --git-hooks")
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


def all_bundle_terms(home, repo=None, report=None):
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
    return global_terms + domain_terms


def bundle_install_text(name, date, commit, gate_record, *, harvest=True, publish=False):
    heading = f"# Harness bundle for {name}, built {date} from {commit}\n\n{gate_record}\n\n"
    first = f"1. Unpack this folder as the harness install folder named in machines/{name}.md. Replace the previous folder entirely; keep nothing from it."
    restart = "4. Restart Claude Code and run /setup; it asks for what this machine still needs (who you are, git identities, gh account, Codex model, engagement rules) and writes the local files."
    if publish:
        heading = f"# Harness bundle for {name}, built {date}\n\n"
        first = f"1. Clone the repository as the harness install folder named in machines/{name}.md, or unpack this folder there. Update it later with git pull; a release replaces the shared files and never touches your local files."
        restart = "4. Restart Claude Code and run /setup; it asks who you are, your git identities and gh account, and whether you use Codex CLI, and writes the local files."
    elif harvest:
        restart += " Memories travel back with `python harvest.py --export <folder>`."
    return (
        f"{heading}"
        f"{first}\n"
        f"2. Dry run and read every line: `python install.py --machine {name} --dry-run` (use the interpreter the machine file names; where the permission classifier blocks config edits, run the real install with the ! prefix).\n"
        f"3. Real run: `python install.py --machine {name}`. The hook tests run last and all must pass.\n"
        f"{restart}\n"
    )


def bundle_file_entries(repo, name, date, gate_record, *, harvest=True, plan_tools=True, publish=False):
    entries = {}

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

    add(repo / "install.py", "install.py")
    add(repo / "install-manifest.json", "install-manifest.json")
    add(repo / "claude" / "CLAUDE.md", "claude/CLAUDE.md")
    add(repo / "claude" / "keybindings.json", "claude/keybindings.json")
    add(repo / "claude" / "statusline-command.sh", "claude/statusline-command.sh")
    for source in sorted((repo / "claude" / "hooks").glob("*.py")):
        add(source, Path("claude/hooks") / source.name)
    add_tree(repo / "claude" / "skills", "claude/skills", excluded=set() if harvest else {"harvest"})
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
    entries["INSTALL.md"] = bundle_install_text(name, date, commit, gate_record, harvest=harvest, publish=publish).encode("utf-8")
    return sorted(entries.items())


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
        flags.pop("codex_first")
        flags.pop("statusline")
        entries = bundle_file_entries(options["repo"], name, date, gate_record, **flags)
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

    if is_zip:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
            root = Path(temporary)
            write_bundle_tree(root, entries)
            archive_base = str(output)[:-4]
            shutil.make_archive(archive_base, "zip", root_dir=root)
    else:
        output.mkdir(parents=True, exist_ok=True)
        write_bundle_tree(output, entries)
    print(f"bundle written {output} ({len(entries)} files)")
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
        validate_statusline_weather(machine, sources.__getitem__)
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
    for source in sorted((options["repo"] / "claude" / "hooks").glob("*.py")):
        installer.copy_file(source, options["home"] / ".claude" / "hooks" / source.name)
    skills = options["repo"] / "claude" / "skills"
    if skills.is_dir():
        for source in sorted(path for path in skills.iterdir() if path.is_dir()):
            installer.sync_directory(
                source, options["home"] / ".claude" / "skills" / source.name
            )
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


def main(arguments=None):
    try:
        options = parse_args(sys.argv[1:] if arguments is None else arguments)
        return install(options)
    except (ValueError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
