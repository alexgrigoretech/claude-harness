"""Pack and restore untracked project context with the shared bundle term gate."""

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath


DEFAULT_PATTERNS = ("*.md", "docs/**/*.md", "docs/**/*.json",
                    ".claude/*.json", ".claude/rules/*.md", ".claude/skills/**/*.md",
                    ".mcp.json", "AGENTS.md", "shared-modules.md")
EXCLUDED_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__", "target",
                  "dist", "build", ".scratch", ".terraform"}
MAX_SIZE = 2 * 1024 * 1024
MANIFEST = "PROJECT-BUNDLE.md"


def git(repo, *arguments):
    allowed = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_SYSTEM"}
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_") or key in allowed}
    environment.update(GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, env=environment)
    if result.returncode:
        reason = result.stderr.decode("utf-8", errors="replace").splitlines()
        raise ValueError(reason[0] if reason else "git query failed")
    return result.stdout.decode("utf-8")


def matches_default(parts, pattern):
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return matches_default(parts, pattern[1:]) or bool(parts and matches_default(parts[1:], pattern))
    return bool(parts and fnmatch.fnmatchcase(parts[0], pattern[0]) and matches_default(parts[1:], pattern[1:]))


def safe_relative(name):
    relative = PurePosixPath(name)
    if (not name or relative.is_absolute() or PureWindowsPath(name).drive or "\\" in name
            or any(part in ("", ".", "..") for part in name.split("/"))
            or any(part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved() for part in relative.parts)
            or any(character in name for character in "\r\n\x00:")):
        raise ValueError(f"unsafe path: {name!r}")
    return relative


def check_ancestors(path, root=None):
    for ancestor in (path, *path.parents):
        junction = getattr(ancestor, "is_junction", lambda: False)
        if ancestor.is_symlink() or junction():
            raise ValueError(f"unsafe symlink destination: {ancestor}")
        if ancestor == root:
            break


def selected_files(root, includes, excludes):
    records = iter(git(root, "status", "--short", "--ignored", "--untracked-files=all", "-z").split("\0"))
    candidates = set()
    for record in records:
        status = record[:2]
        if "R" in status or "C" in status:
            next(records, None)
        if status in ("??", "!!"):
            candidates.add(record[3:])
    for name in sorted(candidates):
        if name.endswith("/"):
            continue
        relative = safe_relative(name)
        if (set(relative.parts) & EXCLUDED_PARTS or ".local." in relative.name
                or relative.name.startswith(".env")):
            continue
        if not (any(matches_default(relative.parts, pattern.split("/")) for pattern in DEFAULT_PATTERNS)
                or any(matches_default(relative.parts, pattern.split("/")) for pattern in includes)):
            continue
        if any(matches_default(relative.parts, pattern.split("/")) for pattern in excludes):
            continue
        path = root / name
        check_ancestors(path)
        if not path.is_file():
            continue
        content = path.read_bytes()
        if len(content) > MAX_SIZE:
            print(f"skipped {name} (size)")
            continue
        yield name, content


def load_terms(install, home, global_path):
    if global_path.exists():
        try:
            return install.load_bundle_terms(home)
        except install.NoDomainLists:
            return install.read_bundle_terms(global_path, "bundle-terms.txt"), {}
    folder = install.bundle_terms_directory(home)
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
        raise install.BundleRefusal(f"{folder}: {exc}") from exc
    domain_paths = {}
    for path in paths:
        name = path.stem.casefold()
        if name in domain_paths:
            raise install.BundleRefusal(f"domain lists {domain_paths[name]} and {path} have the same case-insensitive name")
        domain_paths[name] = path
    domains = {name: install.read_bundle_terms(path, str(path)) for name, path in domain_paths.items()}
    for terms in domains.values():
        if not terms:
            raise install.BundleRefusal(f"{terms.path} holds no terms")
    return [], domains


