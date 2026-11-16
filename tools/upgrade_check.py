"""Replay a bundle upgrade in an isolated install folder and home."""

import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path, PureWindowsPath

if __package__:
    from .project_bundle import check_ancestors, safe_relative
else:
    from project_bundle import check_ancestors, safe_relative


REPO = Path(__file__).resolve().parents[1]
HOOK = {"matcher": "Read", "hooks": [{"type": "command", "command": "upgrade-check-marker"}]}
PERMISSION = "Bash(upgrade-check-marker)"


class UsageError(ValueError):
    pass


class CannotRun(ValueError):
    def __init__(self, reason, command):
        super().__init__(reason)
        self.command = command


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def parse_args(argv):
    parser = Parser(description=__doc__)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--from", dest="baseline", type=Path, metavar="ZIP")
    parser.add_argument("--repo", type=Path, default=REPO, metavar="DIR")
    parser.add_argument("--work", type=Path, metavar="DIR")
    parser.add_argument("--with-tests", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", args.machine):
        parser.error("machine must be a filename stem")
    args.repo = args.repo.expanduser().resolve()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    args.work = (args.work or args.repo / ".scratch" / "upgrade-check" / f"{args.machine}-{stamp}").expanduser().resolve()
    return args


def newest_bundle(repo, machine):
    pattern = re.compile(r"harness-bundle-" + re.escape(machine) + r"-(\d{4}-\d{2}-\d{2})([^/]*)\.zip$")
    candidates = []
    for path in (repo / "bundles").glob("*.zip"):
        match = pattern.fullmatch(path.name)
        if match and path.is_file():
            try:
                date = datetime.date.fromisoformat(match[1])
            except ValueError:
                continue
            suffix = tuple((0, int(part)) if part.isdigit() else (1, part)
                           for part in re.findall(r"\d+|\D+", match[2]))
            candidates.append(((date, suffix), path))
    return max(candidates)[1] if candidates else None


def find_baseline(repo, machine, work, environment):
    baseline = newest_bundle(repo, machine)
    if baseline is None and (repo / ".git").exists():
        log = work / "baseline-lookup.log"
        command = ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"]
        if run(command, cwd=repo, env=environment, log=log) == 0:
            common = Path(log.read_text(encoding="utf-8").strip())
            if common.is_absolute():
                baseline = newest_bundle(common.parent, machine)
    return baseline


def scratch_environment(home):
    environment = os.environ.copy()
    environment.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0",
        "CLAUDE_HOOKS_HOME": str(home), "CODEX_HOME": str(home / ".codex"),
        "PLAYWRIGHT_BROWSERS_PATH": str(home / ".cache" / "browsers"),
    })
    for name in ("TMP", "TEMP", "TMPDIR"):
        environment[name] = str(home / ".tmp")
    for relative in (".tmp", ".config", ".cache", "AppData/Roaming", "AppData/Local"):
        (home / relative).mkdir(parents=True, exist_ok=True)
    return environment


def run(command, *, cwd, env, log):
    """Keep all subprocess execution here so tests can replace it."""
    result = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", check=False)
    log.write_text(result.stdout, encoding="utf-8")
    return result.returncode


