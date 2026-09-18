import datetime
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import project_bundle


@unittest.skipUnless(shutil.which("git"), "git is unavailable")
class ProjectBundleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.home = self.base / "home"
        self.terms = self.home / ".claude/local/bundle-terms"
        self.terms.mkdir(parents=True)
        (self.terms / "synthetic.txt").write_text("syntheticblockedtoken\n", encoding="utf-8")
        (self.terms.parent / "machine.json").write_text('{"owns": []}', encoding="utf-8")
        self.xdg = self.base / "xdg"
        self.xdg.mkdir()
        environment = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                                                   "XDG_CONFIG_HOME": str(self.xdg), "HOME": str(self.home),
                                                   "USERPROFILE": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic Author")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.write("CLAUDE.md", "Tracked instructions.\n")
        self.write(".gitignore", "docs/ignored*\nHANDOFF-*.md\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial synthetic tree")
        self.output = self.base / "bundle.zip"
        self.destination = self.base / "receiver"
        self.destination.mkdir()

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.repo), *arguments], capture_output=True,
                              text=True, encoding="utf-8", check=True)

    def write(self, relative, content, root=None):
        path = (root or self.repo) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def invoke(self, *arguments, expected=0):
        out = io.StringIO()
        with redirect_stdout(out):
            result = project_bundle.main([str(argument) for argument in arguments])
        self.assertEqual(result, expected, out.getvalue())
        return out.getvalue()

    def build(self, *arguments, expected=0):
        return self.invoke("build", self.repo, "--out", self.output, "--home", self.home, *arguments, expected=expected)

    def entries(self):
        with zipfile.ZipFile(self.output) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def harness_copy(self, global_terms=None):
        harness = self.base / "harness"
        (harness / "tools").mkdir(parents=True)
        source = Path(project_bundle.__file__)
        tool = harness / "tools/project_bundle.py"
        shutil.copyfile(source, tool)
        shutil.copyfile(source.parent.parent / "install.py", harness / "install.py")
        (harness / "claude/hooks").mkdir(parents=True)
        shutil.copyfile(source.parent.parent / "claude/hooks/_common.py", harness / "claude/hooks/_common.py")
        if global_terms is not None:
            (harness / "bundle-terms.txt").write_text(global_terms, encoding="utf-8")
        return tool

    def build_copy(self, tool, *arguments, expected=0):
        result = subprocess.run([sys.executable, str(tool), "build", str(self.repo), "--out", str(self.output),
                                 "--home", str(self.home), *map(str, arguments)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def make_archive(self, entries, recorded=None):
        manifest = project_bundle.manifest_text(self.repo, "abc1234", datetime.date(2026, 1, 1),
                                                {"": len(entries)}, "gate: synthetic", None,
                                                entries if recorded is None else recorded)
        with zipfile.ZipFile(self.output, "w") as archive:
            archive.writestr(project_bundle.MANIFEST, manifest)
            for name, content in entries.items():
                info = zipfile.ZipInfo("entry")
                info.filename = name
                archive.writestr(info, content)

    def linked_tree(self):
        linked = self.base / "linked"
        self.git("worktree", "add", "-q", "-b", "synthetic-branch", str(linked))
        self.write("docs/handoff-linked.md", "Linked context.\n", linked)
        return linked

    def test_tracked_files_are_never_packed(self):
        self.write("CLAUDE.md", "Modified tracked instructions.\n")
        self.write("staged.md", "Staged context.\n")
        self.git("add", "staged.md")
        self.build("--include", "**")
        self.assertEqual(set(self.entries()), {project_bundle.MANIFEST})

    def test_ignored_and_untracked_context_is_packed(self):
        names = ["HANDOFF-synthetic.md", "docs/ignored-note.md", "docs/handoff-note.md", "docs/plan-note.md",
                 "docs/deep/note.md", "docs/result.json", "docs/deep/result.json", ".claude/settings.json",
                 ".claude/rules/style.md", ".claude/skills/sample/SKILL.md", ".mcp.json", "AGENTS.md", "shared-modules.md"]
        for name in names:
            self.write(name, "Synthetic context.\n")
        self.build()
        self.assertEqual(set(self.entries()), {project_bundle.MANIFEST, *(f"files/{name}" for name in names)})

    def test_defaults_do_not_pack_code_or_unrelated_docs(self):
        for name in ("code.py", "other/note.md", "docs/code.py", "other/data.json", ".claude/agents/example.md"):
            self.write(name, "Synthetic context.\n")
        self.build()
        self.assertEqual(set(self.entries()), {project_bundle.MANIFEST})

    def test_untracked_nested_repository_is_not_a_file(self):
        self.git("init", "-q", str(self.repo / "nested"))
        self.write("nested/note.md", "Nested context.\n")
        self.write("docs/note.md", "Project context.\n")
        self.build()
        self.assertEqual(set(self.entries()), {project_bundle.MANIFEST, "files/docs/note.md"})

    def test_extra_globs_use_posix_relative_paths(self):
        self.write("extra/note.txt", "Extra context.\n")
        self.build("--include", "extra/*.txt")
        self.assertIn("files/extra/note.txt", self.entries())

    def test_include_star_matches_only_root_files(self):
        for name in ("note.md", "extra/note.md", "extra/deep/note.md"):
            self.write(name, "Synthetic context.\n")
        self.build("--include", "*.md")
        self.assertEqual(set(self.entries()), {project_bundle.MANIFEST, "files/note.md"})

    def test_include_double_star_matches_all_depths(self):
        names = ("note.md", "extra/note.md", "extra/deep/note.md")
        for name in names:
            self.write(name, "Synthetic context.\n")
        self.build("--include", "**/*.md")
        self.assertEqual(set(self.entries()), {project_bundle.MANIFEST, *("files/" + name for name in names)})

    def test_exclude_star_matches_only_direct_docs(self):
        direct = "docs/handoff-synthetic-timesheet.md"
        nested = "docs/deep/handoff-synthetic-timesheet.md"
        for name in (direct, nested):
            self.write(name, "Synthetic context.\n")
        self.build("--exclude", "docs/handoff-*timesheet*.md")
        self.assertEqual(set(self.entries()), {project_bundle.MANIFEST, "files/" + nested})

    def test_exclude_double_star_matches_docs_at_any_depth(self):
        for name in ("docs/handoff-synthetic-timesheet.md", "docs/deep/handoff-synthetic-timesheet.md",
                     "docs/deep/history/handoff-synthetic-timesheet.md", "docs/kept.md"):
            self.write(name, "Synthetic context.\n")
        self.build("--exclude", "docs/**/handoff-*timesheet*.md")
        self.assertEqual(set(self.entries()), {project_bundle.MANIFEST, "files/docs/kept.md"})

    def test_ignored_html_requires_explicit_include(self):
        name = "docs/erd/index.html"
        self.write(".git/info/exclude", name + "\n")
        self.write(name, "<html>Synthetic rendering.</html>\n")
        self.git("check-ignore", name)
        self.assertNotIn(name, self.build("--dry-run"))
        self.build()
        self.assertNotIn("files/" + name, self.entries())
        self.build("--include", "docs/**/*.html")
        self.assertEqual(self.entries()["files/" + name], b"<html>Synthetic rendering.</html>\n")

    def test_exclude_ignored_notes_from_main_tree_and_worktree(self):
        linked = self.linked_tree()
        name = "docs/handoff-2026-09-01-context.md"
        kept = "docs/handoff-2026-09-02-context.md"
        pattern = "docs/handoff-2026-09-01-*.md"
        for root in (self.repo, linked):
            self.write(".gitignore", pattern + "\n", root)
            self.write(name, "Excluded context.\n", root)
            self.write(kept, "Kept context.\n", root)
        preview = self.build("--worktrees", "--dry-run")
        self.assertIn("files/" + name, preview)
        self.assertIn("worktrees/linked/" + name, preview)
        preview = self.build("--worktrees", "--exclude", pattern, "--dry-run")
        self.assertNotIn(name, preview)
        self.build("--worktrees", "--exclude", pattern)
        entries = self.entries()
        for prefix in ("files/", "worktrees/linked/"):
            self.assertNotIn(prefix + name, entries)
            self.assertIn(prefix + kept, entries)

    def test_exclude_wins_over_include(self):
        name = "docs/erd/index.html"
        self.write(name, "<html>Synthetic rendering.</html>\n")
        options = ("--include", "docs/**/*.html", "--exclude", "docs/erd/*.html")
        self.assertNotIn(name, self.build(*options, "--dry-run"))
        self.build(*options)
        self.assertNotIn("files/" + name, self.entries())

    def test_excluded_file_never_reaches_gate(self):
        name = "docs/ignored-note.md"
        self.write(name, "syntheticblockedtoken\n")
        self.assertIn("GATE files/" + name, self.build(expected=2))
        options = ("--exclude", "docs/ignored-*.md")
        for extra in (("--dry-run",), ()):
            text = self.build(*options, *extra)
            self.assertNotIn(name, text)
            self.assertNotIn("GATE ", text)
        self.assertNotIn("files/" + name, self.entries())
        self.assertNotIn(name, self.entries()[project_bundle.MANIFEST].decode("utf-8"))

    def test_repeatable_excludes_are_case_sensitive_and_skip_size_output(self):
        self.write("docs/large.md", "x" * (2 * 1024 * 1024 + 1))
        self.write("docs/personal.md", "Synthetic personal record.\n")
        self.write("docs/Kept.md", "Kept context.\n")
        options = ("--exclude", "docs/large.md", "--exclude", "docs/personal.md", "--exclude", "docs/kept.md")
        text = self.build(*options, "--dry-run")
        self.assertNotIn("docs/large.md", text)
        self.assertNotIn("docs/personal.md", text)
        self.assertIn("files/docs/Kept.md\n", text)
        self.build(*options)
        self.assertEqual(set(self.entries()), {project_bundle.MANIFEST, "files/docs/Kept.md"})

    def test_local_names_always_excluded(self):
        for name in ("CLAUDE.local.md", ".claude/settings.local.json", "docs/note.local.md", ".env",
                     "docs/.env.sample", "docs/CLAUDE.local.md"):
            self.write(name, "Private context.\n")
        self.build("--include", "**")
        self.assertEqual(set(self.entries()), {project_bundle.MANIFEST})

    def test_excluded_directory_components(self):
        for name in ("node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build", ".scratch", ".terraform"):
            self.write(f"docs/{name}/note.md", "Excluded context.\n")
        self.write(".git/note.md", "Excluded context.\n")
        self.build("--include", "**")
        self.assertEqual(set(self.entries()), {project_bundle.MANIFEST})

    def test_oversize_file_is_skipped(self):
        self.write("docs/large.md", "x" * (2 * 1024 * 1024 + 1))
        self.assertIn("skipped docs/large.md (size)", self.build())
        self.assertNotIn("files/docs/large.md", self.entries())

    def test_worktree_files_have_separate_prefix(self):
        self.linked_tree()
        self.build("--worktrees")
        self.assertIn("worktrees/linked/docs/handoff-linked.md", self.entries())
        self.assertIn(b"Worktree linked: 1 files.", self.entries()[project_bundle.MANIFEST])

    def test_gate_refuses_ignored_note_without_writing(self):
        self.write("docs/ignored-note.md", "syntheticblockedtoken\n")
        text = self.build(expected=2)
        self.assertIn("GATE files/docs/ignored-note.md:1: syntheticblockedtoken", text)
        self.assertFalse(self.output.exists())

    def test_gate_refusal_preserves_existing_output(self):
        self.output.write_bytes(b"original")
        self.write("docs/ignored-note.md", "syntheticblockedtoken\n")
        self.build(expected=2)
        self.assertEqual(self.output.read_bytes(), b"original")

    def test_owned_domains_are_casefolded_and_terms_deduplicated(self):
        (self.terms.parent / "machine.json").write_text('{"owns": ["SYNTHETIC"]}', encoding="utf-8")
        (self.terms / "another.txt").write_text("otherblockedtoken\notherblockedtoken\n", encoding="utf-8")
        self.write("docs/note.md", "syntheticblockedtoken\n")
        self.assertIn("1 terms from 1 domain lists, owned: synthetic", self.build())

    def test_missing_domain_lists_warn_and_record_warning(self):
        empty_home = self.base / "empty-home"
        text = self.build("--home", empty_home)
        warning = next(line for line in text.splitlines() if line.startswith("warning:"))
        self.assertIn("gate uses the global list only", warning)
        self.assertIn(warning, self.entries()[project_bundle.MANIFEST].decode("utf-8"))
        self.assertNotIn(str(self.base), self.entries()[project_bundle.MANIFEST].decode("utf-8"))
        self.assertFalse(empty_home.exists())

    def test_missing_domain_lists_still_apply_global_gate(self):
        with redirect_stdout(io.StringIO()):
            install, terms, record, warning = project_bundle.gate_settings(self.home)
        global_terms = install.parse_bundle_terms("syntheticglobaltoken\n", "synthetic.txt")
        self.write("docs/ignored-note.md", "syntheticglobaltoken\n")
        with mock.patch.object(install, "read_bundle_terms", return_value=global_terms):
            self.assertIn("GATE files/docs/ignored-note.md:", self.build("--home", self.base / "empty-home", expected=2))
        self.assertFalse(self.output.exists())

    def test_malformed_domain_list_refuses(self):
        (self.terms / "synthetic.txt").write_text("substr:\n", encoding="utf-8")
        self.assertIn("empty term", self.build(expected=2))
        self.assertFalse(self.output.exists())

    def test_domain_lists_gate_without_global_list(self):
        tool = self.harness_copy()
        text = self.build_copy(tool)
        record = "gate: 0 global terms, 1 terms from 1 domain lists, owned: none (no global list)"
        self.assertIn(record, text)
        self.assertIn(record, self.entries()[project_bundle.MANIFEST].decode("utf-8"))
        self.write("docs/ignored-note.md", "syntheticblockedtoken\n")
        before = self.output.read_bytes()
        for options in ((), ("--allow-ungated",)):
            text = self.build_copy(tool, *options, expected=2)
            self.assertIn("GATE files/docs/ignored-note.md:1: syntheticblockedtoken", text)
            self.assertEqual(self.output.read_bytes(), before)

    def test_no_term_sources_refuses(self):
        tool = self.harness_copy()
        text = self.build_copy(tool, "--home", self.base / "empty-home", expected=2)
        self.assertEqual(text, "project bundle refused: no term lists on this machine (pass --allow-ungated to build anyway)\n")
        self.assertFalse(self.output.exists())

    def test_allow_ungated_build_records_missing_gate(self):
        tool = self.harness_copy()
        self.write("docs/note.md", "Synthetic context.\n")
        options = ("--home", self.base / "empty-home", "--allow-ungated")
        text = self.build_copy(tool, *options, "--dry-run")
        self.assertIn("warning: ungated build, no term lists on this machine", text)
        self.assertIn("would write", text)
        self.assertFalse(self.output.exists())
        text = self.build_copy(tool, *options)
        self.assertIn("warning: ungated build, no term lists on this machine", text)
        self.assertIn("gate: none", text)
        manifest = self.entries()[project_bundle.MANIFEST].decode("utf-8")
        self.assertIn("gate: none", manifest)
        self.assertIn("This bundle was built without a term gate; read the manifest table and the notes before sharing.", manifest)
        self.assertEqual(self.entries()["files/docs/note.md"], b"Synthetic context.\n")

    def test_allow_ungated_does_not_change_existing_gate(self):
        tool = self.harness_copy("syntheticglobaltoken\n")
        normal = self.build_copy(tool)
        flagged = self.build_copy(tool, "--allow-ungated")
        self.assertEqual(normal, flagged)
        self.assertIn("gate: 1 global terms, 1 terms from 1 domain lists, owned: none", flagged)
        for term in ("syntheticglobaltoken", "syntheticblockedtoken"):
            self.write("docs/ignored-note.md", term + "\n")
            self.assertIn(f"GATE files/docs/ignored-note.md:1: {term}", self.build_copy(tool, "--allow-ungated", expected=2))

    def test_owned_domains_are_dropped_without_global_list(self):
        tool = self.harness_copy()
        (self.terms.parent / "machine.json").write_text('{"owns": ["SYNTHETIC"]}', encoding="utf-8")
        self.write("docs/note.md", "syntheticblockedtoken\n")
        self.assertIn("gate: 0 global terms, 0 terms from 0 domain lists, owned: synthetic (no global list)", self.build_copy(tool))

    def test_allow_ungated_does_not_bypass_malformed_lists(self):
        tool = self.harness_copy()
        for content, reason in (("substr:\n", "empty term"), ("", "holds no terms")):
            (self.terms / "synthetic.txt").write_text(content, encoding="utf-8")
            self.assertIn(reason, self.build_copy(tool, "--allow-ungated", expected=2))
            self.assertFalse(self.output.exists())

    def test_invalid_ownership_refuses(self):
        (self.terms.parent / "machine.json").write_text('{"owns": "synthetic"}', encoding="utf-8")
        self.assertIn("owns must be a list", self.build(expected=2))

    def test_manifest_names_are_gated(self):
        self.write("docs/syntheticblockedtoken.md", "Safe content.\n")
        self.assertIn("GATE PROJECT-BUNDLE.md:", self.build(expected=2))
        self.assertFalse(self.output.exists())

    def test_manifest_lists_correct_sizes_and_hashes(self):
        self.write("docs/note.md", "Synthetic context.\n")
        self.linked_tree()
        self.build("--worktrees")
        entries = self.entries()
        manifest = entries.pop(project_bundle.MANIFEST).decode("utf-8")
        rows = [line.split(" | ") for line in manifest.splitlines() if line.startswith(("| files/", "| worktrees/"))]
        self.assertEqual(len(rows), len(entries))
        for row in rows:
            name, size, digest = row[0][2:], row[1], row[2][:-2]
            self.assertEqual(int(size), len(entries[name]))
            self.assertEqual(digest, hashlib.sha256(entries[name]).hexdigest())
        self.assertIn(self.git("rev-parse", "--short", "HEAD").stdout.strip(), manifest)
        self.assertIn("apply never overwrites an existing file.", manifest)

    def test_list_prints_manifest(self):
        self.build()
        self.assertEqual(self.invoke("list", self.output), self.entries()[project_bundle.MANIFEST].decode("utf-8"))

    def test_missing_manifest_refuses_list_and_apply(self):
        with zipfile.ZipFile(self.output, "w") as archive:
            archive.writestr("files/docs/note.md", "Context.")
        for arguments in (("list", self.output), ("apply", self.output, self.destination)):
            self.assertIn("zip has no PROJECT-BUNDLE.md", self.invoke(*arguments, expected=2))

    def test_apply_writes_skips_and_maps_worktrees(self):
        self.write("docs/note.md", "New context.\n")
        self.write("HANDOFF-synthetic.md", "New handoff.\n")
        self.linked_tree()
        self.build("--worktrees")
        existing = self.write("HANDOFF-synthetic.md", "Keep these exact bytes.\n", self.destination)
        before = existing.read_bytes()
        text = self.invoke("apply", self.output, self.destination)
        self.assertIn("skipped HANDOFF-synthetic.md (exists)", text)
        self.assertIn("applied 2 written, 1 skipped", text)
        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual((self.destination / "docs/note.md").read_bytes(), b"New context.\n")
        self.assertEqual((self.destination / "docs/from-worktrees/linked/docs/handoff-linked.md").read_bytes(), b"Linked context.\n")

    def test_apply_dry_run_writes_nothing(self):
        self.make_archive({"files/docs/note.md": b"Context", "files/exists.md": b"Replacement"})
        existing = self.write("exists.md", "Original", self.destination)
        text = self.invoke("apply", self.output, self.destination, "--dry-run")
        self.assertIn("would write docs/note.md", text)
        self.assertIn("skipped exists.md (exists)", text)
        self.assertEqual(list(self.destination.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"Original")

    def test_apply_refuses_unsafe_paths_before_writing(self):
        for name in ("files/../escape.md", "files//absolute.md", "files/C:/escape.md", "files/dir\\escape.md",
                     "/files/absolute.md", "worktrees/../note.md", "files/.. /escape.md", "files/NUL.md"):
            with self.subTest(name=name):
                self.make_archive({"files/first.md": b"First", name: b"Unsafe"})
                self.assertIn("unsafe path:", self.invoke("apply", self.output, self.destination, expected=2))
                self.assertEqual(list(self.destination.iterdir()), [])

    def test_apply_refuses_symlink_ancestor(self):
        outside = self.base / "outside"
        outside.mkdir()
        try:
            (self.destination / "docs").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation is unavailable")
        self.make_archive({"files/docs/note.md": b"Context"})
        self.assertIn("unsafe symlink destination:", self.invoke("apply", self.output, self.destination, expected=2))
        self.assertEqual(list(outside.iterdir()), [])

    def test_apply_accepts_symlink_above_destination_root(self):
        alias = self.base / "alias"
        try:
            alias.symlink_to(self.base, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation is unavailable")
        self.make_archive({"files/docs/note.md": b"Context"})
        text = self.invoke("apply", self.output, alias / self.destination.name)
        self.assertIn("written docs/note.md", text)
        self.assertEqual((self.destination / "docs/note.md").read_bytes(), b"Context")

    def test_apply_refuses_oversize_entry_before_any_read(self):
        for name in ("files/docs/large.md", "worktrees/linked/docs/large.md"):
            self.make_archive({"files/first.md": b"First", name: b"x" * (project_bundle.MAX_SIZE + 1)})
            for options in ((), ("--dry-run",)):
                with mock.patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("archive was read")) as read:
                    text = self.invoke("apply", self.output, self.destination, *options, expected=2)
                self.assertEqual(text, f"project bundle refused: {name} exceeds {project_bundle.MAX_SIZE} bytes\n")
                read.assert_not_called()
                self.assertEqual(list(self.destination.iterdir()), [])

    def test_apply_refuses_oversize_manifest_before_any_read(self):
        with zipfile.ZipFile(self.output, "w") as archive:
            archive.writestr(project_bundle.MANIFEST, b"x" * (project_bundle.MAX_SIZE + 1))
        with mock.patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("archive was read")) as read:
            text = self.invoke("apply", self.output, self.destination, expected=2)
        self.assertEqual(text, f"project bundle refused: PROJECT-BUNDLE.md exceeds {project_bundle.MAX_SIZE} bytes\n")
        read.assert_not_called()

    def test_apply_refuses_manifest_size_mismatch_before_payload_read(self):
        name = "files/docs/note.md"
        self.make_archive({name: b"Context"}, {name: b"Different size"})
        original_read = zipfile.ZipFile.read

        def read_manifest_only(archive, entry, *args, **kwargs):
            self.assertEqual(entry, project_bundle.MANIFEST)
            return original_read(archive, entry, *args, **kwargs)

        for options in ((), ("--dry-run",)):
            with mock.patch.object(zipfile.ZipFile, "read", autospec=True, side_effect=read_manifest_only):
                text = self.invoke("apply", self.output, self.destination, *options, expected=2)
            self.assertEqual(text, f"project bundle refused: {name} size disagrees with manifest\n")
            self.assertEqual(list(self.destination.iterdir()), [])

    def test_apply_refuses_tampered_content_by_hash(self):
        self.make_archive({"files/docs/note.md": b"Tampered"}, {"files/docs/note.md": b"Original"})
        for options in ((), ("--dry-run",)):
            self.assertIn("sha256 mismatch: files/docs/note.md", self.invoke("apply", self.output, self.destination, *options, expected=2))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_apply_refuses_missing_hash(self):
        self.make_archive({"files/note.md": b"Context"}, {})
        self.assertIn("manifest has no hash", self.invoke("apply", self.output, self.destination, expected=2))

    def test_apply_refuses_colliding_destinations(self):
        self.make_archive({"files/docs/from-worktrees/linked/note.md": b"First", "worktrees/linked/note.md": b"Second"})
        self.assertIn("duplicate destination", self.invoke("apply", self.output, self.destination, expected=2))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_build_dry_run_writes_nothing(self):
        self.write("docs/note.md", "Context.\n")
        text = self.build("--dry-run")
        self.assertIn("files/docs/note.md\n", text)
        self.assertIn("gate:", text)
        self.assertIn("would write", text)
        self.assertFalse(self.output.exists())

    def test_same_day_builds_are_byte_identical(self):
        self.write("docs/note.md", "Context.\n")
        first_time = datetime.datetime(2026, 1, 1, 1, tzinfo=datetime.timezone.utc)
        second_time = first_time.replace(hour=20)
        with mock.patch.object(project_bundle.datetime, "datetime") as clock:
            clock.now.return_value = first_time
            self.build()
            before = self.output.read_bytes()
            clock.now.return_value = second_time
            self.build()
        self.assertEqual(self.output.read_bytes(), before)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.namelist(), sorted(archive.namelist()))
            self.assertTrue(all(info.date_time == (2026, 1, 1, 0, 0, 0) for info in archive.infolist()))
            self.assertTrue(all(info.compress_type == zipfile.ZIP_DEFLATED for info in archive.infolist()))

    def test_build_from_subdirectory_uses_top_level(self):
        self.write("docs/note.md", "Context.\n")
        self.invoke("build", self.repo / "docs", "--out", self.output, "--home", self.home)
        self.assertIn("files/docs/note.md", self.entries())

    def test_build_requires_git_and_apply_requires_directory(self):
        self.invoke("build", self.destination, "--out", self.output, "--home", self.home, expected=2)
        self.build()
        self.assertIn("must exist and be a directory", self.invoke("apply", self.output, self.base / "absent", expected=2))

    def test_build_requires_harness_installer(self):
        with mock.patch.object(project_bundle, "__file__", str(self.base / "tools/project_bundle.py")):
            self.assertIn("project bundle must run from a harness install folder", self.build(expected=2))


if __name__ == "__main__":
    unittest.main()