def gate_settings(home, allow_ungated=False):
    harness = Path(__file__).resolve().parent.parent
    if not (harness / "install.py").is_file():
        raise ValueError("project bundle must run from a harness install folder")
    sys.path.insert(0, str(harness))
    try:
        import install
    finally:
        sys.path.pop(0)
    machine = home / ".claude/local/machine.json"
    settings = json.loads(machine.read_text(encoding="utf-8-sig")) if machine.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError("machine.json must contain an object")
    owns = settings.get("owns", [])
    if not isinstance(owns, list) or any(not isinstance(name, str) for name in owns):
        raise ValueError("machine.json owns must be a list of names")
    owned = {name.casefold() for name in owns}
    global_path = harness / "bundle-terms.txt"
    has_global = global_path.exists()
    global_terms, domains = load_terms(install, home, global_path)
    warning = None
    if not has_global and not domains:
        if not allow_ungated:
            raise ValueError("project bundle refused: no term lists on this machine (pass --allow-ungated to build anyway)")
        print("warning: ungated build, no term lists on this machine")
        print("gate: none")
        return install, None, "gate: none", "This bundle was built without a term gate; read the manifest table and the notes before sharing."
    if not domains:
        warning = "warning: no domain lists under .claude/local/bundle-terms, gate uses the global list only"
        print(warning)
    excluded = {name: terms for name, terms in domains.items() if name.casefold() not in owned}
    global_terms = install.dedupe_terms(global_terms)
    selected_terms = install.dedupe_terms([term for terms in excluded.values() for term in terms], excluded=global_terms)
    record = (f"gate: {len(global_terms)} global terms, {len(selected_terms)} terms from {len(excluded)} domain lists, "
              f"owned: {', '.join(sorted(owned)) or 'none'}")
    if not has_global:
        record += " (no global list)"
    print(record)
    return install, global_terms + selected_terms, record, warning


def table_path(name):
    return name.replace("%", "%25").replace("|", "%7C")


def manifest_text(root, commit, date, counts, record, warning, entries):
    lines = [f"Project bundle for {root.name}.", "", f"Built {date.isoformat()} (UTC), repository HEAD {commit}.", "",
             f"Main tree {root.name}: {counts['']} files."]
    lines.extend(f"Worktree {name}: {count} files." for name, count in sorted(counts.items()) if name)
    lines.extend(["", record, ""])
    if warning:
        lines.extend([warning, ""])
    lines.extend(["apply never overwrites an existing file.", "", "Table paths escape percent signs and vertical bars as %25 and %7C.", "",
                  "| Zip path | Size in bytes | SHA256 |", "| --- | --- | --- |"])
    for name, content in sorted(entries.items()):
        lines.append(f"| {table_path(name)} | {len(content)} | {hashlib.sha256(content).hexdigest()} |")
    return "\n".join(lines) + "\n"