def command_text(command):
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def tail(log):
    if log.is_file():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]:
            print("  " + line)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge(base, override):
    result = dict(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


def home_path(raw, home):
    text = str(raw).replace("\\", "/") if os.name == "nt" else str(raw)
    if text == "~" or text.startswith("~/"):
        path = home / text[2:] if text != "~" else home
    elif text.startswith("~") or (PureWindowsPath(text).drive and os.name != "nt"):
        return None
    else:
        path = home / Path(text)
    return path.resolve()


def confined_path(raw, home):
    path = home_path(raw, home)
    return path is not None and path.is_relative_to(home.resolve())


def check_machine_paths(machine, manifest, home, *, allow_rule_rewrite=False):
    paths = [("delete", value) for value in (machine.get("delete") or [])]
    paths.extend(("manifest delete", value) for value in (manifest.get("delete") or []))
    if not allow_rule_rewrite:
        for rule in machine.get("remove_allow_rules") or []:
            path = home_path(rule.get("file", ""), home)
            if path is None or not path.is_relative_to((home.parent / "outside").resolve()):
                paths.append(("remove_allow_rules.file", rule.get("file", "")))
    paths.extend(("project_settings_moves.from", move.get("from", "")) for move in (machine.get("project_settings_moves") or []))
    proxy = machine.get("proxy") or {}
    if proxy:
        paths.extend((f"proxy.{key}", proxy.get(key, default)) for key, default in (("bin_dir", ".claude/local/bin"), ("key_file", "")))
    for label, value in paths:
        if not confined_path(value, home):
            raise ValueError(f"refused side effect: {label} escapes the scratch home")


def check_tree_paths(repo, machine, home, *, allow_rule_rewrite=False):
    config = read_json(repo / "machines" / f"{machine}.json")
    for path in (repo / "machines" / f"{machine}.local.json", home / ".claude/local/machine.local.json"):
        if path.is_file():
            config = merge(config, read_json(path))
    check_machine_paths(config, read_json(repo / "install-manifest.json"), home, allow_rule_rewrite=allow_rule_rewrite)
    return config


def prepare_machine(install, machine, home, rule_copies):
    for path in (install / "machines" / f"{machine}.json", install / "machines" / f"{machine}.local.json",
                 home / ".claude/local/machine.local.json"):
        if not path.is_file():
            continue
        config = read_json(path)
        changed = False
        for rule in config.get("remove_allow_rules") or []:
            original = rule.get("file", "")
            resolved = home_path(original, home)
            if confined_path(original, home) or (resolved is not None and resolved in rule_copies.values()):
                continue
            if original not in rule_copies:
                name = safe_relative(PureWindowsPath(original).name)
                target = home.parent / "outside" / str(len(rule_copies)) / name
                check_ancestors(target, root=home.parent)
                write_json(target, {"permissions": {"allow": []}})
                rule_copies[original] = target
                print(f"INFO remove_allow_rules for {original} replayed on a scratch copy", flush=True)
            rule["file"] = str(rule_copies[original])
            changed = True
        if changed:
            write_json(path, config)
    config = check_tree_paths(install, machine, home)
    proxy = config.get("proxy")
    key_file = proxy.get("key_file") if isinstance(proxy, dict) else None
    if isinstance(key_file, str) and key_file.strip():
        key_path = home_path(key_file, home)
        if key_path is None or key_path == home.resolve() or not key_path.is_relative_to(home.resolve()):
            raise ValueError("refused side effect: proxy.key_file must be a file inside the scratch home")
        check_ancestors(key_path, root=home)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("upgrade-check-placeholder\n", encoding="utf-8")
        print("INFO planted a placeholder proxy key; the proxy itself is not exercised", flush=True)


def archive_entries(path, machine):
    with zipfile.ZipFile(path) as archive:
        entries = {}
        seen = set()
        for info in archive.infolist():
            name = info.filename.replace("\\", "/").rstrip("/") if info.is_dir() else info.filename.replace("\\", "/")
            try:
                relative = safe_relative(name)
            except ValueError as exc:
                raise ValueError("bundle contains an unsafe archive path") from exc
            if (name.casefold() in seen or any(part.casefold() == ".git" for part in relative.parts)
                    or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("bundle contains an unsafe archive path")
            seen.add(name.casefold())
            if not info.is_dir():
                entries[relative.as_posix()] = archive.read(info)
    heading = entries.get("INSTALL.md", b"").decode("utf-8").splitlines()
    if not heading or not heading[0].startswith(f"# Harness bundle for {machine}, built "):
        raise ValueError("baseline INSTALL.md does not identify the requested machine")
    for required in ("install.py", "install-manifest.json", f"machines/{machine}.json"):
        if required not in entries:
            raise ValueError(f"bundle has no {required}")
    return entries


def check_archive_paths(entries, machine, home):
    config = json.loads(entries[f"machines/{machine}.json"].decode("utf-8-sig"))
    local = entries.get(f"machines/{machine}.local.json")
    if local is not None:
        config = merge(config, json.loads(local.decode("utf-8-sig")))
    home_local = home / ".claude/local/machine.local.json"
    if home_local.is_file():
        config = merge(config, read_json(home_local))
    check_machine_paths(config, json.loads(entries["install-manifest.json"].decode("utf-8-sig")), home, allow_rule_rewrite=True)


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def plant_markers(install, home, machine):
    marker = "upgrade-check-marker " + secrets.token_hex(16)
    files = [install / relative for relative in (
        "HANDOFF-upgrade-check.md", ".scratch/upgrade-check-marker.txt",
        "bundles/upgrade-check-marker.txt", "inbox/upgrade-check-marker.txt",
    )]
    files.extend(home / ".claude" / relative for relative in (
        "skills/upgrade-check-marker/SKILL.md", "projects/upgrade-check/memory/upgrade-check-marker.md",
    ))
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(marker + "\n", encoding="utf-8")
    local_files = {}
    for suffix in ("md", "json"):
        path = install / "machines" / f"{machine}.local.{suffix}"
        if not path.is_file():
            continue
        local_files[path] = path.read_text(encoding="utf-8")
        if suffix == "json":
            value = read_json(path)
            value["upgrade_check_marker"] = marker
            write_json(path, value)
        else:
            path.write_text(local_files[path] + "\n" + marker + "\n", encoding="utf-8")
    settings_path = home / ".claude/settings.json"
    settings = read_json(settings_path) if settings_path.is_file() else {}
    settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append(HOOK)
    settings.setdefault("permissions", {}).setdefault("allow", []).append(PERMISSION)
    write_json(settings_path, settings)
    home_local = home / ".claude/local/machine.local.md"
    if home_local.is_file():
        with home_local.open("a", encoding="utf-8") as handle:
            handle.write("\n" + marker + "\n")
    else:
        home_local = None
    return marker, files, local_files, home_local


def holds_marker(path, marker):
    return path.is_file() and marker.encode("utf-8") in path.read_bytes()


def report_checks(work, machine, planted, before, statuses):
    install, home = work / "install", work / "home"
    marker, files, local_files, home_local = planted
    failed = False

    def report(status, message):
        nonlocal failed
        failed |= status == "FAIL"
        print(f"{status} {message}", flush=True)

    for stage, code in statuses:
        report("FAIL" if code else "PASS", f"{stage} exited {code}")
        if code:
            tail(work / f"{stage}.log")
    for path in files:
        root, key = (install, "install") if path.is_relative_to(install) else (home / ".claude", "home")
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        report("PASS" if digest == before[key][path.relative_to(root).as_posix()] else "FAIL", path.relative_to(work).as_posix())
    for path, old in local_files.items():
        relative = path.relative_to(install)
        if holds_marker(path, marker):
            report("PASS", f"install/{relative.as_posix()} kept marker")
            continue
        backups = [folder / relative for folder in sorted((install / ".scratch").glob("unpack-backup-*"))
                   if folder.is_dir() and re.fullmatch(r"unpack-backup-\d{8}-\d{6}", folder.name)
                   and folder.name not in before["backup_folders"] and holds_marker(folder / relative, marker)]
        if not backups:
            report("FAIL", f"install/{relative.as_posix()} lost marker without a backup")
            continue
        report("WARN", f"install/{relative.as_posix()} replaced; backup {backups[0]}")
        new = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                       fromfile=f"old/{relative.as_posix()}", tofile=f"new/{relative.as_posix()}", lineterm=""))
        for line in diff[:60]:
            print(line)
        if not diff:
            print("  old and new release copies have identical content")
        if len(diff) > 60:
            print("  diff truncated at 60 lines")
    try:
        settings = read_json(home / ".claude/settings.json")
        hook_kept = HOOK in settings.get("hooks", {}).get("PreToolUse", [])
        permission_kept = PERMISSION in settings.get("permissions", {}).get("allow", [])
    except (OSError, ValueError, AttributeError, TypeError):
        hook_kept = permission_kept = False
    report("PASS" if hook_kept else "FAIL", "settings.json marker PreToolUse entry")
    report("PASS" if permission_kept else "FAIL", "settings.json marker permission")
    if home_local is not None:
        if holds_marker(home_local, marker):
            report("PASS", "home/.claude/local/machine.local.md kept marker")
        elif home_local.is_file():
            source = install / "machines" / f"{machine}.local.md"
            if not source.is_file():
                source = install / "install.py"
            report("WARN", f"home/.claude/local/machine.local.md rewritten from {source}")
        else:
            report("FAIL", "home/.claude/local/machine.local.md disappeared")
    after = snapshot(home / ".claude")
    previous = before["home"]
    added, removed = set(after) - set(previous), set(previous) - set(after)
    changed = sorted(path for path in set(after) & set(previous) if after[path] != previous[path])
    print(f"INFO home/.claude: {len(added)} added, {len(changed)} changed, {len(removed)} removed")
    for path in changed[:20]:
        print("  " + path)
    print("INFO hand patches to harness-owned home files are reverted by every install and must be carried back into the source first")
    return 1 if failed else 0


