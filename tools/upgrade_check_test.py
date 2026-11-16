"""Tests for the isolated bundle upgrade replay."""

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import upgrade_check as check


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def bundle(path, machine="fixture", local=True, config=None, local_config=None):
    entries = {
        "install.py": "# Fixture installer.\n",
        "INSTALL.md": f"# Harness bundle for {machine}, built 2026-01-01\n",
        "install-manifest.json": "{}",
        f"machines/{machine}.json": json.dumps(config or {}),
    }
    if local:
        entries[f"machines/{machine}.local.md"] = "Old release facts.\n"
        entries[f"machines/{machine}.local.json"] = json.dumps(local_config or {})
    entries["MANIFEST.txt"] = "\n".join(sorted([*entries, "MANIFEST.txt"])) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for relative, text in entries.items():
            archive.writestr(relative, text)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        write(self.repo / "install.py", "# Fixture installer.\n")
        write(self.repo / "install-manifest.json", "{}")
        write(self.repo / "machines/fixture.json", "{}")
        self.baseline = self.repo / "bundles/harness-bundle-fixture-2026-01-01.zip"
        bundle(self.baseline)
        self.work = self.base / "work"
        self.calls = []
        self.baseline_code = 0
        self.bundle_code = 0
        self.unpack_code = 0
        self.install_code = 0
        self.on_unpack = lambda: None
        self.on_install = lambda: None

    def runner(self, command, *, cwd, env, log):
        self.calls.append((command, cwd, env.copy(), log))
        log.write_text("\n".join(f"output {index}" for index in range(25)), encoding="utf-8")
        if log.name == "baseline.log":
            home = self.work / "home"
            write(home / ".claude/settings.json", "{}")
            write(home / ".claude/local/machine.local.md", "Home facts.\n")
            return self.baseline_code
        if log.name == "bundle.log":
            bundle(self.work / "new.zip")
            return self.bundle_code
        if log.name == "unpack.log":
            self.on_unpack()
            return self.unpack_code
        self.on_install()
        return self.install_code

    def invoke(self, *extra):
        output = io.StringIO()
        argv = ["--machine", "fixture", "--repo", str(self.repo), "--work", str(self.work), *extra]
        with mock.patch.object(check, "run", side_effect=self.runner), contextlib.redirect_stdout(output):
            code = check.main(argv)
        text = output.getvalue()
        self.assertTrue(text.splitlines()[-1].startswith("next:"), text)
        return code, text

    def replace_local(self, backup=True):
        for suffix in ("md", "json"):
            path = self.work / "install/machines" / f"fixture.local.{suffix}"
            if backup:
                saved = self.work / "install/.scratch/unpack-backup-20260101-010203/machines" / path.name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, saved)
            write(path, "New release facts.\n" if suffix == "md" else '{"updated": true}\n')

    def test_newest_date_then_suffix(self):
        names = ["2026-01-02", "2026-01-02-3", "2026-01-02b", "2025-12-31z"]
        for name in names:
            bundle(self.repo / "bundles" / f"harness-bundle-fixture-{name}.zip")
        bundle(self.repo / "bundles/harness-bundle-acme-2027-01-01.zip", machine="acme")
        write(self.repo / "bundles/harness-bundle-fixture-2026-99-99.zip", "invalid date")
        self.assertEqual(check.newest_bundle(self.repo, "fixture").name, "harness-bundle-fixture-2026-01-02b.zip")

    def test_same_day_suffixes_use_natural_order(self):
        paths = [self.repo / "bundles" / f"harness-bundle-fixture-2026-01-02{suffix}.zip"
                 for suffix in ("", "-2", "-10", "b")]
        for path in paths:
            bundle(path)
        for expected in reversed(paths):
            self.assertEqual(check.newest_bundle(self.repo, "fixture"), expected)
            expected.unlink()

    def test_missing_zip(self):
        self.baseline.unlink()
        code, text = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("no baseline zip", text)
        self.assertIn("--from", text)
        self.assertEqual(self.calls, [])

    def test_worktree_falls_back_to_main_tree_bundles(self):
        self.baseline.unlink()
        write(self.repo / ".git", "gitdir: fixture\n")
        main = self.base / "main tree"
        expected = main / "bundles/harness-bundle-fixture-2026-01-02b.zip"
        bundle(expected)
        bundle(main / "bundles/harness-bundle-fixture-2026-01-02.zip")
        original = self.runner
        def runner(command, **kwargs):
            if command[0] == "git":
                self.calls.append((command, kwargs["cwd"], kwargs["env"].copy(), kwargs["log"]))
                self.assertEqual(command[1:], ["-C", str(self.repo), "rev-parse", "--path-format=absolute", "--git-common-dir"])
                write(kwargs["log"], str(main / ".git") + "\n")
                return 0
            return original(command, **kwargs)
        self.runner = runner
        code, text = self.invoke()
        self.assertEqual(code, 0, text)
        self.assertIn(f"baseline: {expected}", text)
        self.assertIn(f"INFO baseline bundle folder: {main / 'bundles'}", text)
        self.assertEqual(self.calls[0][2]["HOME"], str(self.work / "home"))
        self.assertEqual(self.calls[0][2]["USERPROFILE"], str(self.work / "home"))
        self.assertEqual(self.calls[0][2]["XDG_CONFIG_HOME"], str(self.work / "home/.config"))

    def test_local_bundle_takes_precedence_over_main_tree(self):
        write(self.repo / ".git", "gitdir: fixture\n")
        code, text = self.invoke()
        self.assertEqual(code, 0, text)
        self.assertIn(f"baseline: {self.baseline}", text)
        self.assertTrue(all(command[0] != "git" for command, _, _, _ in self.calls))

    def test_proxy_placeholder_uses_merged_local_configuration(self):
        bundle(self.baseline, config={"proxy": {"host": "proxy.example.invalid", "key_file": "unused.key"}},
               local_config={"proxy": {"key_file": "~/.claude/local/proxy-client.key"}})
        original = self.runner
        def runner(command, **kwargs):
            if kwargs["log"].name in ("baseline.log", "install.log"):
                key = self.work / "home/.claude/local/proxy-client.key"
                self.assertEqual(key.read_text(encoding="utf-8"), "upgrade-check-placeholder\n")
                self.assertFalse((self.work / "home/unused.key").exists())
                self.assertRegex(key.read_text(encoding="utf-8").strip(), r"^[A-Za-z0-9_-]+$")
                self.assertEqual(command[0], sys.executable)
            return original(command, **kwargs)
        self.runner = runner
        code, text = self.invoke()
        self.assertEqual(code, 0, text)
        self.assertIn("INFO planted a placeholder proxy key; the proxy itself is not exercised", text)
        self.assertEqual(len(self.calls), 4)

    def test_proxy_missing_empty_and_unsafe_key_paths(self):
        for index, proxy in enumerate(({}, {"key_file": None}, {"key_file": ""}, {"key_file": "  "},
                                       {"key_file": "."}, {"key_file": "../outside.key"})):
            with self.subTest(proxy=proxy):
                self.work = self.base / f"proxy{index}"
                self.calls.clear()
                self.baseline_code = 2
                bundle(self.baseline, config={"proxy": proxy})
                code, text = self.invoke()
                self.assertEqual(code, 2, text)
                self.assertTrue((self.work / "home").is_dir())
                self.assertNotIn("planted a placeholder proxy key", text)
                if index < 4:
                    self.assertIn("baseline install of", text)
                    self.assertEqual(len(self.calls), 1)
                else:
                    self.assertIn("refused side effect", text)
                    self.assertEqual(self.calls, [])
                self.assertFalse((self.work / "outside.key").exists())
        output = io.StringIO()
        with mock.patch.object(check, "parse_args", side_effect=KeyError("fixture")), contextlib.redirect_stdout(output):
            self.assertEqual(check.main([]), 2)
        self.assertTrue(output.getvalue().startswith("next:"))

    def test_external_allow_rules_use_scratch_copies_on_both_installs(self):
        outside = self.base / "original/settings.json"
        write(outside, '{"original": true}\n')
        config = {"remove_allow_rules": [{"file": str(outside), "match": "fixture"}]}
        local = {"remove_allow_rules": [{"file": "Z:/fixture/settings.json", "match": "acme"}]}
        bundle(self.baseline, config=config, local_config=local)
        check.write_json(self.repo / "machines/fixture.json", config)
        original = self.runner
        def runner(command, **kwargs):
            if kwargs["log"].name in ("baseline.log", "install.log"):
                for index, filename in enumerate(("fixture.json", "fixture.local.json")):
                    value = check.read_json(self.work / "install/machines" / filename)
                    target = Path(value["remove_allow_rules"][0]["file"])
                    self.assertEqual(target, self.work / "outside" / str(index) / "settings.json")
                    self.assertEqual(check.read_json(target), {"permissions": {"allow": []}})
            return original(command, **kwargs)
        self.runner = runner
        def unpack():
            check.write_json(self.work / "install/machines/fixture.json", config)
            path = self.work / "install/machines/fixture.local.json"
            value = check.read_json(path)
            value.update(local)
            check.write_json(path, value)
        self.on_unpack = unpack
        code, text = self.invoke()
        self.assertEqual(code, 0, text)
        self.assertIn(f"INFO remove_allow_rules for {outside} replayed on a scratch copy", text)
        self.assertIn("INFO remove_allow_rules for Z:/fixture/settings.json replayed on a scratch copy", text)
        self.assertEqual(outside.read_text(encoding="utf-8"), '{"original": true}\n')
        self.assertEqual(check.read_json(self.repo / "machines/fixture.json"), config)

    def test_exit_two_guidance_has_one_typed_command(self):
        for failure in ("missing", "baseline", "bundle", "unsafe"):
            with self.subTest(failure=failure):
                self.work = self.base / failure
                self.baseline_code = 1 if failure == "baseline" else 0
                self.bundle_code = 1 if failure == "bundle" else 0
                bundle(self.baseline, config={"delete": ["../outside"]} if failure == "unsafe" else None)
                if failure == "missing":
                    self.baseline.unlink()
                code, text = self.invoke()
                self.assertEqual(code, 2, text)
                line = text.splitlines()[-1]
                self.assertEqual(line.count("next:"), 1)
                self.assertEqual(line.count(check.Path(check.__file__).resolve().as_posix()), 1)
                self.assertEqual(line.count("python "), 1)
                self.assertNotIn(sys.executable, line)
                self.assertNotIn("correct the input", line)
                if failure == "missing":
                    self.assertIn("--from", line)
                    self.assertEqual(line.count("no baseline zip for the machine"), 1)

    def test_baseline_failure_tail_and_retention(self):
        self.baseline_code = 9
        code, text = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("baseline install of", text)
        self.assertIn("  output 5\n", text)
        self.assertNotIn("  output 4\n", text)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.work.is_dir())

    def test_bundle_failure(self):
        self.bundle_code = 2
        code, text = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("bundle build failed", text)
        self.assertEqual(len(self.calls), 2)

    def test_markers_snapshots_commands_environment_and_cleanup(self):
        def inspect():
            install, home = self.work / "install", self.work / "home"
            local = check.read_json(install / "machines/fixture.local.json")
            marker = local["upgrade_check_marker"]
            self.assertRegex(marker, r"^upgrade-check-marker [a-f0-9]{32}$")
            self.assertIn(marker, (install / "machines/fixture.local.md").read_text(encoding="utf-8"))
            before = check.read_json(self.work / "before.json")
            self.assertEqual(before["install"], check.snapshot(install))
            self.assertEqual(before["home"], check.snapshot(home / ".claude"))
            for path in ("HANDOFF-upgrade-check.md", ".scratch/upgrade-check-marker.txt", "bundles/upgrade-check-marker.txt", "inbox/upgrade-check-marker.txt"):
                self.assertTrue(check.holds_marker(install / path, marker))
        self.on_unpack = inspect
        code, text = self.invoke()
        self.assertEqual(code, 0, text)
        self.assertNotIn("FAIL", text)
        for line in ("install/HANDOFF-upgrade-check.md", "install/.scratch/upgrade-check-marker.txt",
                     "install/bundles/upgrade-check-marker.txt", "install/inbox/upgrade-check-marker.txt",
                     "home/.claude/skills/upgrade-check-marker/SKILL.md",
                     "home/.claude/projects/upgrade-check/memory/upgrade-check-marker.md",
                     "install/machines/fixture.local.md kept marker", "install/machines/fixture.local.json kept marker",
                     "settings.json marker PreToolUse entry", "settings.json marker permission",
                     "home/.claude/local/machine.local.md kept marker"):
            self.assertIn("PASS " + line, text)
        self.assertFalse(self.work.exists())
        self.assertEqual(len(self.calls), 4)
        for command, cwd, env, log in self.calls:
            self.assertEqual(command[0], sys.executable)
            if log.name == "bundle.log":
                continue
            self.assertEqual(env["HOME"], str(self.work / "home"))
            self.assertEqual(env["USERPROFILE"], str(self.work / "home"))
            self.assertEqual(env["XDG_CONFIG_HOME"], str(self.work / "home/.config"))
        self.assertIn("--no-tests", self.calls[0][0])
        self.assertIn("--no-tests", self.calls[-1][0])
        build = self.calls[1][0]
        self.assertEqual(build[build.index("--home") + 1], str(Path.home()))
        self.assertEqual(Path(self.calls[2][0][1]), self.work / "install/install.py")

    def test_bundle_build_keeps_process_environment(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.base / "process-home"),
                                          "USERPROFILE": str(self.base / "process-profile"),
                                          "XDG_CONFIG_HOME": str(self.base / "process-config"),
                                          "UPGRADE_CHECK_ENV": "fixture"}):
            expected = os.environ.copy()
            code, text = self.invoke()
            self.assertEqual(code, 0, text)
            for command, cwd, env, log in self.calls:
                if log.name == "bundle.log":
                    self.assertEqual(env, expected)
                    self.assertEqual(command[command.index("--home") + 1], str(Path.home()))
                else:
                    self.assertEqual(env["HOME"], str(self.work / "home"))
                    self.assertEqual(env["USERPROFILE"], str(self.work / "home"))
                    self.assertEqual(env["XDG_CONFIG_HOME"], str(self.work / "home/.config"))

    def test_with_tests_and_keep(self):
        code, text = self.invoke("--with-tests", "--keep", "--from", str(self.baseline))
        self.assertEqual(code, 0, text)
        self.assertNotIn("--no-tests", self.calls[-1][0])
        self.assertTrue((self.work / "install.log").is_file())
        self.assertTrue((self.work / "unpack.log").is_file())

    def test_each_plain_marker_missing_or_changed(self):
        relatives = ["install/HANDOFF-upgrade-check.md", "install/.scratch/upgrade-check-marker.txt",
                     "install/bundles/upgrade-check-marker.txt", "install/inbox/upgrade-check-marker.txt",
                     "home/.claude/skills/upgrade-check-marker/SKILL.md",
                     "home/.claude/projects/upgrade-check/memory/upgrade-check-marker.md"]
        def damage():
            for index, relative in enumerate(relatives):
                path = self.work / relative
                if index % 2:
                    write(path, "changed\n")
                else:
                    path.unlink()
        self.on_install = damage
        code, text = self.invoke()
        self.assertEqual(code, 1)
        for relative in relatives:
            self.assertIn("FAIL " + relative, text)
        self.assertTrue(self.work.is_dir())

    def test_local_replacement_backup_warn_diff_exit_zero(self):
        self.on_unpack = self.replace_local
        code, text = self.invoke("--keep")
        self.assertEqual(code, 0, text)
        self.assertIn("WARN install/machines/fixture.local.md replaced; backup", text)
        self.assertIn("WARN install/machines/fixture.local.json replaced; backup", text)
        self.assertIn("old/machines/fixture.local.md", text)
        self.assertIn("-Old release facts.", text)
        self.assertIn("+New release facts.", text)

    def test_only_warn_still_cleans_up(self):
        self.on_unpack = self.replace_local
        code, text = self.invoke()
        self.assertEqual(code, 0, text)
        self.assertIn("WARN", text)
        self.assertFalse(self.work.exists())

    def test_cleanup_retries_readonly_files_and_preserves_pass_on_failure(self):
        original_rmtree = shutil.rmtree
        for mode in ("legacy", "modern", "failure"):
            with self.subTest(mode=mode):
                self.work = self.base / mode
                target = self.work / "install/readonly.txt"
                def make_readonly():
                    write(target, "Fixture.\n")
                    target.chmod(stat.S_IREAD)
                self.on_install = make_readonly
                retried = []
                def operation(path):
                    self.assertTrue(Path(path).stat().st_mode & stat.S_IWRITE)
                    retried.append(path)
                    if mode == "failure":
                        raise PermissionError("fixture cleanup failure")
                    Path(path).unlink()
                def rmtree(path, *, onerror):
                    error = PermissionError("fixture readonly file")
                    detail = (PermissionError, error, None) if mode == "legacy" else error
                    onerror(operation, str(target), detail)
                    original_rmtree(path)
                with mock.patch.object(check.shutil, "rmtree", side_effect=rmtree):
                    code, text = self.invoke()
                self.assertEqual(code, 0, text)
                self.assertEqual(retried, [str(target)])
                if mode == "failure":
                    self.assertTrue(self.work.is_dir())
                    self.assertIn(f"INFO could not remove {self.work}: fixture cleanup failure", text)
                else:
                    self.assertFalse(self.work.exists())

    def test_diff_limited_to_sixty_lines(self):
        def replace():
            self.replace_local()
            write(self.work / "install/machines/fixture.local.md", "\n".join(f"New line {i}" for i in range(100)))
        self.on_unpack = replace
        code, text = self.invoke()
        self.assertEqual(code, 0, text)
        self.assertIn("diff truncated at 60 lines", text)
        self.assertNotIn("+New line 99", text)

    def test_local_replacement_without_backup_fails(self):
        self.on_unpack = lambda: self.replace_local(backup=False)
        code, text = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("FAIL install/machines/fixture.local.md lost marker without a backup", text)
        self.assertIn("FAIL install/machines/fixture.local.json lost marker without a backup", text)

    def test_backups_require_new_unpack_folder_and_exact_relative_path(self):
        original = self.runner
        folder = "unpack-backup-20260101-010203"
        for mode in ("loose", "wrong-folder", "wrong-relative", "preexisting", "fresh"):
            with self.subTest(mode=mode):
                self.work = self.base / mode
                def runner(command, **kwargs):
                    code = original(command, **kwargs)
                    if kwargs["log"].name == "baseline.log" and mode == "preexisting":
                        (self.work / "install/.scratch" / folder).mkdir(parents=True)
                    return code
                self.runner = runner
                def replace():
                    install = self.work / "install"
                    for suffix in ("md", "json"):
                        path = install / "machines" / f"fixture.local.{suffix}"
                        if mode == "loose":
                            saved = install / ".scratch" / path.name
                        elif mode == "wrong-folder":
                            saved = install / ".scratch/saved/machines" / path.name
                        elif mode == "wrong-relative":
                            saved = install / ".scratch" / folder / "other" / path.name
                        else:
                            saved = install / ".scratch" / folder / "machines" / path.name
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, saved)
                    self.replace_local(backup=False)
                self.on_unpack = replace
                code, text = self.invoke("--keep")
                if mode == "fresh":
                    self.assertEqual(code, 0, text)
                    self.assertIn("WARN install/machines/fixture.local.md replaced; backup", text)
                else:
                    self.assertEqual(code, 1, text)
                    self.assertIn("FAIL install/machines/fixture.local.md lost marker without a backup", text)
                    self.assertIn("FAIL install/machines/fixture.local.json lost marker without a backup", text)

    def test_local_files_absent_after_baseline(self):
        bundle(self.baseline, local=False)
        code, text = self.invoke()
        self.assertEqual(code, 0, text)
        self.assertNotIn("install/machines/fixture.local", text)

    def test_settings_hook_permission_and_invalid_file(self):
        for mode in ("hook", "permission", "invalid", "missing"):
            with self.subTest(mode=mode):
                self.work = self.base / mode
                def damage():
                    path = self.work / "home/.claude/settings.json"
                    value = check.read_json(path)
                    if mode == "missing":
                        path.unlink()
                    elif mode == "invalid":
                        write(path, "invalid")
                    else:
                        value["hooks"]["PreToolUse"] = [] if mode == "hook" else [check.HOOK]
                        value["permissions"]["allow"] = [] if mode == "permission" else [check.PERMISSION]
                        check.write_json(path, value)
                self.on_install = damage
                code, text = self.invoke()
                self.assertEqual(code, 1)
                self.assertIn("FAIL settings.json marker", text)

    def test_home_local_rewrite_and_disappearance(self):
        self.on_install = lambda: write(self.work / "home/.claude/local/machine.local.md", "Rewritten facts.\n")
        code, text = self.invoke()
        self.assertEqual(code, 0, text)
        self.assertIn("WARN home/.claude/local/machine.local.md rewritten from", text)
        self.work = self.base / "missing-home-local"
        self.on_install = lambda: (self.work / "home/.claude/local/machine.local.md").unlink()
        code, text = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("FAIL home/.claude/local/machine.local.md disappeared", text)

    def test_home_change_summary(self):
        def change():
            root = self.work / "home/.claude"
            write(root / "local/machine.local.md", "Changed\n")
            write(root / "new.txt", "Added\n")
            (root / "skills/upgrade-check-marker/SKILL.md").unlink()
        self.on_install = change
        code, text = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("INFO home/.claude: 1 added, 1 changed, 1 removed", text)
        self.assertIn("  local/machine.local.md", text)
        self.assertIn("hand patches to harness-owned home files are reverted", text)

    def test_unpack_failure_skips_install(self):
        self.unpack_code = 8
        code, text = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("FAIL unpack exited 8", text)
        self.assertIn("output 24", text)
        self.assertEqual(len(self.calls), 3)

    def test_install_failure(self):
        self.install_code = 7
        code, text = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("FAIL install exited 7", text)

    def test_usage_and_help(self):
        for arguments, expected in (([], 3), (["--machine", "../fixture"], 3)):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = check.main(arguments)
            self.assertEqual(code, expected)
            self.assertTrue(output.getvalue().splitlines()[-1].startswith("next:"))

    def test_help_and_system_exit_codes(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = check.main(["--help"])
        self.assertEqual(code, 0)
        self.assertIs(type(code), int)
        self.assertIn("usage:", output.getvalue())
        self.assertNotIn("next:", output.getvalue())
        for exit_code, expected in ((None, 0), (0, 0), (2, 2), ("fixture error", 3)):
            with self.subTest(exit_code=exit_code):
                output = io.StringIO()
                with mock.patch.object(check, "parse_args", side_effect=SystemExit(exit_code)), contextlib.redirect_stdout(output):
                    code = check.main([])
                self.assertEqual(code, expected)
                self.assertIs(type(code), int)
                self.assertEqual("next:" in output.getvalue(), expected != 0)

    def test_refuses_external_writes_before_any_subprocess(self):
        for index, config in enumerate(({"delete": ["../outside"]}, {"proxy": {"bin_dir": "../outside"}},
                                        {"project_settings_moves": [{"from": "../outside"}]})):
            with self.subTest(config=config):
                self.work = self.base / f"refusal{index}"
                bundle(self.baseline, config=config)
                code, text = self.invoke()
                self.assertEqual(code, 2)
                self.assertIn("refused side effect", text)
                self.assertEqual(self.calls, [])

    def test_refuses_new_bundle_external_write(self):
        original = self.runner
        def runner(command, **kwargs):
            code = original(command, **kwargs)
            if kwargs["log"].name == "bundle.log":
                bundle(self.work / "new.zip", config={"delete": ["../outside"]})
            return code
        self.runner = runner
        code, text = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("refused side effect", text)
        self.assertEqual(len(self.calls), 2)

    def test_manifest_source_local_and_named_home_paths_are_confined(self):
        for kind in ("manifest", "source", "named-home"):
            with self.subTest(kind=kind):
                self.work = self.base / kind
                if kind == "manifest":
                    check.write_json(self.repo / "install-manifest.json", {"delete": ["../outside"]})
                elif kind == "source":
                    check.write_json(self.repo / "install-manifest.json", {})
                    check.write_json(self.repo / "machines/fixture.local.json", {"delete": ["../outside"]})
                else:
                    (self.repo / "machines/fixture.local.json").unlink()
                    bundle(self.baseline, config={"project_settings_moves": [{"from": "~someone/outside"}]})
                code, text = self.invoke()
                self.assertEqual(code, 2)
                self.assertIn("refused side effect", text)
                self.assertEqual(self.calls, [])

    def test_runner_writes_full_log_and_passes_environment(self):
        environment = check.scratch_environment(self.base / "runner-home")
        log = self.base / "runner.log"
        result = subprocess.CompletedProcess(["fixture"], 4, "all output\n")
        with mock.patch.object(check.subprocess, "run", return_value=result) as runner:
            code = check.run([sys.executable, "fixture.py"], cwd=self.repo, env=environment, log=log)
        self.assertEqual(code, 4)
        self.assertEqual(log.read_text(encoding="utf-8"), "all output\n")
        self.assertEqual(runner.call_args.kwargs["env"], environment)
        self.assertEqual(runner.call_args.kwargs["stderr"], subprocess.STDOUT)

    def test_refuses_nonempty_work_and_unsafe_zip(self):
        write(self.work / "user.txt", "Keep this.\n")
        code, text = self.invoke()
        self.assertEqual(code, 2)
        self.assertTrue((self.work / "user.txt").is_file())
        self.work = self.base / "other-work"
        with zipfile.ZipFile(self.baseline, "a") as archive:
            archive.writestr("../outside.txt", "Escape")
        code, text = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("unsafe archive path", text)
        self.assertEqual(self.calls, [])


class EndToEndTests(unittest.TestCase):
    @unittest.skipUnless((check.REPO / "bundle-terms.txt").is_file(), "bundle creation requires repository terms")
    def test_bundle_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            repo, builder = base / "repo", base / "builder"
            repo.mkdir()
            ignore = shutil.ignore_patterns("*.local.json", "*.local.md", "*.bundle-terms.txt", "__pycache__")
            for directory in (".claude", "machines", "claude", "templates", "codex", "tools", "publish", "monitor"):
                source = check.REPO / directory
                if source.exists():
                    shutil.copytree(source, repo / directory, ignore=ignore)
            for filename in ("install.py", "install-manifest.json", "bundle-terms.txt"):
                shutil.copy2(check.REPO / filename, repo / filename)
            machine = sorted(path.stem for path in (repo / "machines").glob("*.json")
                             if not path.name.endswith(".local.json"))[0]
            outside = base / "original/settings.json"
            write(outside, '{"original": true}\n')
            for path in (repo / "machines").glob("*.json"):
                config = check.read_json(path)
                config["owns"] = []
                if path.stem == machine:
                    config["design"] = False
                    config["proxy"] = {"host": "proxy.example.invalid", "key_file": "~/.claude/local/proxy-client.key"}
                    config["remove_allow_rules"] = [{"file": str(outside), "match": "fixture"}]
                check.write_json(path, config)
            check.write_json(repo / "machines/domain-owner.json", {"client": False, "owns": ["probe"]})
            write(builder / ".claude/local/bundle-terms/probe.txt", "bundleprobe" + " exclusion\n")
            environment = check.scratch_environment(builder)
            baseline = base / "baseline.zip"
            code = check.run([sys.executable, str(repo / "install.py"), "--bundle", machine,
                              "--out", str(baseline), "--repo", str(repo), "--home", str(builder)],
                             cwd=repo, env=environment, log=base / "build.log")
            self.assertEqual(code, 0, (base / "build.log").read_text(encoding="utf-8"))
            output = io.StringIO()
            with mock.patch.object(check.Path, "home", return_value=builder), contextlib.redirect_stdout(output):
                code = check.main(["--machine", machine, "--repo", str(repo), "--from", str(baseline),
                                   "--work", str(base / "work"), "--keep"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertTrue(output.getvalue().splitlines()[-1].startswith("next:"))
            self.assertIn("INFO planted a placeholder proxy key; the proxy itself is not exercised", output.getvalue())
            self.assertIn(f"INFO remove_allow_rules for {outside} replayed on a scratch copy", output.getvalue())
            self.assertEqual((base / "work/home/.claude/local/proxy-client.key").read_text(encoding="utf-8"), "upgrade-check-placeholder\n")
            self.assertEqual(check.read_json(base / "work/outside/0/settings.json"), {"permissions": {"allow": []}})
            self.assertEqual(outside.read_text(encoding="utf-8"), '{"original": true}\n')


if __name__ == "__main__":
    unittest.main()