def build(args):
    root = Path(git(args.repo, "rev-parse", "--show-toplevel").strip()).resolve()
    entries = {f"files/{name}": content for name, content in selected_files(root, args.include, args.exclude)}
    counts = {"": len(entries)}
    if args.worktrees:
        trees = [Path(field[9:]) for field in git(root, "worktree", "list", "--porcelain", "-z").split("\0")
                 if field.startswith("worktree ")]
        for tree in trees:
            if tree.resolve() == root:
                continue
            name = tree.name
            if name.casefold() in {key.casefold() for key in counts}:
                raise ValueError(f"duplicate worktree basename: {name}")
            files = list(selected_files(tree, args.include, args.exclude))
            counts[name] = len(files)
            entries.update({f"worktrees/{name}/{relative}": content for relative, content in files})
    if args.dry_run:
        for name in sorted(entries):
            print(name)
    install, terms, record, warning = gate_settings((args.home or Path.home()).expanduser(), args.allow_ungated)
    date = datetime.datetime.now(datetime.timezone.utc).date()
    commit = git(root, "rev-parse", "--short", "HEAD").strip()
    count = len(entries)
    entries[MANIFEST] = manifest_text(root, commit, date, counts, record, warning, entries).encode("utf-8")
    hits = install.bundle_gate(sorted(entries.items()), terms) if terms is not None else 0
    if hits:
        raise ValueError(f"project bundle refused: {hits} gate hits")
    if args.dry_run:
        print(f"would write {args.out} ({count} files)")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            info = zipfile.ZipInfo(name, (date.year, date.month, date.day, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    print(f"project bundle written {args.out} ({count} files)")
    return 0


def read_manifest(archive):
    if MANIFEST not in archive.namelist():
        raise ValueError(f"zip has no {MANIFEST}")
    return archive.read(MANIFEST).decode("utf-8")


def manifest_hashes(text):
    hashes = {}
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        columns = [column.strip() for column in line.split("|")[1:-1]]
        if len(columns) != 3 or columns[0] in ("Zip path", "---"):
            continue
        name, size, digest = columns
        name = name.replace("%7C", "|").replace("%25", "%")
        if name in hashes or not size.isdecimal() or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"invalid manifest row: {name}")
        hashes[name] = (int(size), digest)
    return hashes


def apply(args):
    repo = args.repo.resolve()
    if not repo.is_dir():
        raise ValueError("apply destination must exist and be a directory")
    check_ancestors(repo, repo)
    with zipfile.ZipFile(args.zip) as archive:
        infos = archive.infolist()
        for info in infos:
            if info.file_size > MAX_SIZE:
                raise ValueError(f"project bundle refused: {info.filename} exceeds {MAX_SIZE} bytes")
        hashes = manifest_hashes(read_manifest(archive))
        planned = []
        seen = set()
        for info in infos:
            safe_relative(info.orig_filename)
            if info.filename == MANIFEST:
                continue
            parts = safe_relative(info.filename).parts
            if parts[0] == "files" and len(parts) > 1:
                relative = PurePosixPath(*parts[1:])
            elif parts[0] == "worktrees" and len(parts) > 2:
                relative = PurePosixPath("docs/from-worktrees", *parts[1:])
            else:
                raise ValueError(f"unknown zip entry: {info.filename}")
            destination = repo / relative
            check_ancestors(destination, repo)
            if destination in seen:
                raise ValueError(f"duplicate destination: {relative}")
            seen.add(destination)
            if info.filename not in hashes:
                raise ValueError(f"manifest has no hash for {info.filename}")
            if hashes[info.filename][0] != info.file_size:
                raise ValueError(f"project bundle refused: {info.filename} size disagrees with manifest")
            planned.append((info, relative, destination))
        written = skipped = 0
        for info, relative, destination in planned:
            check_ancestors(destination, repo)
            if destination.exists():
                print(f"skipped {relative.as_posix()} (exists)")
                skipped += 1
                continue
            content = archive.read(info)
            if hashlib.sha256(content).hexdigest() != hashes[info.filename][1]:
                raise ValueError(f"sha256 mismatch: {info.filename}")
            if args.dry_run:
                print(f"would write {relative.as_posix()}")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as handle:
                    handle.write(content)
                print(f"written {relative.as_posix()}")
                if hashlib.sha256(destination.read_bytes()).hexdigest() != hashes[info.filename][1]:
                    raise ValueError(f"sha256 mismatch: {info.filename}")
            written += 1
    print(f"applied {written} written, {skipped} skipped")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("repo", type=Path)
    builder.add_argument("--out", required=True, type=Path)
    builder.add_argument("--worktrees", action="store_true")
    builder.add_argument("--include", action="append", default=[])
    builder.add_argument("--exclude", action="append", default=[])
    builder.add_argument("--home", type=Path)
    builder.add_argument("--dry-run", action="store_true")
    builder.add_argument("--allow-ungated", action="store_true")
    listing = commands.add_parser("list")
    listing.add_argument("zip", type=Path)
    receiver = commands.add_parser("apply")
    receiver.add_argument("zip", type=Path)
    receiver.add_argument("repo", type=Path)
    receiver.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            return build(args)
        if args.command == "apply":
            return apply(args)
        with zipfile.ZipFile(args.zip) as archive:
            print(read_manifest(archive), end="")
        return 0
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        print(str(exc).splitlines()[0])
        return 2


if __name__ == "__main__":
    sys.exit(main())