def remove_work(work):
    def retry_remove(operation, path, error):
        target = Path(path)
        target.chmod(target.stat().st_mode | stat.S_IWRITE)
        operation(path)

    try:
        shutil.rmtree(work, onerror=retry_remove)
    except OSError as exc:
        print(f"INFO could not remove {work}: {exc}", flush=True)


def replay(args):
    retry = ["python", Path(__file__).resolve().as_posix(), "--machine", args.machine, "--repo", str(args.repo)]
    work = args.work
    if work == args.repo or args.repo.is_relative_to(work) or (work.exists() and any(work.iterdir())):
        raise CannotRun("work folder must be empty and must not contain the source repository", retry + ["--work", "<EMPTY_DIR>"])
    install, home = work / "install", work / "home"
    work.mkdir(parents=True, exist_ok=True)
    environment = scratch_environment(home)
    baseline = args.baseline.expanduser().resolve() if args.baseline else find_baseline(args.repo, args.machine, work, environment)
    if baseline is None or not baseline.is_file():
        raise CannotRun("no baseline zip for the machine", retry + ["--from", "<ZIP>"])
    entries = archive_entries(baseline, args.machine)
    check_archive_paths(entries, args.machine, home)
    check_tree_paths(args.repo, args.machine, home, allow_rule_rewrite=True)
    install.mkdir()
    for relative, content in entries.items():
        path = install / relative
        check_ancestors(path, root=install)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    rule_copies = {}
    prepare_machine(install, args.machine, home, rule_copies)
    command = [sys.executable, str(install / "install.py"), "--machine", args.machine,
               "--home", str(home), "--repo", str(install)]
    print(f"baseline: {baseline}", flush=True)
    print(f"INFO baseline bundle folder: {baseline.parent}", flush=True)
    if run(command + ["--no-tests"], cwd=install, env=environment, log=work / "baseline.log"):
        tail(work / "baseline.log")
        raise CannotRun(f"baseline install of {baseline} failed (see {work / 'baseline.log'})", retry + ["--from", str(baseline)])
    planted = plant_markers(install, home, args.machine)
    before = {"home": snapshot(home / ".claude"), "install": snapshot(install),
              "backup_folders": sorted(path.name for path in (install / ".scratch").iterdir() if path.is_dir())}
    write_json(work / "before.json", before)
    build = [sys.executable, str(args.repo / "install.py"), "--bundle", args.machine,
             "--out", str(work / "new.zip"), "--repo", str(args.repo), "--home", str(Path.home())]
    print("building new bundle", flush=True)
    # Bundle creation reads real-home term lists and writes only the requested output.
    if run(build, cwd=args.repo, env=os.environ.copy(), log=work / "bundle.log"):
        tail(work / "bundle.log")
        raise CannotRun(f"bundle build failed (see {work / 'bundle.log'})", retry + ["--from", str(baseline)])
    new_entries = archive_entries(work / "new.zip", args.machine)
    check_archive_paths(new_entries, args.machine, home)
    unpack = [sys.executable, str(install / "install.py"), "--unpack", str(work / "new.zip"), "--repo", str(install)]
    print("unpacking and installing upgrade", flush=True)
    unpack_code = run(unpack, cwd=install, env=environment, log=work / "unpack.log")
    statuses = [("unpack", unpack_code)]
    if not unpack_code:
        prepare_machine(install, args.machine, home, rule_copies)
        install_code = run(command + ([] if args.with_tests else ["--no-tests"]), cwd=install, env=environment, log=work / "install.log")
        statuses.append(("install", install_code))
    result = report_checks(work, args.machine, planted, before, statuses)
    if result == 0:
        if not args.keep:
            remove_work(work)
        print("next: ship the bundle; carry back the WARN files first if the remote edited them")
    else:
        print(f"next: fix the FAIL lines before shipping; work folder {work}")
    return result


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    retry = ["python", Path(__file__).resolve().as_posix(), *arguments]
    try:
        return replay(parse_args(arguments))
    except UsageError as exc:
        print(f"next: {exc}; run {command_text(retry[:2] + ['--help'])}")
        return 3
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code) if isinstance(exc.code, int) else 3
        if code:
            print(f"next: usage error; run {command_text(retry[:2] + ['--help'])}")
        return code
    except CannotRun as exc:
        print(f"next: {exc}; run {command_text(exc.command)}")
        return 2
    except (OSError, ValueError, TypeError, AttributeError, KeyError, zipfile.BadZipFile, RuntimeError) as exc:
        print(f"next: {exc}; rerun {command_text(retry)}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
